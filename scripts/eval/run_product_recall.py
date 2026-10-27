# -*- coding: utf-8 -*-
"""商品检索（product_search）召回评测 —— 见教程 13-2 章。

直连 `CatalogSearchUseCase`，不过 HTTP、不过 Agent：召回评测的定位是模块级
「日常体检」，改一行权重、换一版 reranker 都该能几秒钟跑一遍，才可能常驻 CI。

用法（项目根目录执行）：

    # 默认档（有 embedding 凭据就走向量+精排，否则自动降级）
    uv run python scripts/eval/run_product_recall.py

    # 三档降级链对比：量化"降级到底损失多少召回质量"
    uv run python scripts/eval/run_product_recall.py --compare-strategies

    # 无凭据也能跑：纯关键词档，适合 CI
    uv run python scripts/eval/run_product_recall.py --strategy keyword_2gram

默认报告 K=8，与既有基线保持一致。候选窗口由 RECALL_CANDIDATES 控制，
支持权威目录 ID 过滤或渐进补召回，不能把候选窗口和最终展示 K 混为一谈。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.application.usecases.catalog_search import CatalogSearchUseCase  # noqa: E402
from app.domain.catalog.product_search_spec import ProductSearchSpec  # noqa: E402
from app.infrastructure.embedding.openai_embedding_client import (  # noqa: E402
    OpenAIEmbeddingClient,
)
from app.infrastructure.persistence.in_memory_repositories import (  # noqa: E402
    InMemoryProductRepository,
)
from app.infrastructure.rerank.factory import create_reranker  # noqa: E402
from app.infrastructure.settings import load_settings  # noqa: E402
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index  # noqa: E402
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex  # noqa: E402
from scripts.eval.metrics import (  # noqa: E402
    Aggregate,
    QueryResult,
    Thresholds,
    evaluate,
    gate,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from scripts.eval.hard_constraints import find_hit_constraint_violations  # noqa: E402
from scripts.eval.run_manifest import (  # noqa: E402
    SPLITS, build_manifest, finish_manifest, manifest_report, select_cases,
    validate_baseline_selection, write_manifest,
)

_DATASET = Path("eval/product_recall.jsonl")
_COMPARISON_STRATEGIES = ("embedding_rerank", "embedding_only", "keyword_2gram")
_STRATEGIES = (*_COMPARISON_STRATEGIES, "bm25", "hybrid_rerank")


def profile_thresholds(profile: str) -> Thresholds:
    """返回正式线上链路或离线降级链路的门禁口径。"""
    if profile == "online-main":
        return Thresholds(
            recall=0.90, precision=None, mrr=0.85, ndcg=0.85,
            empty_accuracy=1.0, filter_accuracy=1.0,
            required_recall_strategies=frozenset({"embedding_rerank"}),
        )
    if profile == "offline-fallback":
        # 关键词档用于离线退化监控；质量由批准基线约束，安全底线仍必须绝对通过。
        return Thresholds(
            recall=0.0, precision=None, mrr=0.0, ndcg=0.0,
            empty_accuracy=1.0, filter_accuracy=1.0,
            required_recall_strategies=frozenset({"keyword_2gram"}),
        )
    if profile == "hybrid-experimental":
        return Thresholds(
            recall=0.90, precision=None, mrr=0.85, ndcg=0.85,
            empty_accuracy=1.0, filter_accuracy=1.0,
            # 空候选无需执行精排；完整执行要求由逐条 hybrid_execution_errors 校验。
            required_recall_strategies=None,
        )
    raise ValueError(f"未知评测 profile：{profile}")


def load_dataset(path: Path) -> list[dict]:
    cases = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            cases.append(json.loads(raw))
    return cases


def load_baselines(path: Path | None) -> dict[str, dict[str, float]]:
    """读取经人工批准的各检索档位基线；未传文件时只执行绝对门槛。"""
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    strategies = raw.get("strategies", raw)
    if not isinstance(strategies, dict):
        raise ValueError("基线文件必须是 {strategy: {recall, precision, mrr, ndcg}} 对象")
    return {
        str(name): {metric: float(value) for metric, value in metrics.items()}
        for name, metrics in strategies.items()
        if isinstance(metrics, dict)
    }


async def build_usecase(strategy: str) -> tuple[CatalogSearchUseCase, InMemoryProductRepository, str]:
    """按目标档位装配 UseCase。

    降级档位不是靠开关切换的，而是**靠少注入依赖自然形成**——这正好复用了线上
    真实的降级逻辑（`execute()` 里 embedder/vector_index 为 None 就走关键词），
    评测因此测的是真链路，不是为评测另写的一套。
    """
    repo = InMemoryProductRepository()
    if strategy in {"keyword_2gram", "bm25"}:
        return CatalogSearchUseCase(repo, hybrid_enabled=strategy == "bm25"), repo, strategy

    settings = load_settings()
    embedder = OpenAIEmbeddingClient(settings)
    vector_index = QdrantProductIndex(settings)
    ok = await bootstrap_product_index(repo, embedder, vector_index)
    if not ok:
        print("  [warn] 向量建库失败，本档实际会降级到关键词召回")

    reranker = None
    if strategy in {"embedding_rerank", "hybrid_rerank"}:
        reranker = create_reranker(settings)
        if reranker is None:
            print("  [warn] 精排未配置，实际按召回排序")
    return (
        CatalogSearchUseCase(
            repo, embedder=embedder, vector_index=vector_index, reranker=reranker,
            hybrid_enabled=strategy == "hybrid_rerank",
            hybrid_lexical_weight=settings.hybrid_lexical_weight,
            hybrid_vector_weight=settings.hybrid_vector_weight,
            recall_candidates=settings.recall_candidates,
        ),
        repo,
        strategy,
    )


def check_filter(
    case: dict, hits: list[dict], filtered_out: list[dict], repo_products: dict[str, Any],
) -> tuple[Optional[bool], str]:
    """硬约束过滤是否正确。

    只对声明了约束的 query 判定；未声明的返回 None（不参与统计）。

    这里刻意**不重算汇率与关税**——那是 TariffSchedule 的职责，评测重算一遍等于
    把业务逻辑抄两份，抄错了还会误判。改为查两个不依赖换算的事实：
      1. 泄漏：返回结果里有不满足 ship_to 的商品（ships_to 是明确的枚举，无歧义）
      2. 误杀：标注为相关的商品出现在 filtered_out 里
    """
    ship_to = case.get("ship_to")
    price_cap = case.get("price_max_major")
    excluded_material_tags = case.get("excluded_material_tags", [])
    required_material_tags = case.get("required_material_tags", [])
    category = case.get("category")
    if not ship_to and price_cap is None and not excluded_material_tags and not required_material_tags and not category:
        return None, ""

    problems = []
    violations = find_hit_constraint_violations(
        hits,
        repo_products,
        price_max_major=price_cap,
        ship_to=ship_to,
        category=category,
        target_currency=case.get("target_currency", "CNY"),
        excluded_material_tags=excluded_material_tags,
        required_material_tags=required_material_tags,
    )
    for product_id, reasons in sorted(violations.items()):
        problems.append(f"泄漏 {product_id}（{','.join(sorted(reasons))}）")

    rejected_ids = {item["product_id"] for item in filtered_out}
    for pid in case["relevant"]:
        if pid in rejected_ids:
            problems.append(f"误杀 {pid}（标注为相关却被硬约束挡掉）")

    return (not problems), "；".join(problems)


async def run_dataset(
    usecase: CatalogSearchUseCase, repo: InMemoryProductRepository, cases: list[dict], top_k: int,
    *, observations: list[dict] | None = None,
) -> Aggregate:
    products = {p.product_id: p for p in await repo.list_all()}
    results: list[QueryResult] = []
    empty_results: list[bool] = []
    recall_strategies: set[str] = set()

    for case in cases:
        spec = ProductSearchSpec(
            normalized_query=case["query"],
            top_k=top_k,
            category=case.get("category"),
            price_max_major=case.get("price_max_major"),
            ship_to=case.get("ship_to"),
            target_currency=case.get("target_currency", "CNY"),
            excluded_material_tags=case.get("excluded_material_tags", []),
            required_material_tags=case.get("required_material_tags", []),
        )
        payload = await usecase.execute(spec)
        recall_strategies.add(str(payload.get("recall_strategy", "unknown")))
        hits = payload.get("hits", [])
        raw_retrieved = [hit["product_id"] for hit in hits]

        def canonical(product_id: str) -> str:
            product = products.get(product_id)
            return product.canonical_product_id if product and product.canonical_product_id else product_id

        retrieved = []
        for product_id in raw_retrieved:
            canonical_id = canonical(product_id)
            if canonical_id not in retrieved:
                retrieved.append(canonical_id)
        relevant = case.get("relevant_canonical_ids") or [canonical(product_id) for product_id in case["relevant"]]
        duplicate_rate = (
            (len(raw_retrieved) - len(retrieved)) / len(raw_retrieved)
            if raw_retrieved else 0.0
        )
        dimensions = {
            "category": str(case.get("category") or "ALL"),
            "target_currency": str(case.get("target_currency") or "CNY"),
            "ship_to": str(case.get("ship_to") or "ALL"),
            "split": str(case.get("split") or "ALL"),
        }
        observation = {
            "case_id": case.get("id"), "query": case["query"], "split": case.get("split"),
            "actual_strategy": str(payload.get("recall_strategy", "unknown")),
            "retrieval_variant": payload.get("retrieval_variant", "legacy_two_stage"),
            "rerank_applied": payload.get("rerank_applied"), "vector_available": payload.get("vector_available"),
            "raw_retrieved": raw_retrieved, "canonical_retrieved": retrieved, "relevant": relevant,
            "expected_empty": bool(case.get("expected_empty")), "empty_pass": not retrieved if case.get("expected_empty") else None,
        }
        if observations is not None:
            observations.append(observation)

        # 无结果题是负例，不能把空 relevant 的 Recall=0 混进正例均值；
        # 它的正确性是“没有返回任何候选”，单独以泄漏率门禁。
        if case.get("expected_empty"):
            empty_results.append(not retrieved)
            continue

        filter_ok, filter_note = check_filter(
            case, hits, payload.get("filtered_out", []), products,
        )
        observation.update(filter_ok=filter_ok, filter_note=filter_note)
        results.append(
            QueryResult(
                query=case["query"],
                retrieved=retrieved,
                relevant=relevant,
                recall=recall_at_k(retrieved, relevant, top_k),
                mrr=mrr(retrieved, relevant),
                ndcg=ndcg_at_k(retrieved, relevant, top_k),
                precision=precision_at_k(retrieved, relevant, top_k),
                filter_ok=filter_ok,
                note=filter_note,
                kind=case.get("kind", "lexical"),
                canonical_duplicate_rate=duplicate_rate,
                dimensions=dimensions,
            ),
        )
    return evaluate(
        results, k=top_k, empty_results=empty_results,
        recall_strategies=sorted(recall_strategies),
    )


def by_kind(agg: Aggregate) -> dict[str, Aggregate]:
    """按 query 类型拆开。

    拆开是必需的：字面类 query 上关键词召回本来就很强（语料描述关键词密集），
    混在一起算总均会把语义类的差距抹平，看不出向量召回到底买到了什么。
    """
    groups: dict[str, list[QueryResult]] = {}
    for r in agg.per_query:
        groups.setdefault(r.kind, []).append(r)
    return {kind: evaluate(rs, k=agg.k) for kind, rs in sorted(groups.items())}


def hybrid_execution_errors(observations: list[dict]) -> list[str]:
    errors = []
    for row in observations:
        if row.get("actual_strategy") not in {"hybrid_rerank", "hybrid_only"}:
            errors.append(f"{row.get('case_id') or row['query']}: 实际策略不是 Hybrid 向量链")
        if row.get("vector_available") is not True:
            errors.append(f"{row.get('case_id') or row['query']}: Hybrid 向量侧未证明健康")
        if row.get("raw_retrieved") and (row.get("rerank_applied") is not True or row.get("actual_strategy") != "hybrid_rerank"):
            errors.append(f"{row.get('case_id') or row['query']}: 非空候选未执行真实精排")
    return errors


def by_dimension(agg: Aggregate, dimension: str) -> dict[str, Aggregate]:
    """按正式集的业务维度分桶，避免总平均掩盖局部退化。"""
    groups: dict[str, list[QueryResult]] = {}
    for result in agg.per_query:
        value = result.dimensions.get(dimension, "ALL")
        groups.setdefault(value, []).append(result)
    return {value: evaluate(results, k=agg.k) for value, results in sorted(groups.items())}


def print_summary(label: str, agg: Aggregate) -> None:
    filt = "n/a" if agg.filter_accuracy is None else f"{agg.filter_accuracy:.3f}"
    empty = "n/a" if agg.empty_accuracy is None else f"{agg.empty_accuracy:.3f} ({agg.empty_count} 条)"
    duplicate = "n/a" if agg.canonical_duplicate_rate is None else f"{agg.canonical_duplicate_rate:.3f}"
    actual_strategy = ",".join(sorted(agg.recall_strategies)) or "unknown"
    print(
        f"  {label:<18} Recall@{agg.k}={agg.recall:.3f}  Precision@{agg.k}={agg.precision:.3f}  MRR={agg.mrr:.3f}  "
        f"NDCG@{agg.k}={agg.ndcg:.3f}  过滤准确率={filt}  无结果准确率={empty}  同款重复率={duplicate}  实际链路={actual_strategy}",
    )
    for kind, sub in by_kind(agg).items():
        print(
            f"    └─ {kind:<10}({sub.count:>2} 条) Recall={sub.recall:.3f}  Precision={sub.precision:.3f}  "
            f"MRR={sub.mrr:.3f}  NDCG={sub.ndcg:.3f}",
        )


def render_report(
    per_strategy: dict[str, Aggregate], thresholds: Thresholds, top_k: int,
    baselines: dict[str, dict[str, float]] | None = None,
    dataset: Path | str = _DATASET,
    profile: str = "custom",
) -> str:
    lines = [
        f"# 商品检索召回评测报告（{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）",
        "",
        f"标注集 `{dataset}`，profile={profile}，K={top_k}。",
        "",
        "## 指标总览",
        "",
        f"| 档位 | 实际召回链 | Recall@{top_k} | Precision@{top_k} | MRR | NDCG@{top_k} | 过滤准确率 | 无结果准确率 | 同款重复率 | 门禁 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, agg in per_strategy.items():
        verdict, _ = gate(agg, thresholds, (baselines or {}).get(name))
        filt = "n/a" if agg.filter_accuracy is None else f"{agg.filter_accuracy:.3f}"
        empty = "n/a" if agg.empty_accuracy is None else f"{agg.empty_accuracy:.3f}"
        duplicate = "n/a" if agg.canonical_duplicate_rate is None else f"{agg.canonical_duplicate_rate:.3f}"
        lines.append(
            f"| {name} | {','.join(sorted(agg.recall_strategies)) or 'unknown'} | {agg.recall:.3f} | {agg.precision:.3f} | {agg.mrr:.3f} | {agg.ndcg:.3f} | {filt} | {empty} | {duplicate} | {verdict} |",
        )

    precision_gate = "观察项（未穷举金标，不阻断）" if thresholds.precision is None else f"≥ {thresholds.precision}"
    lines += ["", f"门禁阈值：Recall ≥ {thresholds.recall}、Precision {precision_gate}、MRR ≥ {thresholds.mrr}"
              f"、NDCG ≥ {thresholds.ndcg}、过滤准确率 ≥ {thresholds.filter_accuracy}、无结果准确率 ≥ {thresholds.empty_accuracy}。", ""]

    lines += ["## 按 query 类型拆分", "",
              "字面类（lexical）query 与语料共享词汇，关键词召回天然占优；"
              "语义类（semantic）query 刻意不含商品字面，是向量召回真正创造价值的地方。", "",
              f"| 档位 | 类型 | 条数 | Recall@{top_k} | Precision@{top_k} | MRR | NDCG@{top_k} |",
              "|---|---|---|---|---|---|---|"]
    for name, agg in per_strategy.items():
        for kind, sub in by_kind(agg).items():
            lines.append(
                f"| {name} | {kind} | {sub.count} | {sub.recall:.3f} | {sub.precision:.3f} | "
                f"{sub.mrr:.3f} | {sub.ndcg:.3f} |",
            )
    lines.append("")

    lines += ["## 正式分桶监控", "",
              "以下各桶分别展示，供定位品类、币种、目的国和 dev/release 的局部退化；正式数据缺桶会在数据校验阶段直接报错。", "",
              f"| 档位 | 维度 | 取值 | 条数 | Recall@{top_k} | Precision@{top_k} | MRR | NDCG@{top_k} |", 
              "|---|---|---|---|---|---|---|---|"]
    for name, agg in per_strategy.items():
        for dimension in ("category", "target_currency", "ship_to", "split"):
            for value, sub in by_dimension(agg, dimension).items():
                lines.append(
                    f"| {name} | {dimension} | {value} | {sub.count} | {sub.recall:.3f} | "
                    f"{sub.precision:.3f} | {sub.mrr:.3f} | {sub.ndcg:.3f} |",
                )
    lines.append("")

    for name, agg in per_strategy.items():
        verdict, reasons = gate(agg, thresholds, (baselines or {}).get(name))
        lines += [f"## {name}（{verdict}，{agg.count} 条）", ""]
        if reasons:
            lines += ["未达标项：", *[f"- {r}" for r in reasons], ""]
        lines += ["| query | 类型 | Recall | Precision | MRR | NDCG | 召回序 | 标注 | 过滤 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in agg.per_query:
            filt = "-" if r.filter_ok is None else ("OK" if r.filter_ok else f"FAIL {r.note}")
            lines.append(
                f"| {r.query} | {r.kind} | {r.recall:.2f} | {r.precision:.2f} | {r.mrr:.2f} | {r.ndcg:.2f} | "
                f"{','.join(r.retrieved) or '（空）'} | {','.join(r.relevant)} | {filt} |",
            )
        lines.append("")
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="商品检索召回评测")
    parser.add_argument("--dataset", default=str(_DATASET))
    parser.add_argument("--split", choices=SPLITS, default="all", help="先选择输入集，再执行与门禁；默认 all 兼容旧命令")
    parser.add_argument("--dry-run", action="store_true", help="只校验选集并写 NOT_RUN 证据，不调用模型或向量服务")
    parser.add_argument("--top-k", type=int, default=8, help="默认 8：向量召回深度上限即 8")
    parser.add_argument("--strategy", choices=_STRATEGIES, default="embedding_rerank")
    parser.add_argument("--compare-strategies", action="store_true", help="三档降级链对比")
    parser.add_argument("--min-recall", type=float, default=0.75)
    parser.add_argument("--min-precision", type=float, default=0.45)
    parser.add_argument("--min-mrr", type=float, default=0.65)
    parser.add_argument("--min-ndcg", type=float, default=0.70)
    parser.add_argument("--min-empty-accuracy", type=float, default=None)
    parser.add_argument("--formal-gates", action="store_true", help="兼容旧命令，等同 --profile online-main")
    parser.add_argument("--profile", choices=("online-main", "offline-fallback", "hybrid-experimental"), default=None)
    parser.add_argument("--baseline-file", type=Path, default=None, help="批准基线 JSON；任一指标下降超过 2 个百分点即阻断")
    parser.add_argument("--report-dir", default="eval")
    args = parser.parse_args(argv)
    if args.top_k <= 0:
        parser.error("--top-k 必须为正整数")
    try:
        cases, selection = select_cases(load_dataset(Path(args.dataset)), args.split)
        validate_baseline_selection(args.baseline_file, selection, Path(args.dataset))
    except (ValueError, OSError) as err:
        parser.error(str(err))
    print(f"标注集 {args.dataset}：split={args.split}，{len(cases)} 条，K={args.top_k}")

    profile = args.profile
    if args.formal_gates:
        if profile and profile != "online-main":
            parser.error("--formal-gates 只能与 --profile online-main 一起使用")
        profile = "online-main"
    if profile == "online-main":
        if args.compare_strategies or args.strategy != "embedding_rerank":
            parser.error("online-main 只评 embedding_rerank；关键词降级请使用 --profile offline-fallback")
        thresholds = profile_thresholds(profile)
    elif profile == "offline-fallback":
        if args.compare_strategies or args.strategy != "keyword_2gram":
            parser.error("offline-fallback 只评 keyword_2gram")
        if args.baseline_file is None:
            parser.error("offline-fallback 必须提供 --baseline-file，防止无基线时假绿")
        thresholds = profile_thresholds(profile)
    elif profile == "hybrid-experimental":
        if args.compare_strategies or args.strategy != "hybrid_rerank":
            parser.error("hybrid-experimental 只评显式 --strategy hybrid_rerank，不替代 online-main")
        thresholds = profile_thresholds(profile)
    else:
        thresholds = Thresholds(
            recall=args.min_recall, precision=args.min_precision, mrr=args.min_mrr,
            ndcg=args.min_ndcg, empty_accuracy=args.min_empty_accuracy,
        )
    targets = list(_COMPARISON_STRATEGIES) if args.compare_strategies else [args.strategy]
    baselines = load_baselines(args.baseline_file)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"recall-{args.split}-report-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.md"
    manifest = build_manifest(
        runner="product_recall", dataset=Path(args.dataset), selection=selection, baseline=args.baseline_file,
        parameters={"profile": profile or "custom", "top_k": args.top_k, "requested_strategies": targets,
                    "thresholds": thresholds, "dry_run": args.dry_run,
                    "variant": "bm25_vector_rrf_v1" if args.strategy in {"bm25", "hybrid_rerank"} else "legacy_two_stage",
                    "gate_scope": "experiment" if args.strategy in {"bm25", "hybrid_rerank"} else "release" if args.split == "release" and profile == "online-main" else "diagnostic"},
    )
    if args.dry_run:
        path = write_manifest(manifest, report_path)
        print(f"仅校验选集：NOT_RUN；未计算指标、未判定通过。证据：{path}")
        return

    per_strategy: dict[str, Aggregate] = {}
    observations: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for strategy in targets:
        print(f"\n[{strategy}] 装配中…")
        observations[strategy] = []
        try:
            usecase, repo, _ = await build_usecase(strategy)
            agg = await run_dataset(usecase, repo, cases, args.top_k, observations=observations[strategy])
        except Exception as err:  # 保留失败证据，不把初始化异常或部分执行算作通过
            errors[strategy] = f"{type(err).__name__}: {err}"
            print(f"  [error] {errors[strategy]}")
            continue
        per_strategy[strategy] = agg
        print_summary(strategy, agg)

    verdicts = {name: gate(agg, thresholds, baselines.get(name))[0] for name, agg in per_strategy.items()}
    if profile == "hybrid-experimental":
        failures = hybrid_execution_errors(observations.get("hybrid_rerank", []))
        if failures:
            errors["hybrid_execution"] = "；".join(failures)
    blocked = bool(errors) or not verdicts or "BLOCK" in verdicts.values()
    stable = finish_manifest(
        manifest, actual_strategies={name: sorted(agg.recall_strategies) for name, agg in per_strategy.items()},
        gate="BLOCK" if blocked else "PASS", status="ERROR" if errors else "COMPLETED",
        metrics=per_strategy, observations=observations, errors=errors, strategy_verdicts=verdicts,
    )
    write_manifest(manifest, report_path)
    report_path.write_text(
        render_report(per_strategy, thresholds, args.top_k, baselines, dataset=Path(args.dataset), profile=profile or "custom")
        + manifest_report(manifest)
        + ("\n执行错误：\n" + "\n".join(f"- {name}: {message}" for name, message in errors.items()) if errors else ""),
        encoding="utf-8",
    )
    print(f"\n报告已写入 {report_path}")

    # 任一档位被阻断即以非零码退出，便于直接接 CI
    print(f"最终门禁：{manifest['execution']['gate']}；" + "，".join(f"{n}={v}" for n, v in verdicts.items()))
    if not stable:
        print("阻断原因：运行期间代码或数据输入发生变化，需在固定版本上重跑。")
    if blocked or not stable:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
