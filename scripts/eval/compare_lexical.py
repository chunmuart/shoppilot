"""冻结数据上的本地词项对比；此实验不能证明 Hybrid 在线收益或批准发布。"""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.eval.run_product_recall import load_dataset, run_dataset, by_kind
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository


async def main():
    _DATASET = ROOT/"eval/v1/product_retrieval.jsonl"
    cases = load_dataset(_DATASET)
    repo = InMemoryProductRepository()
    files = [ROOT/"app/infrastructure/retrieval/bm25.py", ROOT/"app/application/usecases/catalog_search.py", Path(_DATASET)]
    fingerprint = lambda: {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    report = {"experiment": "lexical_baseline_vs_bm25_v1", "data_sha256": hashlib.sha256(Path(_DATASET).read_bytes()).hexdigest(),
              "cases": len(cases), "top_k": 8, "source_before": fingerprint(), "promotion": "NOT_APPROVED",
              "scope": "本地词项诊断；无向量/重排/LLM，不代表Hybrid在线主链", "variants": {}}
    for name, enabled in [("keyword_2gram", False), ("bm25", True)]:
        evidence = []
        started = time.monotonic()
        aggregate = await run_dataset(CatalogSearchUseCase(repo, hybrid_enabled=enabled), repo, cases, 8, observations=evidence)
        report["variants"][name] = {"metrics": asdict(aggregate), "by_kind": {k: asdict(v) for k,v in by_kind(aggregate).items()},
                                    "elapsed_ms": round((time.monotonic()-started)*1000), "observations": evidence}
    report["source_after"] = fingerprint()
    report["inputs_unchanged"] = report["source_before"] == report["source_after"]
    target = ROOT/"eval/verification/hybrid-20260909/lexical-comparison.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=lambda x: sorted(x) if isinstance(x,set) else str(x))+"\n", encoding="utf-8")
    print(json.dumps({"path": str(target), "inputs_unchanged": report["inputs_unchanged"], "metrics": {
        name: {k: value["metrics"][k] for k in ("recall", "precision", "mrr", "ndcg", "filter_accuracy", "empty_accuracy")}
        for name, value in report["variants"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
