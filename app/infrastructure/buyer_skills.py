"""买家个人 Skill：独立所有权、不可变修订、乐观并发；不进入全局审核库。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


class BuyerSkillConflict(ValueError):
    pass


class BuyerSkillStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS buyer_skill_versions (
                buyer_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                payload TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(buyer_id,id,version))""")
            db.execute("""CREATE TABLE IF NOT EXISTS buyer_skill_heads (
                buyer_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                active INTEGER NOT NULL, PRIMARY KEY(buyer_id,id))""")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row, *, body=False):
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["content_hash"]:
            raise ValueError("个人 Skill 内容校验失败")
        doc = json.loads(row["payload"])
        if not body:
            doc.pop("body")
        return {**doc, "content_hash": row["content_hash"], "updated_at": row["created_at"]}

    def list(self, buyer_id, *, body=False):
        with self.transaction() as db:
            rows = db.execute("""SELECT v.* FROM buyer_skill_heads h
                JOIN buyer_skill_versions v USING(buyer_id,id,version)
                WHERE h.buyer_id=? AND h.active=1 ORDER BY v.created_at DESC,h.id""", (buyer_id,)).fetchall()
            return [self.decode(row, body=body) for row in rows]

    def save(self, buyer_id, title, description, body, *, skill_id=None, expected_version=None):
        for value, maximum in ((title,120),(description,400),(body,12000)):
            if not isinstance(value,str) or not value.strip() or len(value)>maximum:
                raise ValueError("名称、用途和正文不能为空或超出长度限制")
        with self.transaction() as db:
            version = 1
            if skill_id:
                head = db.execute("SELECT * FROM buyer_skill_heads WHERE buyer_id=? AND id=? AND active=1",
                                  (buyer_id,skill_id)).fetchone()
                if head is None:
                    raise LookupError("个人 Skill 不存在")
                if str(head["version"]) != expected_version:
                    raise BuyerSkillConflict("Skill 已被更新，请刷新后再编辑")
                previous = db.execute("""SELECT * FROM buyer_skill_versions
                    WHERE buyer_id=? AND id=? AND version=?""",
                    (buyer_id, skill_id, head["version"])).fetchone()
                current = self.decode(previous, body=True)
                # CAS 先检查；无内容变化的保存不制造新版本和无意义的目录更新。
                if (current["title"], current["description"], current["body"]) == (
                    title.strip(), description.strip(), body.strip()
                ):
                    return current
                version = head["version"] + 1
            else:
                count = db.execute("SELECT count(*) FROM buyer_skill_heads WHERE buyer_id=? AND active=1", (buyer_id,)).fetchone()[0]
                if count >= 50:
                    raise ValueError("最多保存 50 个个人 Skill")
                skill_id = "personal-" + uuid.uuid4().hex
            doc = {"kind":"skill","id":skill_id,"version":str(version),"title":title.strip(),
                   "description":description.strip(),"body":body.strip(),"scope":"shopping",
                   "allowed_tools":[],"evidence":[],"expires_at":None,"authority":"reference_only","source":"buyer"}
            raw=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(",",":"))
            digest=hashlib.sha256(raw.encode()).hexdigest()
            created=datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO buyer_skill_versions VALUES(?,?,?,?,?,?)",(buyer_id,skill_id,version,raw,digest,created))
            db.execute("""INSERT INTO buyer_skill_heads VALUES(?,?,?,1)
                ON CONFLICT(buyer_id,id) DO UPDATE SET version=excluded.version,active=1""",(buyer_id,skill_id,version))
            return {**doc,"content_hash":digest,"updated_at":created}

    def delete(self, buyer_id, skill_id, expected_version):
        with self.transaction() as db:
            head=db.execute("SELECT * FROM buyer_skill_heads WHERE buyer_id=? AND id=? AND active=1",(buyer_id,skill_id)).fetchone()
            if head is None:
                raise LookupError("个人 Skill 不存在")
            if str(head["version"]) != expected_version:
                raise BuyerSkillConflict("Skill 已被更新，请刷新后再删除")
            db.execute("UPDATE buyer_skill_heads SET active=0 WHERE buyer_id=? AND id=?",(buyer_id,skill_id))

    def load(self, buyer_id, skill_id, version):
        with self.transaction() as db:
            row=db.execute("""SELECT v.* FROM buyer_skill_heads h JOIN buyer_skill_versions v USING(buyer_id,id,version)
                WHERE h.buyer_id=? AND h.id=? AND h.active=1 AND CAST(h.version AS TEXT)=?""",(buyer_id,skill_id,version)).fetchone()
            if row is None:
                raise LookupError("个人 Skill 已更新、删除或不属于当前买家，请刷新方案")
            return self.decode(row,body=True)
