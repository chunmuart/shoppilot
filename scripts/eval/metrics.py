# -*- coding: utf-8 -*-
"""recall metrics —— 召回评测的三个核心指标 + 聚合 + 发版门禁

指标口径与 13-1 章一致：

    Recall@K   Top-K 覆盖了多少标注项      —— 任何召回环节的底线
    Precision@K Top-K 中有多少是真正相关项 —— 防止塞满无关候选
    MRR        首条命中的倒数排名          —— Top-1 直接影响主 Agent 精挑
    NDCG@K     考虑位置 + 标注序的 gain    —— 不只看命中，还看好的是否靠前

三者都只依赖「召回出来的 id 序列」与「标注的 id 序列」，因此纯函数、零依赖、可单测，
商品检索与品类知识库两条链路共用同一套实现。

标注序即重要性序：`relevant[0]` 最相关。NDCG 用线性 gain（`len(relevant) - i`），
比二元相关更能区分「命中了但排在最后」和「命中且排第一」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


def _dedup_keep_order(items: Iterable[str]) -> list[str]:
    """去重但保序。

    召回结果理论上不该有重复 id，但真实链路里（多路合并、降级重试）可能出现；
    不去重会让 Recall 虚高、MRR 失真，故在指标入口统一清洗。
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Top-K 召回里覆盖了多少标注。

    注意：标注数大于 K 时，本指标天然取不到 1.0（这是 Recall@K 的定义，不是 bug）。
    因此选 K 时应让 K >= 单条 query 的常见标注数。
    """
    if k <= 0:
        return 0.0
    rel = set(relevant)
    if not rel:
        return 0.0
    top_k = set(_dedup_keep_order(retrieved)[:k])
    return len(top_k & rel) / len(rel)


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Top-K 已返回结果中的相关项占比。

    分母使用实际返回条数：检索链路因硬约束只返回一条且该条相关时，不应被
    人为补齐到 K 个空位而罚分。
    """
    if k <= 0 or not relevant:
        return 0.0
    top_k = _dedup_keep_order(retrieved)[:k]
    if not top_k:
        return 0.0
    return len(set(top_k) & set(relevant)) / len(top_k)


def mrr(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    """首条相关项的倒数排名；一条都没命中记 0。"""
    rel = set(relevant)
    if not rel:
        return 0.0
    for index, item in enumerate(_dedup_keep_order(retrieved), start=1):
        if item in rel:
            return 1.0 / index
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """NDCG@K：按标注序给线性 gain，按位置打折。"""
    if k <= 0 or not relevant:
        return 0.0
    # 标注序越靠前 gain 越大：第 0 位得 len(relevant)，末位得 1
    gain = {item: len(relevant) - i for i, item in enumerate(relevant)}
    ranked = _dedup_keep_order(retrieved)[:k]
    dcg = sum(gain.get(item, 0) / math.log2(i + 2) for i, item in enumerate(ranked))
    ideal = sum(
        gain[item] / math.log2(i + 2) for i, item in enumerate(list(relevant)[:k])
    )
    return dcg / ideal if ideal else 0.0


@dataclass
class QueryResult:
    """单条 query 的评测结果。"""

    query: str
    retrieved: list[str]
    relevant: list[str]
    recall: float
    mrr: float
    ndcg: float
    precision: float = 0.0
    # 硬约束过滤是否正确：None = 该 query 未声明约束，不参与统计
    filter_ok: bool | None = None
    note: str = ""
    # query 类型（lexical / semantic），用于拆分统计
    kind: str = "lexical"
    # 原始候选中跨平台同款的占比；指标按 canonical 去重后计算，但该值保留暴露多样性问题。
    canonical_duplicate_rate: float | None = None
    # 正式评测必须能按场景与业务约束拆分；缺失的维度由运行器显式填为 ALL。
    dimensions: dict[str, str] = field(default_factory=dict)


@dataclass
class Aggregate:
    """整个标注集的平均指标。"""

    k: int
    count: int
    recall: float
    mrr: float
    ndcg: float
    precision: float = 0.0
    filter_accuracy: float | None = None
    # 无结果/不可回答类不应被当作“空 relevant 的正例”混进 Recall，单独统计。
    empty_count: int = 0
    empty_accuracy: float | None = None
    # 政策题不能把无来源/过期材料包装成确定事实，单独统计拒答证据。
    policy_count: int = 0
    policy_rejection_accuracy: float | None = None
    canonical_duplicate_rate: float | None = None
    # 本轮实际走到的召回策略；正式主链不能把静默降级当作向量+精排的成绩。
    recall_strategies: frozenset[str] = field(default_factory=frozenset)
    per_query: list[QueryResult] = field(default_factory=list)


def evaluate(
    results: Sequence[QueryResult],
    k: int,
    empty_results: Sequence[bool] = (),
    policy_results: Sequence[bool] = (),
    recall_strategies: Sequence[str] = (),
) -> Aggregate:
    """把逐条结果聚合成平均指标（宏平均：每条 query 等权）。"""
    empty_count = len(empty_results)
    empty_accuracy = round(sum(empty_results) / empty_count, 4) if empty_count else None
    policy_count = len(policy_results)
    policy_rejection_accuracy = round(sum(policy_results) / policy_count, 4) if policy_count else None
    if not results:
        return Aggregate(
            k=k, count=0, recall=0.0, mrr=0.0, ndcg=0.0, precision=0.0,
            empty_count=empty_count, empty_accuracy=empty_accuracy,
            policy_count=policy_count, policy_rejection_accuracy=policy_rejection_accuracy,
            recall_strategies=frozenset(recall_strategies), per_query=[],
        )

    count = len(results)
    checked = [r for r in results if r.filter_ok is not None]
    duplicate_rates = [r.canonical_duplicate_rate for r in results if r.canonical_duplicate_rate is not None]
    filter_accuracy = (
        sum(1 for r in checked if r.filter_ok) / len(checked) if checked else None
    )
    return Aggregate(
        k=k,
        count=count,
        recall=round(sum(r.recall for r in results) / count, 4),
        mrr=round(sum(r.mrr for r in results) / count, 4),
        ndcg=round(sum(r.ndcg for r in results) / count, 4),
        precision=round(sum(r.precision for r in results) / count, 4),
        filter_accuracy=None if filter_accuracy is None else round(filter_accuracy, 4),
        empty_count=empty_count,
        empty_accuracy=empty_accuracy,
        policy_count=policy_count,
        policy_rejection_accuracy=policy_rejection_accuracy,
        canonical_duplicate_rate=(
            None if not duplicate_rates else round(sum(duplicate_rates) / len(duplicate_rates), 4)
        ),
        recall_strategies=frozenset(recall_strategies),
        per_query=list(results),
    )


@dataclass(frozen=True)
class Thresholds:
    """发版门禁阈值。

    召回、精度与排序任一退化都判 BLOCK。电商检索里“召回到了但排得很后”或
    “候选塞满无关商品”都会直接伤害买家体验，不能只做告警。
    """

    recall: float = 0.75
    # 当金标尚未穷举时，Precision@K 只能作为观察指标，不能设置不可达的硬门槛。
    precision: float | None = 0.45
    mrr: float = 0.65
    ndcg: float = 0.70
    empty_accuracy: float | None = None
    filter_accuracy: float | None = None
    policy_rejection_accuracy: float | None = None
    required_recall_strategies: frozenset[str] | set[str] | None = None


def gate(
    agg: Aggregate,
    thresholds: Thresholds,
    baseline: Mapping[str, float] | None = None,
    max_baseline_drop: float = 0.02,
) -> tuple[str, list[str]]:
    """返回 (verdict, 原因列表)；verdict ∈ PASS / WARN / BLOCK。"""
    blocks: list[str] = []
    if agg.count == 0:
        return "BLOCK", ["标注集为空，无法评测"]

    if agg.recall < thresholds.recall:
        blocks.append(f"Recall@{agg.k} {agg.recall} < {thresholds.recall}")
    if thresholds.precision is not None and agg.precision < thresholds.precision:
        blocks.append(f"Precision@{agg.k} {agg.precision} < {thresholds.precision}")
    if agg.mrr < thresholds.mrr:
        blocks.append(f"MRR {agg.mrr} < {thresholds.mrr}")
    if agg.ndcg < thresholds.ndcg:
        blocks.append(f"NDCG@{agg.k} {agg.ndcg} < {thresholds.ndcg}")
    if thresholds.empty_accuracy is not None:
        if agg.empty_accuracy is None:
            blocks.append("无结果准确率未统计")
        elif agg.empty_accuracy < thresholds.empty_accuracy:
            blocks.append(f"无结果准确率 {agg.empty_accuracy} < {thresholds.empty_accuracy}")
    if thresholds.filter_accuracy is not None:
        if agg.filter_accuracy is None:
            blocks.append("硬约束准确率未统计")
        elif agg.filter_accuracy < thresholds.filter_accuracy:
            blocks.append(f"硬约束准确率 {agg.filter_accuracy} < {thresholds.filter_accuracy}")
    if thresholds.policy_rejection_accuracy is not None:
        if agg.policy_rejection_accuracy is None:
            blocks.append("政策拒答准确率未统计")
        elif agg.policy_rejection_accuracy < thresholds.policy_rejection_accuracy:
            blocks.append(
                f"政策拒答准确率 {agg.policy_rejection_accuracy} < {thresholds.policy_rejection_accuracy}",
            )
    if thresholds.required_recall_strategies is not None:
        required_strategies = set(thresholds.required_recall_strategies)
        actual_strategies = set(agg.recall_strategies)
        if actual_strategies != required_strategies:
            blocks.append(
                f"实际召回策略 {sorted(actual_strategies)} != 要求 {sorted(required_strategies)}",
            )

    if baseline:
        current = {
            "recall": agg.recall,
            "precision": agg.precision,
            "mrr": agg.mrr,
            "ndcg": agg.ndcg,
        }
        for metric, approved in baseline.items():
            if metric not in current:
                blocks.append(f"批准基线包含未知指标：{metric}")
                continue
            if current[metric] < float(approved) - max_baseline_drop:
                blocks.append(
                    f"{metric} 相比批准基线下降 {float(approved) - current[metric]:.3f} > {max_baseline_drop:.3f}",
                )

    if blocks:
        return "BLOCK", blocks
    return "PASS", []
