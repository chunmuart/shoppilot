"""统一模型目录、版本快照与加密凭据；不执行 Agent、不读取买家聊天。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import aiosqlite
import yaml
from agentscope.model import ModelCard
from cryptography.fernet import Fernet, InvalidToken

from app.infrastructure.model_connection import ModelConfigurationError, probe_connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ModelRevisionConflict(ModelConfigurationError):
    pass


class RuntimeConfiguration(dict):
    def __repr__(self) -> str:
        return f"RuntimeConfiguration(profile_id={self.get('profile_id')!r}, model_id={self.get('model_id')!r})"


class ModelRegistry:
    """所有读取返回独立值；历史版本只追加，运行快照不跟随页面配置改写。"""

    def __init__(self, settings, *, database_path: Path | None = None, probe=None):
        self.settings = settings
        self.path = Path(database_path or Path(settings.data_dir) / "models.sqlite3")
        self._lock = asyncio.Lock()
        self._ready = False
        self._cipher: Fernet | None = None
        self._secrets: dict[str, bytes] = {}
        self._probe = probe or probe_connection
        self._probe_limit = asyncio.Semaphore(2)
        self._allowed_hosts = tuple(getattr(settings, "model_allowed_local_hosts", ()))

    def _load_cipher(self) -> Fernet:
        master = getattr(self.settings, "model_master_key", "")
        if master:
            try:
                return Fernet(master.encode())
            except ValueError:
                raise ModelConfigurationError("MODEL_MASTER_KEY 必须是 Fernet 格式密钥") from None
        directory = Path(self.settings.data_dir) / ".secrets"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        key_path = directory / "model_registry.key"
        if not key_path.exists():
            try:
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
        os.chmod(key_path, 0o600)
        try:
            return Fernet(key_path.read_bytes())
        except ValueError:
            raise ModelConfigurationError("模型凭据主密钥文件无效，请恢复原密钥") from None

    async def initialize(self) -> None:
        async with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._cipher = self._load_cipher()
            async with aiosqlite.connect(self.path) as db:
                await db.executescript('''
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS model_profiles (
                        profile_id TEXT NOT NULL, revision INTEGER NOT NULL, buyer_id TEXT NOT NULL,
                        profile_json TEXT NOT NULL, PRIMARY KEY(profile_id, revision));
                    CREATE TABLE IF NOT EXISTS model_secrets (
                        secret_ref TEXT PRIMARY KEY, ciphertext BLOB NOT NULL);
                    CREATE TABLE IF NOT EXISTS model_validation_tickets (
                        ticket TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, config_hash TEXT NOT NULL,
                        draft_json TEXT NOT NULL, ciphertext BLOB NOT NULL,
                        profile_id TEXT, expected_revision INTEGER, expires_at REAL NOT NULL,
                        result_json TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS model_buyer_defaults (
                        buyer_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, selection_json TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS model_display_preferences (
                        buyer_id TEXT NOT NULL, profile_id TEXT NOT NULL, preferences_json TEXT NOT NULL,
                        PRIMARY KEY(buyer_id, profile_id));
                ''')
                # 部署配置变更追加版本；买家显示/窗口偏好存在独立表，不被启动覆盖。
                await db.execute("BEGIN IMMEDIATE")
                for profile in reversed(self._migrate_settings()):
                    if profile.get("fallback_profile_id"):
                        fallback_revision = await (await db.execute(
                            "SELECT MAX(revision) FROM model_profiles WHERE profile_id=?",
                            (profile["fallback_profile_id"],),
                        )).fetchone()
                        profile["fallback_profile_revision"] = fallback_revision[0]
                    row = await (await db.execute(
                        "SELECT revision,profile_json FROM model_profiles WHERE profile_id=? ORDER BY revision DESC LIMIT 1",
                        (profile["profile_id"],),
                    )).fetchone()
                    previous = json.loads(row[1]) if row else {}
                    previous_ref = previous.get("secret_ref", "")
                    previous_secret = await (await db.execute(
                        "SELECT ciphertext FROM model_secrets WHERE secret_ref=?", (previous_ref,),
                    )).fetchone()
                    try:
                        same_key = bool(previous_secret and self._cipher.decrypt(previous_secret[0]).decode() == self.settings.llm_api_key)
                    except InvalidToken:
                        raise ModelConfigurationError("历史模型凭据无法解密，请恢复原主密钥") from None
                    if same_key:
                        profile["secret_ref"] = previous_ref
                    else:
                        # 内置配置也冻结凭据，避免换网关后把新密钥带入旧运行。
                        profile["secret_ref"] = "builtin:" + uuid.uuid4().hex
                        await db.execute("INSERT INTO model_secrets VALUES(?,?)",
                            (profile["secret_ref"], self._cipher.encrypt(self.settings.llm_api_key.encode())))
                    if row:
                        previous["profile_revision"] = 1
                        if previous == profile:
                            continue
                        profile["profile_revision"] = row[0] + 1
                    await db.execute("INSERT INTO model_profiles VALUES(?,?,?,?)",
                        (profile["profile_id"], profile["profile_revision"], "*", _json(profile)))
                await db.commit()
                cursor = await db.execute("SELECT secret_ref,ciphertext FROM model_secrets")
                self._secrets = dict(await cursor.fetchall())
            os.chmod(self.path, 0o600)
            self._ready = True

    def presets(self) -> list[dict]:
        """官方参考模板与已校验连接分开；读取模板不创建运行 profile。"""
        config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/models.yaml").read_text())
        defaults = config.get("preset_defaults", {})
        return [{**deepcopy(defaults), **deepcopy(item)} for item in config.get("presets", [])]

    def _migrate_settings(self) -> list[dict]:
        config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/models.yaml").read_text())
        template = config["migration"]
        thinking = getattr(self.settings, "llm_enable_thinking", None)
        window = int(self.settings.context_size)
        def profile(profile_id, model, context, label, visible=True):
            budgets = sorted({int(x) for x in template["local_budget_options"] if 16384 <= x <= context} | {context})
            return {"profile_id": profile_id, "profile_revision": 1, "label": label,
                "model_id": model, "provider": template["provider"], "protocol": "openai_chat",
                "base_url": self.settings.llm_base_url.rstrip("/"), "secret_ref": "env:llm",
                "source": deepcopy(template["source"]), "status": "active", "visible": visible,
                "capabilities": {"context_max": context, "max_input": None, "max_output": int(template["output_reserve"]),
                                 "streaming": True, "tool_calling": True},
                "window_options": [{"id": f"budget-{x}", "tokens": x, "kind": "local_budget"} for x in budgets],
                "default_window": f"budget-{context}",
                "reasoning": {"kind": "boolean" if thinking is not None else "unsupported",
                              "options": ["default", "off", "on"] if thinking is not None else ["default"],
                              "default": "default" if thinking is None else "on" if thinking else "off"},
                "counter": deepcopy(template["counter"]), "quota_group": "current-gateway",
                "validation": {"scope": "沿用当前部署；未执行新连通性校验", "context_verified": False}}
        primary = profile("current-gateway", self.settings.llm_model, window, self.settings.llm_model)
        result = [primary]
        fallback = getattr(self.settings, "llm_fallback_model", "")
        if fallback and fallback != self.settings.llm_model:
            fallback_window = getattr(self.settings, "llm_fallback_context_size", None) or window
            result.append(profile("current-fallback", fallback, int(fallback_window), fallback, False))
            primary["fallback_profile_id"] = "current-fallback"
        return result

    async def _profile(self, profile_id: str, buyer_id: str, revision: int | None = None) -> dict:
        await self.initialize()
        async with aiosqlite.connect(self.path) as db:
            clause, args = (" AND revision=?", [revision]) if revision is not None else ("", [])
            cursor = await db.execute("SELECT profile_json FROM model_profiles WHERE profile_id=? AND buyer_id IN (?, '*')"
                + clause + " ORDER BY revision DESC LIMIT 1", [profile_id, buyer_id, *args])
            row = await cursor.fetchone()
            if not row:
                raise ModelConfigurationError("模型配置不存在或不属于当前买家")
            profile = json.loads(row[0])
            ref = profile.get("secret_ref", "")
            # API 与执行 worker 可能不同进程；按已校验归属的版本加载新凭据。
            if ref != "env:llm" and ref not in self._secrets:
                secret = await (await db.execute("SELECT ciphertext FROM model_secrets WHERE secret_ref=?", (ref,))).fetchone()
                if not secret:
                    raise ModelConfigurationError("模型凭据不存在，请重新配置连接")
                self._secrets[ref] = secret[0]
        return profile

    def _connection_identity(self, profile: dict, ciphertexts: dict[str, bytes], *, api_key: str | None = None) -> tuple | None:
        """仅在服务端比较实际连接；密钥及其摘要均不进入公开目录。"""
        if api_key is None:
            ref = profile.get("secret_ref", "")
            if ref == "env:llm":
                if profile["base_url"].rstrip("/") != self.settings.llm_base_url.rstrip("/"):
                    return None
                api_key = self.settings.llm_api_key
            else:
                try:
                    api_key = self._cipher.decrypt(ciphertexts[ref]).decode()
                except (KeyError, InvalidToken, UnicodeDecodeError):
                    return None
        if not api_key:
            return None
        return (profile["protocol"], profile["base_url"].rstrip("/"),
                hashlib.sha256(api_key.encode()).hexdigest(), profile["model_id"])

    @staticmethod
    def _runtime_configuration(profile: dict) -> str:
        """显示名称与来源备注不产生新模型；窗口、思考、回退等运行差异必须保留。"""
        fields = ("capabilities", "default_window", "reasoning", "counter", "fallback_profile_id", "fallback_profile_revision")
        value = {key: deepcopy(profile.get(key)) for key in fields}
        value["window_options"] = sorted(profile["window_options"], key=lambda item: item["id"])
        value["reasoning"]["options"] = sorted(set(value["reasoning"]["options"]))
        return _json(value)

    @staticmethod
    def _catalog_priority(profile: dict, preferences: dict) -> tuple:
        validation = profile.get("validation", {})
        verified_preset = bool(profile.get("preset_id") and validation.get("streaming") and validation.get("tool_calling"))
        return (bool(preferences.get("catalog_archived")),
                not verified_preset, validation.get("model_identity_matches") is not True,
                not preferences.get("visible", profile.get("visible", True)),
                profile["profile_id"] != "current-gateway", profile["profile_id"])

    async def catalog(self, buyer_id: str) -> dict:
        await self.initialize()
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute('''SELECT profile_json FROM model_profiles p
                WHERE buyer_id IN (?, '*') AND revision=(SELECT MAX(revision) FROM model_profiles WHERE profile_id=p.profile_id)
                ORDER BY profile_id''', (buyer_id,))).fetchall()
            ciphertexts = dict(await (await db.execute("SELECT secret_ref,ciphertext FROM model_secrets")).fetchall())
            prefs = dict(await (await db.execute("SELECT profile_id,preferences_json FROM model_display_preferences WHERE buyer_id=?", (buyer_id,))).fetchall())
            default_row = await (await db.execute("SELECT revision,selection_json FROM model_buyer_defaults WHERE buyer_id=?", (buyer_id,))).fetchone()
        profiles = [json.loads(raw) for (raw,) in rows]
        preferences = {key: json.loads(raw) for key, raw in prefs.items()}
        presets = self.presets()
        config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/models.yaml").read_text())
        labels = {**config.get("display_names", {}), **{item["model_id"]: item["label"] for item in presets}}
        identities = {item["profile_id"]: self._connection_identity(item, ciphertexts) for item in profiles}
        groups: dict[tuple, list[dict]] = {}
        for profile in profiles:
            identity = identities[profile["profile_id"]]
            if identity is not None:
                groups.setdefault(identity, []).append(profile)
        canonical = {item["profile_id"]: min(group, key=lambda p: self._catalog_priority(p, preferences.get(p["profile_id"], {})))["profile_id"]
                     for group in groups.values() for item in group}
        current_identity = identities["current-gateway"]
        public = []
        for profile in profiles:
            item = deepcopy(profile)
            secret_ref = item.pop("secret_ref", "")
            item["key_configured"] = bool(self.settings.llm_api_key) if secret_ref == "env:llm" else secret_ref in ciphertexts
            item["key_last_four"] = ""  # 不回传 env 凭据片段；自定义仅显示是否配置。
            item["builtin"] = not item["profile_id"].startswith("custom-")
            item.update(preferences.get(item["profile_id"], {}))
            identity = identities[item["profile_id"]]
            same_gateway = bool(identity and current_identity and identity[:3] == current_identity[:3])
            item["catalog_group"] = "default" if item["builtin"] or (same_gateway and item.get("preset_id") and item.get("credential_source")) else "custom"
            item["canonical_profile_id"] = canonical.get(item["profile_id"], item["profile_id"])
            item["catalog_hidden"] = bool(item.get("catalog_archived")) or item["canonical_profile_id"] != item["profile_id"]
            # 只统一模型原名的旧写法，保留买家起的展示名；历史快照也不回写。
            if item["label"].lower().replace(" ", "-") == item["model_id"].lower():
                item["label"] = labels.get(item["model_id"], item["label"])
            public.append(item)
        current = next(item for item in public if item["profile_id"] == "current-gateway")
        default_selection = json.loads(default_row[1]) if default_row else {
            "profile_id": "current-gateway", "window_id": current.get("preferred_window", current["default_window"]),
            "thinking": current.get("preferred_thinking", current["reasoning"]["default"]),
        }
        for preset in presets:
            registered = any(not item["catalog_hidden"] and identities[item["profile_id"]]
                and current_identity and identities[item["profile_id"]][:3] == current_identity[:3]
                and item["model_id"] == preset["model_id"] for item in public if not item.get("is_virtual"))
            if registered:
                continue
            public.append({**deepcopy(preset), "profile_id": "preset-" + preset["preset_id"], "profile_revision": 0,
                "base_url": current["base_url"], "provider": current["provider"], "status": "unconfigured",
                "builtin": True, "visible": True, "is_virtual": True, "catalog_hidden": False, "catalog_group": "default",
                "key_configured": False, "key_last_four": "", "connection_profile_id": "current-gateway",
                "connection_profile_revision": current["profile_revision"],
                "unavailable_reason": "尚未校验当前网关连接，请先连接"})
        return {"default_profile_id": default_selection["profile_id"], "models": public,
                "default_selection": default_selection, "default_revision": default_row[0] if default_row else 0,
                "presets": presets}

    async def resolve(self, profile_id: str = "current-gateway", window_id: str | None = None,
                      thinking: str | None = None, buyer_id: str = "pao-coder", revision: int | None = None) -> dict:
        profile = await self._profile(profile_id, buyer_id, revision)
        if revision is None and (window_id is None or thinking is None):
            async with aiosqlite.connect(self.path) as db:
                row = await (await db.execute("SELECT preferences_json FROM model_display_preferences WHERE buyer_id=? AND profile_id=?", (buyer_id, profile_id))).fetchone()
            preferences = json.loads(row[0]) if row else {}
            preferred_window = preferences.get("preferred_window")
            preferred_thinking = preferences.get("preferred_thinking")
            if window_id is None and preferred_window in {item["id"] for item in profile["window_options"]}:
                window_id = preferred_window
            if thinking is None and preferred_thinking in profile["reasoning"]["options"]:
                thinking = preferred_thinking
        window_id = window_id or profile["default_window"]
        choice = next((value for value in profile["window_options"] if value["id"] == window_id), None)
        if choice is None:
            raise ModelConfigurationError("所选上下文窗口不在该模型的可用档位中")
        thinking = thinking or profile["reasoning"]["default"]
        if thinking not in profile["reasoning"]["options"]:
            raise ModelConfigurationError("所选思考设置不受当前模型支持")
        parameters = self._reasoning_parameters(profile["reasoning"], thinking)
        capabilities = profile["capabilities"]
        snapshot = {"profile_id": profile_id, "profile_revision": profile["profile_revision"],
            "model_id": profile["model_id"], "label": profile["label"], "protocol": profile["protocol"],
            "base_url": profile["base_url"], "secret_ref": profile["secret_ref"],
            "context_size": choice["tokens"], "context_max": capabilities["context_max"],
            "max_input_tokens": capabilities["max_input"], "max_output_tokens": capabilities["max_output"],
            "reasoning_parameters": parameters, "thinking": thinking, "window_id": window_id,
            "counter": deepcopy(profile["counter"]), "quota_group": profile["quota_group"],
            "source": deepcopy(profile["source"]), "fallback": None}
        fallback = profile.get("fallback_profile_id")
        if fallback:
            snapshot["fallback"] = await self.resolve(fallback, buyer_id=buyer_id, revision=profile.get("fallback_profile_revision"))
        snapshot["fingerprint"] = _digest(snapshot)
        return snapshot

    def runtime(self, snapshot: dict) -> RuntimeConfiguration:
        snapshot = deepcopy(snapshot)
        fingerprint = snapshot.pop("fingerprint", None)
        if fingerprint != _digest(snapshot):
            raise ModelConfigurationError("运行模型快照校验失败")
        snapshot["fingerprint"] = fingerprint
        ref = snapshot["secret_ref"]
        if ref == "env:llm":
            # 兼容早期未冻结凭据的快照；网关已换时不得借用新密钥。
            if snapshot["base_url"].rstrip("/") != self.settings.llm_base_url.rstrip("/"):
                raise ModelConfigurationError("历史模型使用的网关已变更且缺少原凭据，请重新选择模型")
            key = self.settings.llm_api_key
        else:
            try:
                key = self._cipher.decrypt(self._secrets[ref]).decode()
            except (KeyError, InvalidToken, AttributeError):
                raise ModelConfigurationError("模型凭据不可用，请重新配置连接") from None
        snapshot["api_key"] = key
        if snapshot.get("fallback"):
            snapshot["fallback"] = self.runtime(snapshot["fallback"])
        return RuntimeConfiguration(snapshot)

    @staticmethod
    def _reasoning_parameters(reasoning: dict, thinking: str) -> dict:
        if thinking == "default":
            return {}
        if reasoning["kind"] == "boolean":
            return {"extra_body": {"enable_thinking": thinking == "on"}}
        if reasoning["kind"] == "effort":
            return {"reasoning_effort": thinking}
        return {}

    def _normalize(self, draft: dict) -> dict:
        def string(name, default="", max_length=200):
            value = draft.get(name, default)
            if not isinstance(value, str) or len(value) > max_length or any(ord(c) < 32 for c in value):
                raise ModelConfigurationError(f"{name} 格式无效")
            return value.strip()
        protocol = string("protocol", "openai_chat")
        if protocol != "openai_chat":
            raise ModelConfigurationError("当前只支持 OpenAI Chat Completions 兼容协议")
        model_id = string("model_id")
        if not model_id:
            raise ModelConfigurationError("请填写模型 ID")
        supplied = draft.get("capabilities", {})
        if not isinstance(supplied, dict):
            raise ModelConfigurationError("模型能力声明格式无效")
        try:
            context = int(supplied.get("context_max", draft.get("context_max", 128000)))
            output = int(supplied.get("max_output", draft.get("max_output_tokens", 8192)))
            raw_input = supplied.get("max_input", draft.get("max_input_tokens"))
            max_input = int(raw_input) if raw_input is not None else None
        except (ValueError, TypeError, OverflowError):
            raise ModelConfigurationError("模型容量必须是整数") from None
        if not 16384 <= context <= 4000000 or not 8192 <= output < context - 4096 or (max_input is not None and not 4096 <= max_input <= context):
            raise ModelConfigurationError("上下文需为 16K～4M，输出至少预留 8192，并留出输入与安全空间")
        label = string("label", model_id) or model_id
        ModelCard(name=model_id, label=label, status="active", context_size=context, output_size=output,
                  parameter_schema={}, parameters_overrides={})
        windows = draft.get("window_options") or [{"id": f"budget-{context}", "tokens": context, "kind": "local_budget"}]
        if not isinstance(windows, list) or len(windows) > 12:
            raise ModelConfigurationError("窗口档位数量无效")
        normalized_windows = []
        for window in windows:
            if not isinstance(window, dict):
                raise ModelConfigurationError("窗口档位格式无效")
            try:
                tokens = int(window["tokens"])
                identity = str(window["id"])
            except (ValueError, TypeError, KeyError):
                raise ModelConfigurationError("窗口档位格式无效") from None
            if (window.get("kind", "local_budget") != "local_budget" or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", identity)
                    or not output + 4096 < tokens <= context):
                raise ModelConfigurationError("仅支持模型容量范围内的离散本地窗口档位")
            normalized_windows.append({"id": identity, "tokens": tokens, "kind": "local_budget"})
        if len({value["id"] for value in normalized_windows}) != len(normalized_windows):
            raise ModelConfigurationError("窗口档位 ID 不能重复")
        default_window = string("default_window", normalized_windows[-1]["id"])
        if default_window not in {x["id"] for x in normalized_windows}:
            raise ModelConfigurationError("默认窗口必须属于已声明档位")
        reasoning = deepcopy(draft.get("reasoning") or {"kind": "unsupported", "options": ["default"], "default": "default"})
        if not isinstance(reasoning, dict):
            raise ModelConfigurationError("思考能力声明格式无效")
        kind = reasoning.get("kind")
        allowed = {"unsupported": {"default"}, "boolean": {"default", "off", "on"},
                   "effort": {"default", "minimal", "low", "medium", "high", "xhigh"}}
        if (not isinstance(kind, str) or kind not in allowed or not isinstance(reasoning.get("options"), list) or not reasoning["options"]
                or any(not isinstance(x, str) or x not in allowed[kind] for x in reasoning["options"]) or reasoning.get("default") not in reasoning["options"]):
            raise ModelConfigurationError("思考能力及档位声明无效")
        counter = deepcopy(draft.get("counter") or {"kind": "calibrated_estimate", "tokenizer_ref": None})
        if not isinstance(counter, dict) or counter.get("kind") != "calibrated_estimate" or counter.get("tokenizer_ref") is not None:
            raise ModelConfigurationError("当前只启用已接通的 AgentScope 校准估算计数器")
        normalized = {"label": label, "model_id": model_id, "provider": string("provider", "自定义网关"),
            "protocol": protocol, "base_url": string("base_url", max_length=2000).rstrip("/"),
            "capabilities": {"context_max": context, "max_input": max_input, "max_output": output,
                             "streaming": True, "tool_calling": True},
            "window_options": normalized_windows, "default_window": default_window,
            "reasoning": reasoning, "counter": counter, "status": "active", "visible": bool(draft.get("visible", True)),
            "source": {"kind": "operator_declared", "checked_at": None}}
        preset_id = draft.get("preset_id")
        if preset_id:
            preset = next((item for item in self.presets() if item["preset_id"] == preset_id), None)
            if preset is None:
                raise ModelConfigurationError("所选模型预设不存在，请刷新目录")
            if model_id != preset["model_id"]:
                raise ModelConfigurationError("模型 ID 与预设不一致，请取消预设后作为自定义模型添加")
            limits = preset["official_limits"]
            conservative_input = min(limits["max_input_off"], limits["max_input_on"])
            if (context > limits["context_max"] or output > limits["max_output"]
                    or (max_input is not None and max_input > conservative_input)):
                raise ModelConfigurationError("配置超过预设的官方容量上限；思考开关共用保守输入上限")
            normalized["capabilities"]["max_input"] = min(conservative_input, context) if max_input is None else max_input
            normalized.update(preset_id=preset_id, official_limits=deepcopy(limits), source=deepcopy(preset["source"]))
        return normalized

    async def _connection_profile(self, draft: dict, buyer_id: str, *, editing_profile_id: str | None = None,
                                  expected_revision: int | None = None) -> tuple[dict, dict | None]:
        profile = self._normalize(draft)
        source_id = draft.get("credential_profile_id")
        source_revision = draft.get("credential_profile_revision")
        preserved_quota = None
        if not source_id and source_revision is None and not draft.get("api_key") and editing_profile_id:
            previous = await self._profile(editing_profile_id, buyer_id)
            if expected_revision != previous["profile_revision"]:
                raise ModelRevisionConflict("模型配置已更新，请刷新后重试")
            inherited = previous.get("credential_source")
            if inherited:
                if profile["base_url"] != previous["base_url"] or profile["provider"] != previous["provider"]:
                    raise ModelConfigurationError("更换服务地址或供应商时请重新输入 API Key，原凭据不会发送到新地址")
                # 普通编辑只改名称/容量也不能丢失共享限流关系；始终沿用冻结来源版本。
                source_id, source_revision = inherited["profile_id"], inherited["profile_revision"]
                preserved_quota = previous["quota_group"]
        if not source_id:
            if source_revision is not None:
                raise ModelConfigurationError("复用凭据时必须同时指定模型配置与版本")
            return profile, None
        if not isinstance(source_id, str) or type(source_revision) is not int or source_revision < 1:
            raise ModelConfigurationError("复用凭据的模型配置或版本无效")
        if draft.get("api_key"):
            raise ModelConfigurationError("复用现有连接与输入新 API Key 只能选择一种")
        source = await self._profile(source_id, buyer_id, revision=source_revision)
        if profile["base_url"] != source["base_url"] or profile["provider"] != source["provider"]:
            raise ModelConfigurationError("复用凭据只能用于原服务地址与供应商，不能把现有密钥发送到新地址")
        profile["credential_source"] = {"profile_id": source_id, "profile_revision": source_revision}
        if preserved_quota is not None:
            source = {**source, "quota_group": preserved_quota}
        return profile, source

    async def validate_connection(self, draft: dict, buyer_id: str, *, profile_id: str | None = None,
                                  expected_revision: int | None = None) -> dict:
        await self.initialize()
        profile, credential_source = await self._connection_profile(draft, buyer_id,
            editing_profile_id=profile_id, expected_revision=expected_revision)
        api_key = draft.get("api_key") or ""
        if not isinstance(api_key, str) or len(api_key) > 16384 or any(ord(c) < 32 for c in api_key):
            raise ModelConfigurationError("API Key 格式无效")
        if credential_source is not None:
            source_snapshot = await self.resolve(credential_source["profile_id"], credential_source["default_window"],
                credential_source["reasoning"]["default"], buyer_id, revision=credential_source["profile_revision"])
            api_key = (await self.runtime_for_snapshot(source_snapshot, buyer_id))["api_key"]
        if profile_id:
            previous = await self._profile(profile_id, buyer_id)
            if not profile_id.startswith("custom-"):
                raise ModelConfigurationError("部署导入连接请通过环境配置维护，页面只支持编辑自定义连接")
            if expected_revision != previous["profile_revision"]:
                raise ModelRevisionConflict("模型配置已更新，请刷新后重试")
            if not api_key:
                if profile["base_url"] != previous["base_url"] or profile["provider"] != previous["provider"]:
                    raise ModelConfigurationError("更换服务地址或供应商时请重新输入 API Key，原凭据不会发送到新地址")
                api_key = self.runtime(await self.resolve(profile_id, buyer_id=buyer_id))["api_key"]
        if not api_key:
            raise ModelConfigurationError("请填写 API Key")
        # 每个可选思考参数都验证，不能只证明默认配置后宣称全部支持。
        results = []
        preset = next((item for item in self.presets() if item["preset_id"] == profile.get("preset_id")), {})
        for thinking in profile["reasoning"]["options"]:
            probe_profile = {**profile, "probe_reasoning_parameters": self._reasoning_parameters(profile["reasoning"], thinking),
                             "probe_model_aliases": preset.get("probe_model_aliases", [])}
            try:
                async with self._probe_limit:
                    results.append(await asyncio.wait_for(
                        self._probe(probe_profile, api_key, allowed_local_hosts=self._allowed_hosts), timeout=60))
            except asyncio.TimeoutError:
                raise ModelConfigurationError("模型校验超时，请稍后重试") from None
        identities = [item.get("model_identity_matches") for item in results]
        if profile.get("preset_id") and any(value is False for value in identities):
            raise ModelConfigurationError("网关返回的模型标识与所选预设不一致，请核实网关映射；未保存为该预设")
        returned_models = sorted({name for item in results for name in item.get("returned_models", [])})
        identity_matches = False if any(value is False for value in identities) else True if all(value is True for value in identities) else None
        result = {**results[0], "reasoning_options": profile["reasoning"]["options"],
                  "usage_available": all(item.get("usage_available", False) for item in results),
                  "requested_model": profile["model_id"], "returned_models": returned_models,
                  "model_identity_matches": identity_matches,
                  "context_verified": False}
        digest = _digest({"profile": profile, "key_hash": hashlib.sha256(api_key.encode()).hexdigest()})
        ticket = uuid.uuid4().hex
        expires = time.time() + 300
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM model_validation_tickets WHERE expires_at < ?", (time.time(),))
            await db.execute("INSERT INTO model_validation_tickets VALUES(?,?,?,?,?,?,?,?,?,0)",
                (ticket, buyer_id, digest, _json(profile), self._cipher.encrypt(api_key.encode()), profile_id,
                 expected_revision, expires, _json(result)))
            await db.commit()
        return {"validation_ticket": ticket, "config_hash": digest, "expires_at": expires, "validation": result}

    async def save_connection(self, draft: dict, buyer_id: str, *, validation_ticket: str, config_hash: str,
                              profile_id: str | None = None, expected_revision: int | None = None) -> dict:
        await self.initialize()
        normalized, credential_source = await self._connection_profile(draft, buyer_id,
            editing_profile_id=profile_id, expected_revision=expected_revision)
        reused = False
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT config_hash,draft_json,ciphertext,profile_id,expected_revision,expires_at,result_json,consumed FROM model_validation_tickets WHERE ticket=? AND buyer_id=?", (validation_ticket, buyer_id))).fetchone()
            if not row or row[7] or row[5] < time.time() or config_hash != row[0] or _json(normalized) != row[1] or profile_id != row[3] or expected_revision != row[4]:
                raise ModelConfigurationError("校验凭证已失效或配置改变，请重新校验")
            api_key = self._cipher.decrypt(row[2]).decode()
            if draft.get("api_key") and draft["api_key"] != api_key:
                raise ModelConfigurationError("API Key 在校验后发生变化，请重新校验")
            revision = 1
            if profile_id:
                previous = await (await db.execute("SELECT MAX(revision) FROM model_profiles WHERE profile_id=? AND buyer_id=?", (profile_id, buyer_id))).fetchone()
                if not previous or previous[0] != expected_revision:
                    raise ModelRevisionConflict("模型配置已更新，请刷新后重试")
                revision = expected_revision + 1
            else:
                # 检查和写入处于同一数据库事务，跨 worker 同时提交也不会制造重复。
                profiles = [json.loads(value[0]) for value in await (await db.execute('''SELECT profile_json FROM model_profiles p
                    WHERE buyer_id IN (?, '*') AND revision=(SELECT MAX(revision) FROM model_profiles WHERE profile_id=p.profile_id)''', (buyer_id,))).fetchall()]
                ciphertexts = dict(await (await db.execute("SELECT secret_ref,ciphertext FROM model_secrets")).fetchall())
                identity = self._connection_identity(normalized, {}, api_key=api_key)
                matches = [item for item in profiles if identity is not None and self._connection_identity(item, ciphertexts) == identity]
                equivalent = [item for item in matches if self._runtime_configuration(item) == self._runtime_configuration(normalized)]
                if equivalent:
                    preferences = {key: json.loads(value) for key, value in await (await db.execute(
                        "SELECT profile_id,preferences_json FROM model_display_preferences WHERE buyer_id=?", (buyer_id,))).fetchall()}
                    existing = min(equivalent, key=lambda item: self._catalog_priority(item, preferences.get(item["profile_id"], {})))
                    profile_id = existing["profile_id"]
                    reused = True
                    # 显式重新添加会恢复该买家的目录入口，仍不改变任何运行版本。
                    pref = preferences.get(profile_id, {})
                    pref.update(catalog_archived=False, visible=True)
                    await db.execute("INSERT INTO model_display_preferences VALUES(?,?,?) ON CONFLICT(buyer_id,profile_id) DO UPDATE SET preferences_json=excluded.preferences_json",
                        (buyer_id, profile_id, _json(pref)))
                elif matches and (not normalized.get("preset_id") or any(item.get("preset_id") for item in matches)):
                    raise ModelConfigurationError("此连接已配置该模型；如需修改窗口或思考设置，请编辑现有模型")
                else:
                    # 允许旧部署首次升级为经过校验的预设，旧版本和旧会话不迁移。
                    profile_id = "custom-" + uuid.uuid4().hex
            if not reused:
                ref = "model:" + uuid.uuid4().hex
                # 相同端点与凭据共用配额组；轮换凭据不会改变已冻结快照。
                quota = credential_source["quota_group"] if credential_source is not None else (
                    "connection:" + hashlib.sha256((normalized["base_url"] + "\0" + api_key).encode()).hexdigest()[:24])
                profile = {**normalized, "profile_id": profile_id, "profile_revision": revision,
                           "secret_ref": ref, "quota_group": quota, "validation": json.loads(row[6])}
                await db.execute("INSERT INTO model_secrets VALUES(?,?)", (ref, row[2]))
                await db.execute("INSERT INTO model_profiles VALUES(?,?,?,?)", (profile_id, revision, buyer_id, _json(profile)))
                self._secrets[ref] = row[2]
            await db.execute("UPDATE model_validation_tickets SET consumed=1 WHERE ticket=?", (validation_ticket,))
            await db.commit()
        item = next(item for item in (await self.catalog(buyer_id))["models"] if item["profile_id"] == profile_id)
        return {**item, "reused": reused}

    async def archive_from_catalog(self, profile_id: str, buyer_id: str, expected_revision: int) -> dict:
        """只隐藏这个买家的目录入口；历史会话、版本和凭据全部保留。"""
        await self._profile(profile_id, buyer_id)
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT MAX(revision) FROM model_profiles WHERE profile_id=? AND buyer_id IN (?, '*')",
                (profile_id, buyer_id))).fetchone()
            if not row or row[0] != expected_revision:
                raise ModelRevisionConflict("模型配置已更新，请刷新后重试")
            previous = await (await db.execute("SELECT preferences_json FROM model_display_preferences WHERE buyer_id=? AND profile_id=?",
                (buyer_id, profile_id))).fetchone()
            preferences = json.loads(previous[0]) if previous else {}
            preferences.update(catalog_archived=True, visible=False)
            await db.execute("INSERT INTO model_display_preferences VALUES(?,?,?) ON CONFLICT(buyer_id,profile_id) DO UPDATE SET preferences_json=excluded.preferences_json",
                (buyer_id, profile_id, _json(preferences)))
            await db.commit()
        return next(item for item in (await self.catalog(buyer_id))["models"] if item["profile_id"] == profile_id)

    async def set_preferences(self, profile_id: str, buyer_id: str, *, visible: bool | None = None,
                              default_window: str | None = None, thinking: str | None = None,
                              expected_revision: int | None = None) -> dict:
        await self.resolve(profile_id, default_window, thinking, buyer_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            version = await (await db.execute("SELECT MAX(revision) FROM model_profiles WHERE profile_id=? AND buyer_id IN (?, '*')", (profile_id, buyer_id))).fetchone()
            if expected_revision is not None and (not version or version[0] != expected_revision):
                raise ModelRevisionConflict("模型配置已更新，请刷新后重试")
            row = await (await db.execute("SELECT preferences_json FROM model_display_preferences WHERE buyer_id=? AND profile_id=?", (buyer_id, profile_id))).fetchone()
            prefs = json.loads(row[0]) if row else {}
            if visible is not None:
                prefs["visible"] = visible
            if default_window is not None:
                prefs["preferred_window"] = default_window
            if thinking is not None:
                prefs["preferred_thinking"] = thinking
            await db.execute("INSERT INTO model_display_preferences VALUES(?,?,?) ON CONFLICT(buyer_id,profile_id) DO UPDATE SET preferences_json=excluded.preferences_json", (buyer_id, profile_id, _json(prefs)))
            await db.commit()
        return next(item for item in (await self.catalog(buyer_id))["models"] if item["profile_id"] == profile_id)


    async def set_default(self, buyer_id: str, profile_id: str, window_id: str | None = None,
                          thinking: str | None = None, expected_revision: int = 0) -> dict:
        """修改之后新会话的默认选择；CAS 使用买家默认记录版本，不借用模型版本。"""
        resolved = await self.resolve(profile_id, window_id, thinking, buyer_id)
        selection = {"profile_id": profile_id, "window_id": resolved["window_id"], "thinking": resolved["thinking"]}
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT revision FROM model_buyer_defaults WHERE buyer_id=?", (buyer_id,))).fetchone()
            previous = row[0] if row else 0
            if expected_revision != previous:
                raise ModelRevisionConflict("新会话默认模型已更新，请刷新后重试")
            revision = previous + 1
            await db.execute("INSERT INTO model_buyer_defaults VALUES(?,?,?) ON CONFLICT(buyer_id) DO UPDATE SET revision=excluded.revision,selection_json=excluded.selection_json", (buyer_id, revision, _json(selection)))
            await db.commit()
        return {"default_profile_id": profile_id, "default_selection": selection, "default_revision": revision}

    async def resolve_default(self, buyer_id: str) -> dict:
        """新会话与后台默认操作统一入口；不改变已有运行快照。"""
        await self.initialize()
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT selection_json FROM model_buyer_defaults WHERE buyer_id=?", (buyer_id,))).fetchone()
        if not row:
            return await self.resolve("current-gateway", buyer_id=buyer_id)
        selection = json.loads(row[0])
        return await self.resolve(selection["profile_id"], selection.get("window_id"),
                                  selection.get("thinking"), buyer_id)

    async def resolve_imported_default(self, buyer_id: str) -> dict:
        """遗留会话沿用首次导入的模型配置，不能套用后来设置的新会话默认。"""
        profile = await self._profile("current-gateway", buyer_id, revision=1)
        snapshot = await self.resolve("current-gateway", profile["default_window"],
                                      profile["reasoning"]["default"], buyer_id, revision=1)
        snapshot["source"]["binding"] = "legacy_migration_inferred"
        snapshot.pop("fingerprint")
        snapshot["fingerprint"] = _digest(snapshot)
        return snapshot

    async def runtime_for_snapshot(self, snapshot: dict, buyer_id: str) -> RuntimeConfiguration:
        """恢复已冻结的运行：初始化、验证版本归属并补齐跨 worker 的凭据缓存。"""
        await self.initialize()

        async def prepare(item: dict) -> None:
            if not isinstance(item, dict):
                raise ModelConfigurationError("运行模型快照格式无效")
            signed = deepcopy(item)
            fingerprint = signed.pop("fingerprint", None)
            if fingerprint != _digest(signed):
                raise ModelConfigurationError("运行模型快照校验失败")
            profile_id, revision = item.get("profile_id"), item.get("profile_revision")
            if not isinstance(profile_id, str) or type(revision) is not int or revision < 1:
                raise ModelConfigurationError("运行模型快照版本无效")
            # _profile 同时校验 buyer 归属，并在缺失时从数据库读取这个版本的密文。
            profile = await self._profile(profile_id, buyer_id, revision)
            window = next((value for value in profile["window_options"] if value["id"] == item.get("window_id")), None)
            thinking = item.get("thinking")
            if window is None or thinking not in profile["reasoning"]["options"]:
                raise ModelConfigurationError("运行快照的窗口或思考设置不属于所选版本")
            capabilities = profile["capabilities"]
            expected = {"model_id": profile["model_id"], "label": profile["label"],
                "protocol": profile["protocol"], "base_url": profile["base_url"], "secret_ref": profile["secret_ref"],
                "context_size": window["tokens"], "context_max": capabilities["context_max"],
                "max_input_tokens": capabilities["max_input"], "max_output_tokens": capabilities["max_output"],
                "reasoning_parameters": self._reasoning_parameters(profile["reasoning"], thinking),
                "counter": profile["counter"], "quota_group": profile["quota_group"]}
            if any(item.get(key) != value for key, value in expected.items()):
                raise ModelConfigurationError("运行快照与已授权模型版本不一致")
            fallback = item.get("fallback")
            fallback_id = profile.get("fallback_profile_id")
            if fallback_id:
                if not isinstance(fallback, dict) or fallback.get("profile_id") != fallback_id:
                    raise ModelConfigurationError("运行快照的回退模型不属于所选版本")
                pinned = profile.get("fallback_profile_revision")
                if pinned is not None and fallback.get("profile_revision") != pinned:
                    raise ModelConfigurationError("运行快照的回退版本已被篡改")
                await prepare(fallback)
            elif fallback is not None:
                raise ModelConfigurationError("所选模型版本未配置回退模型")

        await prepare(snapshot)
        return self.runtime(snapshot)
