"""Prompt 不可变版本、人工发布与会话固定分组；SQLite 是本机多进程权威。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml


class PromptRegistryError(ValueError):
    pass


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def toolset_contract(root: Path, *, web_search_enabled: bool = False) -> dict:
    """绑定实际工具、权限与工厂源码；正文相同但工具契约不同也是不同版本。"""
    paths = [*sorted((root / "app/application/tools").glob("*.py")),
             *(root / "app/application/agents" / name for name in
               ("permissions.py", "main_agent.py", "search_agent.py", "trade_agent.py")),
             root / "app/infrastructure/capability_registry.py",
             root / "app/infrastructure/buyer_skills.py",
             root / "app/infrastructure/semantic_memory.py",
             root / "app/application/agents/tool_confirmation.py",
             root / "app/application/memory/middleware.py",
             root / "app/application/memory/preference_selector.py",
             root / "app/domain/buyer/preference.py"]
    # Skill/策略白名单放入这个目录后自动参与契约，未安装时不会宣称有能力。
    capability_paths = [*sorted((root / "app/application/skills").rglob("*.py")),
                        *sorted((root / "app/application/strategies").rglob("*.py"))]
    files = {str(path.relative_to(root)): sha256(path.read_bytes()) for path in [*paths, *capability_paths] if path.is_file()}
    return {"schema": 1, "web_search_enabled": web_search_enabled, "source_files": files}


def read_prompt(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise PromptRegistryError("Prompt 文件超过 1 MiB")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise PromptRegistryError("Prompt YAML 无法解析") from error
    try:
        roles = [document["main_agent"], document["sub_agents"]["search"], document["sub_agents"]["trade"]]
        if any(not isinstance(role["name"], str) or not role["name"].strip()
               or not isinstance(role["system_prompt"], str) or not role["system_prompt"].strip() for role in roles):
            raise ValueError
        canonical(document)
    except (KeyError, TypeError, ValueError) as error:
        raise PromptRegistryError("Prompt 需要有效的 main_agent/sub_agents.search/sub_agents.trade 名称和正文") from error
    return document, sha256(raw)


class PromptContractChanged(PromptRegistryError):
    """旧会话工具合同不可复用，可在新会话中重新发起当前需求。"""


class PromptRegistry:
    def __init__(self, path: Path, contract: dict, *, pinned_version: str = ""):
        self.path = path
        self.contract = contract
        self.pinned_version = pinned_version
        self.contract_hash = sha256(canonical(contract))

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS prompt_versions (version_id TEXT PRIMARY KEY, payload TEXT NOT NULL, yaml_sha256 TEXT NOT NULL, toolset_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS prompt_deployments (sequence INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT UNIQUE NOT NULL, baseline TEXT NOT NULL REFERENCES prompt_versions(version_id), candidate TEXT REFERENCES prompt_versions(version_id), candidate_bps INTEGER NOT NULL, experiment TEXT NOT NULL, action TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS prompt_assignments (session_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES prompt_versions(version_id), deployment_id TEXT NOT NULL, variant TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS prompt_revocations (version_id TEXT PRIMARY KEY REFERENCES prompt_versions(version_id), reason TEXT NOT NULL, created_at TEXT NOT NULL)")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _version(self, db, version_id: str, *, check_contract=True) -> dict:
        row = db.execute("SELECT * FROM prompt_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise PromptRegistryError("Prompt 版本不存在，请先导入不可变版本")
        if db.execute("SELECT 1 FROM prompt_revocations WHERE version_id=?", (version_id,)).fetchone():
            raise PromptRegistryError("该 Prompt 版本已被紧急撤销；原会话停止执行，请创建新会话")
        payload = json.loads(row["payload"])
        if "p-" + sha256(canonical(payload)) != version_id:
            raise PromptRegistryError("Prompt 版本内容 hash 不符，拒绝加载")
        if sha256(canonical(payload.get("toolset"))) != row["toolset_sha256"]:
            raise PromptRegistryError("Prompt 工具契约元数据 hash 不符，拒绝加载")
        if check_contract and row["toolset_sha256"] != self.contract_hash:
            raise PromptContractChanged("Prompt 绑定工具契约与当前代码不一致，请重新评测发布；不能静默换版本")
        return {"version_id": version_id, "content_sha256": version_id[2:], "yaml_sha256": row["yaml_sha256"],
                "toolset_sha256": row["toolset_sha256"], "created_at": row["created_at"], "document": payload["prompts"]}

    def import_version(self, path: Path) -> dict:
        document, yaml_hash = read_prompt(path)
        payload = canonical({"prompts": document, "toolset": self.contract})
        version_id = "p-" + sha256(payload)
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO prompt_versions VALUES (?,?,?,?,?)", (
                version_id, payload, yaml_hash, self.contract_hash, datetime.now(timezone.utc).isoformat()))
            return self._version(db, version_id)

    @staticmethod
    def _public_version(version: dict) -> dict:
        return {key: value for key, value in version.items() if key != "document"}

    def _deploy(self, db, baseline, candidate, candidate_bps, experiment, action, evidence):
        deployment_id = "pd-" + uuid.uuid4().hex
        db.execute("INSERT INTO prompt_deployments (deployment_id,baseline,candidate,candidate_bps,experiment,action,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (deployment_id, baseline, candidate, candidate_bps, experiment, action, canonical(evidence), datetime.now(timezone.utc).isoformat()))
        return deployment_id

    def bootstrap(self, path: Path) -> dict:
        # 首次启动采用当前行为；之后改 YAML 只能导入候选，不能自动切生产。
        with self._transaction() as db:
            existing = db.execute("SELECT 1 FROM prompt_deployments LIMIT 1").fetchone()
        if not existing:
            version = self.import_version(path)
            with self._transaction() as db:
                if db.execute("SELECT 1 FROM prompt_deployments LIMIT 1").fetchone() is None:
                    self._deploy(db, version["version_id"], None, 0, "bootstrap", "bootstrap", {"scope": "existing_behavior"})
        return self.describe()

    def describe(self) -> dict:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM prompt_deployments ORDER BY sequence DESC LIMIT 1").fetchone()
            if row is None:
                raise PromptRegistryError("Prompt 注册表尚未初始化")
            public = {key: row[key] for key in ("deployment_id", "baseline", "candidate", "candidate_bps", "experiment", "action", "created_at")}
            version_id = self.pinned_version or (row["baseline"] if row["candidate"] is None or row["candidate_bps"] == 0
                else row["candidate"] if row["candidate_bps"] == 10000 else "")
            public["pinned_version"] = self.pinned_version or None
            public["effective_version"] = self._public_version(self._version(db, version_id)) if version_id else None
            return public

    async def assign(self, session_id: str, buyer_id: str) -> dict:
        return await asyncio.to_thread(self._assign, session_id, buyer_id)

    async def existing_assignment(self, session_id: str, buyer_id: str) -> dict | None:
        """只读已有分配，供上下文展示使用；不创建库、版本或会话分组。"""
        return await asyncio.to_thread(self._existing_assignment, session_id, buyer_id)

    def _existing_assignment(self, session_id: str, buyer_id: str) -> dict | None:
        if not session_id or not buyer_id:
            raise PromptRegistryError("读取版本分组需要明确会话和买家")
        if not self.path.is_file():
            return None
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            assigned = connection.execute("SELECT * FROM prompt_assignments WHERE session_id=?", (session_id,)).fetchone()
            if assigned is None:
                return None
            if assigned["buyer_id"] != buyer_id:
                raise PromptRegistryError("会话版本分组属于其他买家")
            version = self._version(connection, assigned["version_id"])
            if self.pinned_version and self.pinned_version != version["version_id"]:
                raise PromptRegistryError("评测固定版本与旧会话分组不一致，请使用新的隔离会话")
            return {**dict(assigned), **version}
        finally:
            connection.close()

    def _assign(self, session_id, buyer_id):
        if not session_id or not buyer_id:
            raise PromptRegistryError("版本分组需要明确会话和买家")
        with self._transaction() as db:
            assigned = db.execute("SELECT * FROM prompt_assignments WHERE session_id=?", (session_id,)).fetchone()
            if assigned is None:
                deployment = db.execute("SELECT * FROM prompt_deployments ORDER BY sequence DESC LIMIT 1").fetchone()
                if deployment is None:
                    raise PromptRegistryError("尚未配置 Prompt 部署")
                bucket = int(sha256(canonical([deployment["experiment"], buyer_id]))[:16], 16) % 10000
                variant = "candidate" if deployment["candidate"] and bucket < deployment["candidate_bps"] else "baseline"
                version_id = self.pinned_version or deployment[variant]
                variant = "pinned" if self.pinned_version else variant
                version = self._version(db, version_id)
                db.execute("INSERT INTO prompt_assignments VALUES (?,?,?,?,?)", (session_id, buyer_id, version_id, deployment["deployment_id"], variant))
                assigned = {"session_id": session_id, "buyer_id": buyer_id, "version_id": version_id,
                            "deployment_id": deployment["deployment_id"], "variant": variant}
            else:
                if assigned["buyer_id"] != buyer_id:
                    raise PromptRegistryError("会话版本分组属于其他买家")
                version = self._version(db, assigned["version_id"])
                if self.pinned_version and self.pinned_version != version["version_id"]:
                    raise PromptRegistryError("评测固定版本与旧会话分组不一致，请使用新的隔离会话")
            return {**dict(assigned), **version}

    def publish(self, *, baseline: str, candidate: str, candidate_bps: int, experiment: str,
                baseline_manifest: Path, candidate_manifest: Path) -> dict:
        from app.infrastructure.prompt_release import compare_release_manifests
        if type(candidate_bps) is not int or not 0 <= candidate_bps <= 10000 or not experiment.strip():
            raise PromptRegistryError("候选流量需为 0..10000 基点且 experiment 不为空")
        # 独立于数据库事务读取证据后，在事务内再次核对版本与当前 baseline。
        evidence = compare_release_manifests(baseline_manifest, candidate_manifest, baseline, candidate)
        with self._transaction() as db:
            for version_id, label in ((baseline, "baseline"), (candidate, "candidate")):
                version = self._version(db, version_id)
                if evidence[label]["toolset_sha256"] != version["toolset_sha256"]:
                    raise PromptRegistryError("评测报告工具契约未对应注册表中的实际版本")
            current = db.execute("SELECT * FROM prompt_deployments ORDER BY sequence DESC LIMIT 1").fetchone()
            compatible_baseline = current is not None and baseline in {current["baseline"], current["candidate"]}
            if current is not None and not compatible_baseline:
                # 工具升级后允许以“正文完全相同、重新绑定新工具”的版本作成对基线。
                # 新基线和候选仍须在当前工具契约下分别跑完整 release，不能沿用旧报告。
                current_text = self._version(db, current["baseline"], check_contract=False)["document"]
                compatible_baseline = self._version(db, baseline)["document"] == current_text
            if not compatible_baseline:
                raise PromptRegistryError("基线不是当前部署的版本，拒绝过期发布")
            deployment_id = self._deploy(db, baseline, candidate, candidate_bps, experiment, "publish", evidence)
        return {"deployment_id": deployment_id, "evidence": evidence}

    def rollback(self, deployment_id: str) -> str:
        with self._transaction() as db:
            target = db.execute("SELECT * FROM prompt_deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
            if target is None:
                raise PromptRegistryError("回滚目标部署不存在")
            self._version(db, target["baseline"])
            if target["candidate"]:
                self._version(db, target["candidate"])
            return self._deploy(db, target["baseline"], target["candidate"], target["candidate_bps"], target["experiment"], "rollback", {"target_deployment": deployment_id})

    def revoke(self, version_id: str, reason: str) -> None:
        if not reason.strip():
            raise PromptRegistryError("紧急撤销必须说明原因")
        with self._transaction() as db:
            self._version(db, version_id, check_contract=False)
            db.execute("INSERT INTO prompt_revocations VALUES (?,?,?)", (version_id, reason, datetime.now(timezone.utc).isoformat()))
