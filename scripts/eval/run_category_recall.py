# -*- coding: utf-8 -*-
"""品类知识库（CategoryInsight）召回评测 —— 见教程 13-1 §5。

与商品检索评测共用 `scripts/eval/metrics.py` 的三个指标，区别只在**标注单位**：

    商品检索      标注单位 = product_id
    品类知识库    标注单位 = 知识文档名（如 travel-gear.md）

标注单位为什么是文档名而不是 chunk id：`bootstrap_category_knowledge` 用
`ApproxTokenChunker(chunk_size=512, overlap=50)` 切片，chunk 边界会随 chunk_size
或文档内容变动而漂移，拿 chunk id 当标注会导致「改了一句话就要重标一遍」。
文档名稳定，且「该问题该由哪篇文档回答」本身就是运营能稳定判断的粒度。

用法（项目根目录执行，需 embedding 凭据 + Qdrant）：

    uv run python scripts/eval/run_category_recall.py
    uv run python scripts/eval/run_category_recall.py --top-k 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.infrastructure.rag.knowledge_retrieval import search_knowledge
from app.infrastructure.rag.category_knowledge import (  # noqa: E402
    bootstrap_category_knowledge,
    build_category_knowledge_base,
    has_answerable_knowledge,
    policy_fact_status,
)
from app.infrastructure.settings import load_settings  # noqa: E402
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
from scripts.eval.run_manifest import (  # noqa: E402
    SPLITS, build_manifest, finish_manifest, manifest_report, select_cases,
    validate_baseline_selection, write_manifest,
)

_DATASET = Path("eval/category_recall.jsonl")


def formal_thresholds() -> Thresholds:
    """知识正式集只门禁可完整标注的召回、排序与拒答能力。"""
    return Thresholds(
        recall=0.85, precision=None, mrr=0.85, ndcg=0.85,
        empty_accuracy=1.0, policy_rejection_accuracy=1.0,
    )


def load_dataset(path: Path) -> list[dict]:
    return [
        json.loads(raw)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]


def load_baseline(path: Path | None) -> dict[str, float] | None:
    """读取知识检索的批准基线；未传时仅执行绝对门槛。"""
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    metrics = raw.get("metrics", raw)
    if not isinstance(metrics, dict):
        raise ValueError("基线文件必须是 {recall, precision, mrr, ndcg} 对象")
    return {metric: float(value) for metric, value in metrics.items()}


def source_of(item) -> str:
    """取一条检索结果所属的知识文档名。

    与 `category_insight_tool` 保持同一口径：metadata.source 缺失时退回 document_id，
    否则评测口径和线上口径会不一致。
    """
    metadata = getattr(item.chunk, "metadata", None)
    if metadata:
        return metadata.get("source", item.document_id)
    return item.document_id


async def run_dataset(knowledge_base, cases: list[dict], top_k: int, *, observations: list[dict] | None = None) -> Aggregate:
    results: list[QueryResult] = []
    empty_results: list[bool] = []
    policy_results: list[bool] = []
    for case in cases:
        hits = await search_knowledge(knowledge_base, case["query"], top_k=top_k)
        # 同一篇文档可能命中多个 chunk：按首次出现保序去重，落到文档粒度
        retrieved: list[str] = []
        for item in hits:
            src = source_of(item)
            if src not in retrieved:
                retrieved.append(src)

        relevant = case["relevant"]
        observation = {
            "case_id": case.get("id"), "query": case["query"], "split": case.get("split"),
            "retrieved": retrieved, "relevant": relevant,
            "expected_unanswerable": bool(case.get("expected_unanswerable")),
            "unanswerable_pass": not has_answerable_knowledge(hits) if case.get("expected_unanswerable") else None,
        }
        if observations is not None:
            observations.append(observation)
        if case.get("expected_unanswerable"):
            # Qdrant 即使没有语义证据也会给出“最近邻”；必须按可回答阈值拒答。
            empty_results.append(not has_answerable_knowledge(hits))
            continue
        if case.get("expected_behavior"):
            statuses = [
                policy_fact_status(getattr(item.chunk, "metadata", None) or {})
                for item in hits
                if source_of(item) in relevant
            ]
            # 命中目标政策文档且每条都被标为不可作确定事实，才算具备可执行拒答证据。
            policy_results.append(bool(statuses) and all(status != "fact_eligible" for status in statuses))
            observation.update(policy_statuses=statuses, policy_pass=policy_results[-1])
        results.append(
            QueryResult(
                query=case["query"],
                retrieved=retrieved,
                relevant=relevant,
                recall=recall_at_k(retrieved, relevant, top_k),
                mrr=mrr(retrieved, relevant),
                ndcg=ndcg_at_k(retrieved, relevant, top_k),
                precision=precision_at_k(retrieved, relevant, top_k),
                kind=case.get("kind", "knowledge"),
                dimensions={"split": str(case.get("split") or "ALL")},
            ),
        )
    return evaluate(results, k=top_k, empty_results=empty_results, policy_results=policy_results)


def render_report(
    agg: Aggregate,
    thresholds: Thresholds,
    baseline: dict[str, float] | None = None,
    dataset: Path | str = _DATASET,
) -> str:
    verdict, reasons = gate(agg, thresholds, baseline)
    lines = [
        f"# 品类知识库召回评测报告（{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）",
        "",
        f"标注集 `{dataset}`，正例 {agg.count} 条。标注单位为知识文档名。",
        "",
        f"| 指标 | 值 | 阈值 |",
        "|---|---|---|",
        f"| Recall@{agg.k} | {agg.recall:.3f} | ≥ {thresholds.recall}（阻断） |",
        f"| Precision@{agg.k} | {agg.precision:.3f} | "
        f"{('观察项（未穷举金标，不阻断）' if thresholds.precision is None else f'≥ {thresholds.precision}（阻断）')} |",
        f"| MRR | {agg.mrr:.3f} | ≥ {thresholds.mrr}（阻断） |",
        f"| NDCG@{agg.k} | {agg.ndcg:.3f} | ≥ {thresholds.ndcg}（阻断） |",
        f"| 不可回答准确率 | {'n/a' if agg.empty_accuracy is None else f'{agg.empty_accuracy:.3f}'} | "
        f"{('未启用' if thresholds.empty_accuracy is None else f'≥ {thresholds.empty_accuracy}（阻断）') } |",
        f"| 政策拒答准确率 | {'n/a' if agg.policy_rejection_accuracy is None else f'{agg.policy_rejection_accuracy:.3f}'} | "
        f"{('未启用' if thresholds.policy_rejection_accuracy is None else f'≥ {thresholds.policy_rejection_accuracy}（阻断）') } |",
        "",
        f"门禁结论：**{verdict}**",
        "",
    ]
    if reasons:
        lines += ["未达标项：", *[f"- {r}" for r in reasons], ""]
    lines += ["| query | Recall | Precision | MRR | NDCG | 召回文档 | 标注文档 |", "|---|---|---|---|---|---|---|"]
    for r in agg.per_query:
        lines.append(
            f"| {r.query} | {r.recall:.2f} | {r.precision:.2f} | {r.mrr:.2f} | {r.ndcg:.2f} | "
            f"{','.join(r.retrieved) or '（空）'} | {','.join(r.relevant)} |",
        )
    return "\n".join(lines) + "\n"


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="品类知识库召回评测")
    parser.add_argument("--dataset", default=str(_DATASET))
    parser.add_argument("--split", choices=SPLITS, default="all", help="仅执行选中的 dev/release；默认 all 兼容旧集")
    parser.add_argument("--dry-run", action="store_true", help="只校验选集并写 NOT_RUN 证据，不调用 embedding/Qdrant")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-recall", type=float, default=0.75)
    parser.add_argument("--min-precision", type=float, default=0.45)
    parser.add_argument("--min-mrr", type=float, default=0.65)
    parser.add_argument("--min-ndcg", type=float, default=0.70)
    parser.add_argument("--min-unanswerable-accuracy", type=float, default=None)
    parser.add_argument("--formal-gates", action="store_true", help="启用正式知识集门槛：K=3、Recall/MRR/NDCG≥0.85、不可回答=100%%")
    parser.add_argument("--baseline-file", type=Path, default=None, help="批准基线 JSON；指标下降超过 2 个百分点即阻断")
    parser.add_argument("--report-dir", default="eval")
    args = parser.parse_args(argv)
    try:
        cases, selection = select_cases(load_dataset(Path(args.dataset)), args.split)
        validate_baseline_selection(args.baseline_file, selection, Path(args.dataset))
    except (ValueError, OSError) as err:
        parser.error(str(err))
    top_k = 3 if args.formal_gates else args.top_k
    if top_k <= 0:
        parser.error("--top-k 必须为正整数")
    print(f"标注集 {args.dataset}：split={args.split}，{len(cases)} 条，K={top_k}")
    thresholds = (
        formal_thresholds()
        if args.formal_gates
        else Thresholds(
            recall=args.min_recall, precision=args.min_precision, mrr=args.min_mrr,
            ndcg=args.min_ndcg, empty_accuracy=args.min_unanswerable_accuracy,
        )
    )
    baseline = load_baseline(args.baseline_file)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"category-recall-{args.split}-report-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.md"
    manifest = build_manifest(
        runner="category_recall", dataset=Path(args.dataset), selection=selection, baseline=args.baseline_file,
        parameters={"formal_gates": args.formal_gates, "top_k": top_k, "thresholds": thresholds,
                    "requested_strategies": ["category_vector_document_scope_v1"], "dry_run": args.dry_run,
                    "gate_scope": "release" if args.split == "release" and args.formal_gates else "diagnostic"},
    )
    if args.dry_run:
        path = write_manifest(manifest, report_path)
        print(f"仅校验选集：NOT_RUN；未计算指标、未判定通过。证据：{path}")
        return
    observations: list[dict] = []
    try:
        settings = load_settings()
        knowledge_base = build_category_knowledge_base(settings)
        inserted = await bootstrap_category_knowledge(knowledge_base)
        print(f"知识库就绪（本次新增 {inserted} 篇）")
        agg = await run_dataset(knowledge_base, cases, top_k, observations=observations)
    except Exception as err:
        finish_manifest(manifest, actual_strategies=["category_vector_document_scope_v1"] if observations else [], gate="BLOCK", status="ERROR", observations=observations, error=f"{type(err).__name__}: {err}")
        write_manifest(manifest, report_path)
        report_path.write_text(f"# 品类知识库评测未完成\n\n{type(err).__name__}: {err}" + manifest_report(manifest), encoding="utf-8")
        print(f"执行失败，已保存 BLOCK 证据：{report_path}")
        raise SystemExit(1) from err
    print(
        f"  Recall@{agg.k}={agg.recall:.3f}  Precision@{agg.k}={agg.precision:.3f}  MRR={agg.mrr:.3f}  "
        f"NDCG@{agg.k}={agg.ndcg:.3f}  不可回答准确率={'n/a' if agg.empty_accuracy is None else f'{agg.empty_accuracy:.3f}'}  "
        f"政策拒答准确率={'n/a' if agg.policy_rejection_accuracy is None else f'{agg.policy_rejection_accuracy:.3f}'}",
    )

    verdict, reasons = gate(agg, thresholds, baseline)
    stable = finish_manifest(manifest, actual_strategies=["category_vector_document_scope_v1"], gate=verdict, metrics=agg, observations=observations, reasons=reasons)
    write_manifest(manifest, report_path)
    report_path.write_text(render_report(agg, thresholds, baseline, dataset=Path(args.dataset)) + manifest_report(manifest), encoding="utf-8")
    print(f"报告已写入 {report_path}")

    print(f"最终门禁：{manifest['execution']['gate']}" + (f"（{'；'.join(reasons)}）" if reasons else ""))
    if not stable:
        print("阻断原因：运行期间代码或数据输入发生变化，需在固定版本上重跑。")
    if verdict == "BLOCK" or not stable:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
