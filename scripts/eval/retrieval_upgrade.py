"""检索升级评测：冻结目录，交错成对比较；缺向量服务时不宣称 Hybrid 达标。"""

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import random
import time
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryProductRepository,
)
from app.infrastructure.settings import load_settings
from app.infrastructure.context_usage import context_usage_sink
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.rerank.factory import create_reranker
from scripts.eval.run_product_recall import run_dataset


def paired_interval(differences):
    if not differences:
        return None
    rng = random.Random(20260918)
    samples = sorted(
        sum(rng.choices(differences, k=len(differences))) / len(differences)
        for _ in range(2000)
    )
    return [samples[49], samples[1949]]


async def evaluate(output, live_rerank=False, live_cases=12):
    output.mkdir(parents=True, exist_ok=False)
    source = Path("eval/v1/product_retrieval.jsonl")
    cases = [json.loads(l) for l in source.read_text().splitlines() if l.strip()]
    repo = InMemoryProductRepository()
    settings = load_settings()
    products = await repo.list_all()
    report = {
        "dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "catalog_sha256": hashlib.sha256(
            "\n".join(p.searchable_text() for p in products).encode()
        ).hexdigest(),
        "code_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                Path("app/application/usecases/catalog_search.py"),
                Path("app/infrastructure/retrieval/bm25.py"),
                Path("app/infrastructure/rerank/http_reranker.py"),
            ]
        },
        "frozen": {
            "rrf_weights": [
                settings.hybrid_lexical_weight,
                settings.hybrid_vector_weight,
            ],
            "recall_candidates": settings.recall_candidates,
            "top_k": 8,
        },
        "promotion": "NOT_APPROVED",
        "experiments": [],
        "dependencies": {},
    }
    try:
        embedding = await asyncio.wait_for(
            OpenAIEmbeddingClient(settings).embed("轻便旅行背包"), 20
        )
        report["dependencies"]["embedding"] = {
            "status": "PASS",
            "dimensions": len(embedding),
            "model": settings.embedding_model,
        }
    except Exception as error:
        report["dependencies"]["embedding"] = {
            "status": "BLOCKED",
            "error_type": type(error).__name__,
            "model": settings.embedding_model,
        }
    variants = {
        "keyword_2gram": CatalogSearchUseCase(repo),
        "bm25": CatalogSearchUseCase(repo, hybrid_enabled=True),
    }

    async def compare(name, selected, variants):
        samples = {key: [] for key in variants}
        usage = {key: [] for key in variants}
        for i, case in enumerate(selected):
            keys = list(variants)
            if i % 2:
                keys.reverse()
            for key in keys:
                started = time.monotonic()
                token = context_usage_sink.set(usage[key].append)
                evidence = []
                try:
                    aggregate = await run_dataset(
                        variants[key], repo, [case], 8, observations=evidence
                    )
                    metrics = {
                        k: asdict(aggregate)[k]
                        for k in (
                            "recall",
                            "precision",
                            "mrr",
                            "ndcg",
                            "filter_accuracy",
                            "empty_accuracy",
                        )
                    }
                    samples[key].append(
                        {
                            "id": case["id"],
                            "split": case["split"],
                            "metrics": metrics,
                            "elapsed_ms": (time.monotonic() - started) * 1000,
                            "observation": evidence[0],
                        }
                    )
                finally:
                    context_usage_sink.reset(token)
        baseline, candidate = list(variants)
        delta = {
            metric: [
                b["metrics"][metric] - a["metrics"][metric]
                for a, b in zip(samples[baseline], samples[candidate])
                if a["metrics"][metric] is not None and b["metrics"][metric] is not None
            ]
            for metric in ("recall", "mrr", "ndcg")
        }

        def summary(key):
            items = samples[key]
            return {
                **{
                    metric: sum(vals) / len(vals) if vals else None
                    for metric in ("recall", "mrr", "ndcg")
                    for vals in [
                        [
                            x["metrics"][metric]
                            for x in items
                            if x["metrics"][metric] is not None
                        ]
                    ]
                },
                "p95_ms": sorted(x["elapsed_ms"] for x in items)[
                    min(len(items) - 1, int(len(items) * 0.95))
                ],
                "actual_model_calls": len(usage[key]),
                "unknown_usage_calls": sum(
                    x["input_tokens"] is None or x["output_tokens"] is None
                    for x in usage[key]
                ),
                "known_input_tokens_subtotal": sum(
                    x["input_tokens"]
                    for x in usage[key]
                    if x["input_tokens"] is not None
                ),
                "known_output_tokens_subtotal": sum(
                    x["output_tokens"]
                    for x in usage[key]
                    if x["output_tokens"] is not None
                ),
                "rerank_applied_cases": sum(
                    bool(x["observation"]["rerank_applied"]) for x in items
                ),
                "hard_constraint_failures": sum(
                    x["observation"].get("filter_ok") is False for x in items
                ),
                "actual_input_tokens": sum(x["input_tokens"] for x in usage[key])
                if usage[key] and all(x["input_tokens"] is not None for x in usage[key])
                else None,
                "actual_output_tokens": sum(x["output_tokens"] for x in usage[key])
                if usage[key]
                and all(x["output_tokens"] is not None for x in usage[key])
                else None,
            }

        return {
            "name": name,
            "scenarios": len(selected),
            "splits": dict(Counter(c["split"] for c in selected)),
            "summary": {k: summary(k) for k in variants},
            "paired_delta": {
                k: {
                    "mean": sum(v) / len(v) if v else None,
                    "bootstrap_95pct": paired_interval(v),
                }
                for k, v in delta.items()
            },
            "samples": samples,
            "usage": usage,
            "scope": "模块级诊断；不是全 Agent 发版门禁",
        }

    report["experiments"].append(
        await compare("full_lexical_diagnostic", cases, variants)
    )
    # 固定按数据集中 release 顺序取样，参数不使用这部分标签调优；小规模只作诊断。
    if live_rerank:
        ranker = create_reranker(replace(settings, reranker_mode="http"))
        selected = [c for c in cases if c["split"] == "release"][:live_cases]
        if not selected:
            raise ValueError("未找到 release 样本")
        report["experiments"].append(
            await compare(
                "live_http_rerank_pilot",
                selected,
                {
                    "bm25": variants["bm25"],
                    "bm25_reranker": CatalogSearchUseCase(
                        repo, hybrid_enabled=True, reranker=ranker
                    ),
                },
            )
        )
    report["hybrid_quality"] = (
        "NOT_EVALUATED"
        if report["dependencies"]["embedding"]["status"] == "PASS"
        else "BLOCKED_EMBEDDING"
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    print(
        json.dumps(
            {
                **report,
                "experiments": [
                    {k: v for k, v in e.items() if k not in {"samples", "usage"}}
                    for e in report["experiments"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--live-rerank", action="store_true")
    p.add_argument("--live-cases", type=int, default=12)
    args = p.parse_args()
    if not 1 <= args.live_cases <= 45:
        p.error("--live-cases 应为 1 到 45")
    asyncio.run(evaluate(args.output, args.live_rerank, args.live_cases))


if __name__ == "__main__":
    main()
