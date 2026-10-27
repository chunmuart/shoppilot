# -*- coding: utf-8 -*-
"""Skill 与策略的不可变版本库。只有本机 CLI 可以审核发布，Agent 只读。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


CAPABILITY_CONTRACT_VERSION = "1"
# Skill 只能描述现有只读业务能力；不含偏好改写、交易确认、代码/文件执行。
SKILL_TOOL_ALLOWLIST = frozenset({"product_search_tool", "category_insight_tool", "conversation_fact_lookup",
                                "query_order_tool", "web_search_tool"})


class CapabilityError(ValueError):
    pass


class CapabilityVersionChanged(CapabilityError):
    """旧会话加载过的资料已改变，不能继续把历史正文当成当前有效策略。"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as error:
        raise CapabilityError("expires_at 必须是带时区的 ISO 时间") from error


def validate(document):
    if not isinstance(document, dict):
        raise CapabilityError("能力文档必须是 JSON 对象")
    permitted = {"kind", "id", "version", "title", "description", "body", "scope", "allowed_tools", "evidence", "expires_at", "keywords"}
    if set(document) - permitted:
        raise CapabilityError("存在未知字段；不能通过文档声明执行权限或动态工具")
    doc = json.loads(canonical(document))
    if doc.get("kind") not in {"skill", "strategy"}:
        raise CapabilityError("kind 只支持 skill 或 strategy")
    for key, maximum in (("id", 64), ("version", 40)):
        if not isinstance(doc.get(key), str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0," + str(maximum - 1) + "}", doc[key]):
            raise CapabilityError(f"{key} 需要安全、非空的版本标识")
    for key, maximum in (("title", 120), ("description", 400), ("body", 12000)):
        if not isinstance(doc.get(key), str) or not doc[key].strip() or len(doc[key]) > maximum:
            raise CapabilityError(f"{key} 必须非空且不超过 {maximum} 字符")
    scope = doc.get("scope", "shopping")
    if not isinstance(scope, str) or not re.fullmatch(r"shopping(?::[a-z0-9_-]{1,40})?", scope):
        raise CapabilityError("scope 仅支持 shopping 或 shopping:品类标识，不允许买家身份或全局权限范围")
    doc["scope"] = scope
    tools = doc.get("allowed_tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools) or not set(tools) <= SKILL_TOOL_ALLOWLIST:
        raise CapabilityError("allowed_tools 超出编译时只读工具白名单")
    if doc["kind"] == "strategy" and tools:
        raise CapabilityError("策略不声明工具权限")
    doc["allowed_tools"] = sorted(set(tools))
    evidence = doc.get("evidence", [])
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
        raise CapabilityError("必须提供 1 至 12 项可核查证据")
    for entry in evidence:
        if not isinstance(entry, dict) or set(entry) != {"reference", "summary"} or any(not isinstance(entry[k], str) or not entry[k].strip() or len(entry[k]) > 1000 for k in entry):
            raise CapabilityError("证据需包含非空 reference 和 summary；不会自动抓取或执行引用")
    expires = doc.get("expires_at")
    if doc["kind"] == "strategy" and not expires:
        raise CapabilityError("策略必须设置 expires_at，不能成为永久未复核的经验规则")
    if expires:
        doc["expires_at"] = parse_time(expires).isoformat()
    else:
        doc["expires_at"] = None
    keywords = doc.get("keywords", [])
    if not isinstance(keywords, list) or len(keywords) > 12 or any(not isinstance(k, str) or not k.strip() or len(k) > 40 for k in keywords):
        raise CapabilityError("keywords 必须是不超过 12 个简短关键词的列表")
    if doc["kind"] == "strategy" and not keywords:
        raise CapabilityError("策略需要明确检索关键词")
    doc["keywords"] = list(dict.fromkeys(k.strip().casefold() for k in keywords))
    return doc


class CapabilityRegistry:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS capabilities (
              kind TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
              reviewer TEXT, review_evidence TEXT, publisher TEXT, published_at TEXT, revoked_reason TEXT,
              PRIMARY KEY(kind,id,version))""")
            db.execute("""CREATE TABLE IF NOT EXISTS capability_audit (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id TEXT NOT NULL,
              version TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, evidence TEXT NOT NULL,
              content_hash TEXT NOT NULL, timestamp TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS capability_sessions (
              session_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS capability_session_policies (
              session_id TEXT PRIMARY KEY REFERENCES capability_sessions(session_id),
              policy_digest TEXT NOT NULL)""")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _actor(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise CapabilityError("审核操作必须提供明确的本机操作员标识")
        return value.strip()

    @staticmethod
    def _proof(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise CapabilityError("审核、发布、撤销必须提供非空证据或理由")
        return value.strip()

    def _row(self, db, kind, identifier, version):
        row = db.execute("SELECT * FROM capabilities WHERE kind=? AND id=? AND version=?", (kind, identifier, version)).fetchone()
        if row is None:
            raise CapabilityError("指定能力版本不存在")
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["content_hash"]:
            raise CapabilityError("能力内容 hash 不匹配，拒绝读取被改动的版本")
        validate(json.loads(row["payload"]))
        return row

    @staticmethod
    def _metadata(row):
        doc = json.loads(row["payload"])
        return {**{k: doc[k] for k in ("kind", "id", "version", "title", "description", "scope", "allowed_tools", "expires_at")},
                "content_hash": row["content_hash"], "state": row["state"], "reviewer": row["reviewer"]}

    def _audit(self, db, row, action, actor, evidence):
        db.execute("INSERT INTO capability_audit(kind,id,version,action,actor,evidence,content_hash,timestamp) VALUES(?,?,?,?,?,?,?,?)",
                   (row["kind"], row["id"], row["version"], action, actor, evidence, row["content_hash"], now().isoformat()))

    def import_draft(self, document, *, author):
        author = self._actor(author)
        doc = validate(document)
        raw = canonical(doc)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        identity = (doc["kind"], doc["id"], doc["version"])
        with self._transaction() as db:
            previous = db.execute("SELECT content_hash FROM capabilities WHERE kind=? AND id=? AND version=?", identity).fetchone()
            if previous and previous["content_hash"] != digest:
                raise CapabilityError("相同 ID/version 的内容不可覆盖；请创建新版本")
            if not previous:
                db.execute("INSERT INTO capabilities(kind,id,version,content_hash,payload,state,author,created_at) VALUES(?,?,?,?,?,'draft',?,?)",
                           (*identity, digest, raw, author, now().isoformat()))
            row = self._row(db, *identity)
            if not previous:
                self._audit(db, row, "import", author, "导入草稿，未生效")
            return self._metadata(row)

    def review(self, kind, identifier, version, *, reviewer, evidence):
        reviewer, evidence = self._actor(reviewer), self._proof(evidence)
        with self._transaction() as db:
            row = self._row(db, kind, identifier, version)
            if row["state"] != "draft":
                raise CapabilityError("只有草稿可以审核；已发布或撤销版本不能重新审核覆盖")
            db.execute("UPDATE capabilities SET state='reviewed',reviewer=?,review_evidence=? WHERE kind=? AND id=? AND version=?", (reviewer, evidence, kind, identifier, version))
            self._audit(db, row, "review", reviewer, evidence)
            return self._metadata(self._row(db, kind, identifier, version))

    def publish(self, kind, identifier, version, *, publisher, evidence):
        publisher, evidence = self._actor(publisher), self._proof(evidence)
        with self._transaction() as db:
            row = self._row(db, kind, identifier, version)
            if row["state"] != "reviewed" or not row["reviewer"] or not row["review_evidence"]:
                raise CapabilityError("发布前必须先审核；撤销版本不能重新发布")
            doc = json.loads(row["payload"])
            if doc["expires_at"] and parse_time(doc["expires_at"]) <= now():
                raise CapabilityError("该能力已经过期，不能发布")
            db.execute("UPDATE capabilities SET state='published',publisher=?,published_at=? WHERE kind=? AND id=? AND version=?", (publisher, now().isoformat(), kind, identifier, version))
            self._audit(db, row, "publish", publisher, evidence)
            return self._metadata(self._row(db, kind, identifier, version))

    def publish_learned(self, document, *, base, actor, evidence):
        """学习服务完成审核后发布；基准 CAS 与注册表写入处于同一事务。"""
        doc = validate(document)
        actor, evidence = self._actor(actor), self._proof(evidence)
        if doc['kind'] != 'strategy' or not doc['id'].startswith('learned-'):
            raise CapabilityError('自动维护只允许 learned- 命名空间的系统策略')
        raw = canonical(doc)
        content_hash = hashlib.sha256(raw.encode()).hexdigest()
        with self._transaction() as db:
            existing = db.execute('SELECT * FROM capabilities WHERE kind=? AND id=? AND version=?', ('strategy', doc['id'], doc['version'])).fetchone()
            if existing:
                row = self._row(db, 'strategy', doc['id'], doc['version'])
                if row['content_hash'] == content_hash and row['state'] == 'published':
                    return self._metadata(row)
                raise CapabilityError('目标版本已存在，不能覆盖或重新发布已撤销内容')
            active = db.execute("SELECT * FROM capabilities WHERE kind='strategy' AND id=? AND state='published' ORDER BY published_at DESC LIMIT 1", (doc['id'],)).fetchone()
            if base:
                if not active or any(active[key] != base[key] for key in ('id', 'version', 'content_hash')):
                    raise CapabilityError('策略基准版本已改变，请重新提炼和验证')
                self._row(db, 'strategy', base['id'], base['version'])
            elif active:
                raise CapabilityError('新增策略 ID 已占用，必须显式指定基准版本')
            if parse_time(doc['expires_at']) <= now():
                raise CapabilityError('策略已过期')
            stamp = now().isoformat()
            db.execute("INSERT INTO capabilities(kind,id,version,content_hash,payload,state,author,created_at,reviewer,review_evidence,publisher,published_at) VALUES(?,?,?,?,?,'published',?,?,?,?,?,?)",
                       ('strategy', doc['id'], doc['version'], content_hash, raw, actor, stamp, actor, evidence, actor, stamp))
            row = self._row(db, 'strategy', doc['id'], doc['version'])
            self._audit(db, row, 'publish_learned', actor, evidence)
            return self._metadata(row)

    def revoke(self, kind, identifier, version, *, actor, reason):
        actor, reason = self._actor(actor), self._proof(reason)
        with self._transaction() as db:
            row = self._row(db, kind, identifier, version)
            if row["state"] == "revoked":
                return self._metadata(row)
            db.execute("UPDATE capabilities SET state='revoked',revoked_reason=? WHERE kind=? AND id=? AND version=?", (reason, kind, identifier, version))
            self._audit(db, row, "revoke", actor, reason)
            return self._metadata(self._row(db, kind, identifier, version))

    def _active(self, row):
        doc = json.loads(row["payload"])
        return row["state"] == "published" and (not doc["expires_at"] or parse_time(doc["expires_at"]) > now())

    def metadata(self, *, available_tools=None, expected_digest=None):
        """常驻摘要只有 Skill 元数据；同 ID 只展示最新发布版本，旧版本仍可显式读取。"""
        with self._transaction() as db:
            self._check_digest(db, expected_digest)
            rows = db.execute("SELECT * FROM capabilities WHERE kind='skill' AND state='published' ORDER BY published_at DESC,id,version").fetchall()
            result, seen = [], set()
            for unverified in rows:
                row = self._row(db, unverified["kind"], unverified["id"], unverified["version"])
                item = self._metadata(row)
                if row["id"] in seen or not self._active(row) or (available_tools is not None and not set(item["allowed_tools"]) <= set(available_tools)):
                    continue
                seen.add(row["id"])
                result.append(item)
            return result

    def load_skill(self, skill_id, version, *, available_tools, expected_digest=None, require_current=False):
        with self._transaction() as db:
            self._check_digest(db, expected_digest)
            row = self._row(db, "skill", skill_id, version)
            if not self._active(row):
                raise CapabilityError("Skill 未发布、已过期或已撤销")
            if require_current:
                candidates = db.execute("""SELECT * FROM capabilities WHERE kind='skill' AND id=?
                    AND state='published' ORDER BY published_at DESC,id,version""", (skill_id,)).fetchall()
                latest = next((r for r in candidates if self._active(r)
                               and set(json.loads(r["payload"])["allowed_tools"]) <= set(available_tools)), None)
                if latest is None or latest["version"] != version:
                    raise CapabilityVersionChanged("Skill 已更新，请按当前目录选择明确版本")
            doc = json.loads(row["payload"])
            if not set(doc["allowed_tools"]) <= set(available_tools):
                raise CapabilityError("Skill 依赖的工具在当前会话不可用；不会动态注册或提升权限")
            return {**doc, "content_hash": row["content_hash"], "reviewer": row["reviewer"], "authority": "reference_only"}

    def lookup_strategies(self, query, scope="shopping", *, limit=3, expected_digest=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise CapabilityError("策略检索需要非空且不超过 2000 字符的选购问题")
        if not isinstance(scope, str) or not re.fullmatch(r"shopping(?::[a-z0-9_-]{1,40})?", scope):
            raise CapabilityError("策略 scope 无效")
        with self._transaction() as db:
            self._check_digest(db, expected_digest)
            rows = db.execute("SELECT * FROM capabilities WHERE kind='strategy' AND state='published' ORDER BY published_at DESC,id,version").fetchall()
            hits, seen = [], set()
            for unverified in rows:
                row = self._row(db, "strategy", unverified["id"], unverified["version"])
                doc = json.loads(row["payload"])
                if row["id"] in seen or not self._active(row):
                    continue
                seen.add(row["id"])
                if doc["scope"] not in {"shopping", scope}:
                    continue
                score = sum(keyword in query.casefold() for keyword in doc["keywords"])
                if score:
                    hits.append((score, {**doc, "content_hash": row["content_hash"], "reviewer": row["reviewer"], "authority": "advisory_only"}))
            hits.sort(key=lambda hit: hit[0], reverse=True)
            return [doc for _, doc in hits[:max(0, min(limit, 5))]]

    def inspect(self, *, kind=None):
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM capabilities WHERE (? IS NULL OR kind=?) ORDER BY kind,id,created_at", (kind, kind)).fetchall()
            return [self._metadata(self._row(db, row["kind"], row["id"], row["version"])) for row in rows]

    def show(self, kind, identifier, version):
        with self._transaction() as db:
            row = self._row(db, kind, identifier, version)
            return {**json.loads(row["payload"]), **self._metadata(row), "review_evidence": row["review_evidence"],
                    "author": row["author"], "revoked_reason": row["revoked_reason"]}

    def version_fingerprint(self):
        """缓存/编排器可绑定当前生效集合；撤销和时间过期都会使旧答案失配。"""
        with self._transaction() as db:
            return self._fingerprint(db)

    def _fingerprint(self, db):
        active = []
        for item in db.execute("SELECT * FROM capabilities WHERE state='published' ORDER BY kind,id,version").fetchall():
            row = self._row(db, item["kind"], item["id"], item["version"])
            if self._active(row):
                active.append((row["kind"], row["id"], row["version"], row["content_hash"]))
        return hashlib.sha256(canonical(active).encode()).hexdigest()

    def _check_digest(self, db, expected_digest):
        # 与正文/元数据读取共用 BEGIN IMMEDIATE，不能在绑定事务之后另读新发布内容。
        if expected_digest is not None and self._fingerprint(db) != expected_digest:
            raise CapabilityVersionChanged("本轮绑定的 Skill 或审核策略版本已变化；不能混用新资料与旧版本记录，请新建选购会话")

    def _policy_fingerprint(self, db):
        """普通 Skill 发布可热更新；策略变化、公共撤销/过期仍阻断旧会话。"""
        items = []
        for item in db.execute("SELECT * FROM capabilities ORDER BY kind,id,version").fetchall():
            row = self._row(db, item["kind"], item["id"], item["version"])
            if (row["kind"] == "strategy" and self._active(row)) or (
                row["kind"] == "skill" and (row["state"] == "revoked" or
                (row["state"] == "published" and not self._active(row)))
            ):
                items.append((row["kind"], row["id"], row["version"], row["state"], row["content_hash"]))
        return hashlib.sha256(canonical(items).encode()).hexdigest()

    def bind_session(self, session_id, buyer_id, *, allow_skill_updates=False):
        """普通轮次可刷新资料；工具读取仍固定本轮 digest，强撤销与策略变更不放行。"""
        if not session_id or not buyer_id:
            raise CapabilityError("能力版本绑定需要可信会话和买家")
        with self._transaction() as db:
            digest = self._fingerprint(db)
            previous = db.execute("SELECT * FROM capability_sessions WHERE session_id=?", (session_id,)).fetchone()
            if previous is not None and previous["buyer_id"] != buyer_id:
                raise CapabilityError("能力版本绑定不属于当前买家")
            if previous is not None and previous["digest"] != digest:
                policy = db.execute("SELECT policy_digest FROM capability_session_policies WHERE session_id=?",
                                    (session_id,)).fetchone()
                if not allow_skill_updates or policy is None or policy["policy_digest"] != self._policy_fingerprint(db):
                    raise CapabilityVersionChanged("本会话参考的 Skill 或审核策略已发布新版本、过期或撤销；为避免沿用历史正文，请新建选购会话")
                db.execute("UPDATE capability_sessions SET digest=? WHERE session_id=?", (digest, session_id))
            db.execute("INSERT OR IGNORE INTO capability_sessions VALUES(?,?,?,?)", (session_id, buyer_id, digest, now().isoformat()))
            if allow_skill_updates:
                policy_digest = self._policy_fingerprint(db)
                old = db.execute("SELECT policy_digest FROM capability_session_policies WHERE session_id=?", (session_id,)).fetchone()
                if old is not None and old["policy_digest"] != policy_digest:
                    raise CapabilityVersionChanged("审核策略或公共撤销记录已变化，请新建选购会话")
                db.execute("INSERT OR IGNORE INTO capability_session_policies VALUES(?,?)", (session_id, policy_digest))
            return digest

    def audit(self):
        with self._transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM capability_audit ORDER BY sequence")]
