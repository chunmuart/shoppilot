"""人工审核后的显式发布；候选库锁覆盖注册表 CAS，崩溃重试不重复版本。"""
from __future__ import annotations

import json
import time

from app.domain.learning import canonical, digest


def publish_candidate(store, registry, identity, *, expected_hash, actor, expires_at):
    with store.transaction() as db:
        row = store._decode(db.execute("SELECT * FROM strategy_candidates WHERE id=?", (identity,)).fetchone())
        if row["status"] not in {"approved", "published"} or row["reviewed_hash"] != expected_hash or row["content_hash"] != expected_hash:
            raise ValueError("发布仅接受已审核的精确候选版本")
        draft, base = row["document"], row["base"]
        if digest({"draft": draft, "base": base}) != expected_hash:
            raise ValueError("候选内容哈希校验失败")
        body = ("适用条件："+draft["applies_when"]+"\n步骤：\n"+"\n".join(f"{i+1}. {step}" for i, step in enumerate(draft["steps"]))
                +"\n例外与不适用：\n"+"\n".join(draft["exceptions"]))
        doc = {"kind": "strategy", "id": base.get("id") or "learned-"+identity[:24],
               "version": "v-"+expected_hash[:16], "title": draft["title"], "description": draft["applies_when"][:400],
               "body": body, "scope": draft["scope"], "allowed_tools": [], "keywords": draft["keywords"],
               "evidence": [{"reference": "learning-candidate:"+identity, "summary": "结构化来源与正反例验证保存在本机候选库；发布前经人工审核"}],
               "expires_at": expires_at}
        # 固定发布载荷。崩溃后换到期时间会改变哈希，应拒绝而不是覆盖已发布版本。
        proof = canonical({"candidate": identity, "hash": expected_hash, "reviewer": row["reviewer"], "validation_hash": digest(row["validation"])})
        published = registry.publish_learned(doc, base=base, actor=actor, evidence=proof)
        db.execute("UPDATE strategy_candidates SET status='published',updated_at=? WHERE id=?", (time.time(), identity))
        return published


def withdraw_candidate(store, registry, identity, *, actor, reason):
    if not actor.strip() or not reason.strip():
        raise ValueError("撤回需要操作员与原因")
    with store.transaction() as db:
        row = store._decode(db.execute("SELECT * FROM strategy_candidates WHERE id=?", (identity,)).fetchone())
        if row["status"] == "published":
            registry.revoke("strategy", row["base"].get("id") or "learned-"+identity[:24], "v-"+row["content_hash"][:16], actor=actor, reason=reason)
        db.execute("UPDATE strategy_candidates SET status='withdrawn',review_note=?,updated_at=? WHERE id=?", (canonical({"actor": actor, "reason": reason}), time.time(), identity))
