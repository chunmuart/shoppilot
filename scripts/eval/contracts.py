# -*- coding: utf-8 -*-
"""评测输入输出契约。

评测器不能把 Judge 的缺字段、错类型或漏判悄悄当成通过；否则报告数字会比
真实质量更乐观。本模块只做确定性校验，主观表达质量仍由 Judge 自身判断。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any


class JudgeOutputError(ValueError):
    """Judge 返回不满足评测协议。"""


_LEVELS = ("p0", "p1", "p2")
_EXPLICIT_PASS = re.compile(r"pass\s*=\s*(true|false)", re.IGNORECASE)
_EXPLICIT_VERDICT = re.compile(r"结论\s*[：:]\s*(通过|不通过)")
_FINAL_VERDICT = re.compile(r"结论\s*[：:]\s*(通过|不通过)\s*$")


def _assert_reason_matches_pass(reason: str, passed: bool, level: str, criterion: str) -> None:
    """拒绝报告中最危险的一类自相矛盾：文字说通过但布尔值写成失败。"""
    explicit = _EXPLICIT_PASS.findall(reason)
    if explicit and (explicit[-1].lower() == "true") != passed:
        raise JudgeOutputError(f"{level}.{criterion} 的 reason 与 pass 冲突")
    verdicts = _EXPLICIT_VERDICT.findall(reason)
    final = _FINAL_VERDICT.search(reason)
    if final is None:
        raise JudgeOutputError(f"{level}.{criterion} 的 reason 缺少显式结论")
    if verdicts and (verdicts[-1] == "通过") != passed:
        raise JudgeOutputError(f"{level}.{criterion} 的 reason 与 pass 冲突")


def validate_judge_output(raw: Any, rubric: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    """校验并返回规范 Judge 输出。

    每个 rubric criterion 必须在同一等级中恰好出现一次；空等级只能在 rubric
    本身为空时返回空列表。任何协议问题都应导致评测 case 为 ERROR，而非满分。
    """
    if not isinstance(raw, dict):
        raise JudgeOutputError("Judge 输出必须是 JSON 对象")
    if set(raw) != set(_LEVELS):
        raise JudgeOutputError("Judge 输出必须且只能包含 p0/p1/p2")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for level in _LEVELS:
        expected = rubric.get(level, [])
        items = raw[level]
        if not isinstance(expected, list) or not all(isinstance(c, str) and c for c in expected):
            raise JudgeOutputError(f"rubric.{level} 必须是非空字符串列表")
        if not isinstance(items, list):
            raise JudgeOutputError(f"{level} 必须是列表")

        checked: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise JudgeOutputError(f"{level} 项必须是对象")
            if set(item) != {"criterion", "reason", "pass"}:
                raise JudgeOutputError(f"{level} 项字段必须为 criterion/reason/pass")
            criterion, reason, passed = item["criterion"], item["reason"], item["pass"]
            if not isinstance(criterion, str) or not criterion:
                raise JudgeOutputError(f"{level}.criterion 必须是非空字符串")
            if not isinstance(reason, str) or not reason.strip():
                raise JudgeOutputError(f"{level}.{criterion} 的 reason 不能为空")
            if type(passed) is not bool:
                raise JudgeOutputError(f"{level}.{criterion} 的 pass 必须是布尔值")
            _assert_reason_matches_pass(reason, passed, level, criterion)
            checked.append({"criterion": criterion, "reason": reason.strip(), "pass": passed})

        if Counter(item["criterion"] for item in checked) != Counter(expected):
            raise JudgeOutputError(f"{level} 的 criterion 必须与 rubric 一一对应")
        normalized[level] = checked
    return normalized
