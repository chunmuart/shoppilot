"""离线学习合同：候选不等于发布，失败轨迹不等于正确答案。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

PROCESSOR_VERSION = "learning-v1"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def input_fingerprint(value) -> str:
    # 同一需求的空白、大小写变体不能跨集合；语义近重复仍需人工审核。
    return digest(re.sub(r"\s+", "", canonical(value)).casefold())


class StrategyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=120)
    applies_when: str = Field(min_length=1, max_length=1200)
    steps: list[str] = Field(min_length=1, max_length=8)
    exceptions: list[str] = Field(min_length=1, max_length=8)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    keywords: list[str] = Field(min_length=1, max_length=12)
    scope: str = Field(default="shopping", pattern=r"^shopping(?::[a-z0-9_-]{1,40})?$")


class Reflection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["supported_success", "failure", "insufficient_evidence"]
    reason: str = Field(min_length=1, max_length=1000)
    strategy: StrategyDraft | None


class StrategyExtractor(Protocol):
    async def extract(self, evidence: dict) -> tuple[Reflection, list[dict]]: ...


def validate_strategy(draft: dict, evidence: dict) -> dict:
    result = StrategyDraft.model_validate(draft).model_dump()
    known = {item["ref"] for item in evidence.get("facts", [])}
    if not known or not set(result["evidence_refs"]) <= known:
        raise ValueError("策略引用必须来自本次冻结证据")
    prose = canonical({key: result[key] for key in ("title", "applies_when", "steps", "exceptions")})
    if any(len(item) > 1800 or not item.strip() for key in ("steps", "exceptions") for item in result[key]):
        raise ValueError("策略步骤/例外为空或过长")
    if any(len(word) > 40 or not word.strip() for word in result["keywords"]):
        raise ValueError("策略关键词为空或过长")
    # 业务数值只能出现在来源事实中。泛化策略不固化 SKU、价格、库存、买家身份。
    if re.search(r"\bP\d{3,}(?:-S\d+)?\b|\b(?:sk|pk)-[\w-]+|https?://|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", prose):
        raise ValueError("策略包含商品身份、凭据、网址或个人信息")
    if re.search(r"\d+(?:\.\d+)?\s*(?:元|美元|CNY|USD|件库存)", prose, re.I):
        raise ValueError("不得把历史报价或库存固化为通用策略")
    for key in ("buyer_id", "session_id"):
        if len(str(evidence.get(key, ""))) >= 4 and evidence[key] in prose:
            raise ValueError("策略不得包含买家或会话身份")
    return result


def validate_checks(checks: list[dict], *, require_both=False):
    if not checks or any(set(c) != {"case_id", "kind", "passed", "evidence_hash"}
                         or c["kind"] not in {"positive", "negative"}
                         or c["passed"] is not True or not c["case_id"]
                         or not re.fullmatch(r"[a-f0-9]{64}", str(c["evidence_hash"])) for c in checks):
        raise ValueError("验证必须包含明确场景、通过断言及证据哈希")
    if len({c["case_id"] for c in checks}) != len(checks):
        raise ValueError("验证场景重复")
    if require_both and {c["kind"] for c in checks} != {"positive", "negative"}:
        raise ValueError("策略需要适用场景与不适用场景的验证")
