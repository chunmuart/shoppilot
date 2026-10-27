"""千件商品检索对照：纯词项/专用向量/混合召回/HTTP reranker；不调用 LLM。"""

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import time

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.context_usage import context_usage_sink
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryProductRepository,
)
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.rerank.factory import create_reranker
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
from scripts.eval.hard_constraints import find_hit_constraint_violations
from scripts.eval.retrieval_upgrade import paired_interval
from scripts.retrieval_preflight import check

ROOT = Path(__file__).resolve().parents[2]


def score(hits, relevant, k):
    # v2 无同类档位偏好时二元相关；不把按 ID 排序误解释成线性偏好金标。
    ids = list(
        dict.fromkeys(h["canonical_product_id"] or h["product_id"] for h in hits)
    )[:k]
    gold = set(relevant)
    if not gold:
        return {"recall": None, "mrr": None, "ndcg": None, "empty_ok": not hits}
    found = gold.intersection(ids)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(gold))))
    return {
        "recall": len(found) / len(gold),
        "mrr": next((1 / (i + 1) for i, x in enumerate(ids) if x in gold), 0),
        "ndcg": sum(1 / math.log2(i + 2) for i, x in enumerate(ids) if x in gold)
        / ideal,
        "empty_ok": None,
    }


def validate_cases(cases, products):
    ids = {p.product_id: p for p in products}
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("场景 ID 重复")
    families = defaultdict(set)
    for c in cases:
        families[c["family"]].add(c["split"])
        expected = {ids[pid].canonical_product_id for pid in c["relevant"]}
        if expected != set(c["relevant_canonical_ids"]):
            raise ValueError("金标实体不一致")
        if bool(expected) == c["expected_empty"]:
            raise ValueError("空结果金标不一致")
    if any(len(v) > 1 for v in families.values()):
        raise ValueError("开发/留出场景族泄漏")


def aggregate(samples):
    result = {}
    for metric in ("recall", "mrr", "ndcg"):
        values = [s[metric] for s in samples if s[metric] is not None]
        result[metric] = sum(values) / len(values) if values else None
    values = sorted(s["elapsed_ms"] for s in samples)
    result.update(
        p95_ms=values[math.ceil(len(values) * 0.95) - 1],
        hard_constraint_failures=sum(bool(s["violations"]) for s in samples),
        empty_failures=sum(s["empty_ok"] is False for s in samples),
        rerank_successes=sum(s["rerank_applied"] is True for s in samples),
        vector_successes=sum(s["vector_available"] is True for s in samples),
    )
    return result


async def evaluate(output, split, live=False):
    output.mkdir(parents=True, exist_ok=False)
    settings = load_settings()
    # 历史 v2 评测显式绑定自己的底座；默认目录升级不能改变旧实验的语料。
    catalog = ROOT / "data/catalog-v2.jsonl"
    repo = InMemoryProductRepository([_product_from_record(json.loads(line)) for line in catalog.read_text().splitlines() if line.strip()])
    products = await repo.list_all()
    by_id = {p.product_id: p for p in products}
    dataset = ROOT / "eval/v2/product_retrieval.jsonl"
    all_cases = [json.loads(l) for l in dataset.read_text().splitlines()]
    validate_cases(all_cases, products)
    cases = [c for c in all_cases if c["split"] == split]
    frozen_paths = [
        dataset,
        catalog,
        ROOT / "app/application/usecases/catalog_search.py",
        ROOT / "app/infrastructure/retrieval/bm25.py",
        ROOT / "app/infrastructure/rerank/http_reranker.py",
        Path(__file__),
    ]
    fingerprint = lambda: {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in frozen_paths
    }
    report = {
        "split": split,
        "cases": len(cases),
        "k": 5,
        "catalog_size": len(products),
        "platforms": dict(Counter(p.source_platform for p in products)),
        "frozen": fingerprint(),
        "weights": [1, 1],
        "candidate_limit": 32,
        "models": {
            "embedding": settings.embedding_model,
            "reranker": settings.reranker_model,
        },
        "scope": "合成目录模块评测；二元NDCG；按场景族配对bootstrap；不是生产收益",
        "llm_calls": 0,
        "status": "OFFLINE_ONLY",
        "samples": {},
        "usage": {},
    }
    variants = {
        "keyword": CatalogSearchUseCase(repo),
        "bm25": CatalogSearchUseCase(repo, hybrid_enabled=True),
    }
    index = None
    if live:
        report["dependencies"] = await check(settings)
        if all(x["status"] == "PASS" for x in report["dependencies"].values()):
            # 只向新目录建索引，绝不使用运行中的买家数据库或线上 Qdrant。
            isolated = replace(
                settings,
                data_dir=output / "index",
                qdrant_url="",
                qdrant_collection="retrieval_v2",
            )
            index = QdrantProductIndex(isolated)
            embedder = OpenAIEmbeddingClient(settings)
            embeddings = await embedder.embed_batch(
                [p.searchable_text() for p in products]
            )
            await index.ensure_ready(len(embeddings[0]))
            await index.upsert_products(products, embeddings)
            (output / "vectors.json").write_text(
                json.dumps({"model": settings.embedding_model, "vectors": embeddings})
            )
            ranker = create_reranker(replace(settings, reranker_mode="http"))
            variants.update(
                {
                    "dense": CatalogSearchUseCase(
                        repo,
                        embedder,
                        index,
                        hybrid_enabled=True,
                        hybrid_lexical_weight=0,
                    ),
                    "hybrid": CatalogSearchUseCase(
                        repo, embedder, index, hybrid_enabled=True
                    ),
                    "hybrid_reranker": CatalogSearchUseCase(
                        repo, embedder, index, reranker=ranker, hybrid_enabled=True
                    ),
                }
            )
            report["status"] = "LIVE_COMPLETED"
        else:
            report["status"] = "BLOCKED_DEPENDENCIES"
    try:
        for name in variants:
            report["samples"][name] = []
            report["usage"][name] = []
        for i, c in enumerate(cases):
            keys = list(variants)
            keys = keys[i % len(keys) :] + keys[: i % len(keys)]
            for name in keys:
                spec = ProductSearchSpec(
                    normalized_query=c["query"],
                    top_k=5,
                    ship_to=c.get("ship_to"),
                    price_max_major=c.get("price_max_major"),
                    category=c.get("category"),
                    target_currency=c.get("target_currency", "CNY"),
                )
                token = context_usage_sink.set(report["usage"][name].append)
                start = time.monotonic()
                try:
                    result = await variants[name].execute(spec)
                finally:
                    context_usage_sink.reset(token)
                hits = result["hits"]
                violations = find_hit_constraint_violations(
                    hits,
                    by_id,
                    ship_to=c.get("ship_to"),
                    price_max_major=c.get("price_max_major"),
                    target_currency=c.get("target_currency", "CNY"),
                    category=c.get("category"),
                    excluded_material_tags=[],
                    required_material_tags=[],
                )
                row = {
                    "id": c["id"],
                    "family": c["family"],
                    "kind": c["kind"],
                    "query": c["query"],
                    **score(hits, c["relevant_canonical_ids"], 5),
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                    "ids": [h["product_id"] for h in hits],
                    "gold": c["relevant_canonical_ids"],
                    "violations": {pid: sorted(v) for pid, v in violations.items()},
                    "strategy": result["recall_strategy"],
                    "rerank_applied": result.get("rerank_applied"),
                    "vector_available": result.get("vector_available"),
                    "diagnostics": result.get("retrieval_diagnostics", {}),
                }
                report["samples"][name].append(row)
            (output / "partial.json").write_text(json.dumps(report, ensure_ascii=False))
            print(f"{split}: {i + 1}/{len(cases)}", flush=True)
    finally:
        if index:
            await index.close()
    report["summary"] = {
        name: aggregate(rows) for name, rows in report["samples"].items()
    }
    report["by_kind"] = {
        name: {
            kind: aggregate([s for s in rows if s["kind"] == kind])
            for kind in {s["kind"] for s in rows}
        }
        for name, rows in report["samples"].items()
    }
    report["paired"] = {}
    for baseline, candidate in [
        ("keyword", "bm25"),
        ("dense", "hybrid"),
        ("dense", "hybrid_reranker"),
        ("hybrid", "hybrid_reranker"),
    ]:
        if candidate not in variants:
            continue
        metrics = {}
        for metric in ("recall", "mrr", "ndcg"):
            families = defaultdict(list)
            for a, b in zip(report["samples"][baseline], report["samples"][candidate]):
                if a[metric] is not None:
                    families[a["family"]].append(b[metric] - a[metric])
            deltas = [sum(v) / len(v) for v in families.values()]
            metrics[metric] = {
                "mean_delta": sum(deltas) / len(deltas),
                "family_bootstrap_95pct": paired_interval(deltas),
            }
        report["paired"][f"{candidate}_minus_{baseline}"] = metrics
    report["inputs_unchanged"] = report["frozen"] == fingerprint()
    report["failures"] = {
        name: [
            s
            for s in rows
            if s["recall"] is not None
            and s["recall"] < 1
            or s["empty_ok"] is False
            or s["violations"]
        ]
        for name, rows in report["samples"].items()
    }
    report["all_live_paths_executed"] = (
        all(
            s["vector_available"] is True
            and (not s["ids"] or s["rerank_applied"] is True)
            for s in report["samples"].get("hybrid_reranker", [])
        )
        if live and "hybrid_reranker" in variants
        else False
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("samples", "usage", "failures", "frozen")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--split", choices=["dev", "holdout"], required=True)
    p.add_argument("--live", action="store_true")
    args = p.parse_args()
    r = asyncio.run(evaluate(args.output, args.split, args.live))
    if args.live and not r["all_live_paths_executed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
