"""本机学习管理入口。只产生候选和数据，不启动训练，也不自动发布。"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
from pathlib import Path

from app.domain.learning import digest
from app.infrastructure.learning_store import LearningStore


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


async def run_jobs(store, *, max_jobs, with_model=False):
    from app.application.learning import LearningWorker
    extractor = None
    if with_model:
        from dataclasses import replace
        from app.infrastructure.llm import create_chat_model
        from app.infrastructure.settings import load_settings
        from app.infrastructure.strategy_extractor import AgentScopeStrategyExtractor
        config = replace(load_settings(), llm_fallback_model="", llm_max_retries=0)
        extractor = AgentScopeStrategyExtractor(create_chat_model(config, stream=False))
    worker = LearningWorker(store, extractor)
    results = []
    for _ in range(max_jobs):
        result = await worker.run_once()
        if result is None:
            break
        results.append(result)
    return results


def write_report(store, output):
    output.mkdir(parents=True, exist_ok=False)
    summary = store.summary()
    jobs = store.list_records("job", 200)
    result = {"summary": summary, "jobs_shown": len(jobs), "job_limit": 200,
              "usage": [usage for job in jobs for attempt in (job.get("result") or {}).get("attempt_history", []) for usage in attempt.get("usage", [])],
              "failures": [{"id": job["id"], "status": job["status"], "attempts": job["attempts"], "error": job["last_error"]} for job in jobs if job["last_error"]],
              "benefit": "未知；命中与单测不是收益证明", "trainer_started": False}
    (output/"report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    sections = "".join("<h2>"+html.escape(table)+"</h2><pre>"+html.escape(json.dumps(rows, ensure_ascii=False, indent=2))+"</pre>" for table, rows in summary.items())
    (output/"report.html").write_text("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>学习回流报告</title><style>body{max-width:960px;margin:48px auto;font:16px/1.65 system-ui;padding:24px}pre{background:#f5f5f2;padding:20px;white-space:pre-wrap}h1{font-size:30px}</style><h1>策略与训练数据回流</h1><p>候选需要验证和人工审核。失败诊断不能作为正确答案。没有启动训练。</p>"+sections+"<h2>验证边界</h2><p>这里只汇总最近 200 个任务；未知 usage 不记零。命中不代表采纳，采纳不代表收益。收益以独立成对评测报告为准。</p></html>")
    return {"report": str(output/"report.html"), "trainer_started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest"); ingest.add_argument("--manifest", required=True, type=Path)
    evidence = sub.add_parser("import-evidence"); evidence.add_argument("--file", required=True, type=Path)
    work = sub.add_parser("work"); work.add_argument("--max-jobs", type=int, default=20); work.add_argument("--with-model", action="store_true")
    listing = sub.add_parser("list"); listing.add_argument("--kind", choices=["job", "strategy", "sample", "feedback"], required=True)
    validate = sub.add_parser("validate-strategy"); validate.add_argument("--file", type=Path, required=True)
    prepare = sub.add_parser("prepare-training"); prepare.add_argument("--file", type=Path, required=True)
    review = sub.add_parser("review"); review.add_argument("--file", type=Path, required=True)
    publish = sub.add_parser("publish-strategy"); publish.add_argument("--file", type=Path, required=True); publish.add_argument("--registry", required=True, type=Path)
    withdraw = sub.add_parser("withdraw"); withdraw.add_argument("--file", required=True, type=Path); withdraw.add_argument("--registry", type=Path)
    feedback = sub.add_parser("feedback"); feedback.add_argument("--file", required=True, type=Path)
    retry = sub.add_parser("retry-job"); retry.add_argument("--file", required=True, type=Path)
    export = sub.add_parser("export"); export.add_argument("--output", required=True, type=Path); export.add_argument("--dataset-version", required=True); export.add_argument("--split", default="train", choices=["train", "dev", "val", "runtime"])
    report = sub.add_parser("report"); report.add_argument("--output", required=True, type=Path)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    store = LearningStore(args.db)
    if args.command == "ingest":
        from scripts.eval.learning_import import import_learning_report
        result = import_learning_report(store, args.manifest)
        result["processing"] = asyncio.run(run_jobs(store, max_jobs=200))
    elif args.command == "import-evidence":
        payload = read_json(args.file)
        if payload.get("synthetic") is not True:
            raise ValueError("外部证据包目前仅允许合成 fixture；真实运行必须从事务日志回流")
        identity = store.enqueue(source_kind="fixture", source_ref=str(args.file.resolve()), source_hash=digest(payload), buyer_id=payload["buyer_id"], session_id=payload["session_id"], payload=payload)
        result = {"job_id": identity}
    elif args.command == "work":
        if not 1 <= args.max_jobs <= 200:
            raise ValueError("每次最多处理 200 个任务")
        result = asyncio.run(run_jobs(store, max_jobs=args.max_jobs, with_model=args.with_model))
    elif args.command == "list":
        result = store.list_records(args.kind)
    elif args.command == "validate-strategy":
        result = store.validate_candidate(**read_json(args.file))
    elif args.command == "prepare-training":
        result = {"sample_id": store.prepare_training(**read_json(args.file)), "trainer_started": False}
    elif args.command == "review":
        result = store.review(**read_json(args.file))
    elif args.command == "publish-strategy":
        from app.infrastructure.capability_registry import CapabilityRegistry
        from app.infrastructure.learning_publication import publish_candidate
        result = publish_candidate(store, CapabilityRegistry(args.registry), **read_json(args.file))
    elif args.command == "withdraw":
        data = read_json(args.file)
        if data["kind"] == "strategy":
            from app.infrastructure.capability_registry import CapabilityRegistry
            from app.infrastructure.learning_publication import withdraw_candidate
            if args.registry is None:
                raise ValueError("撤回策略需要注册表路径，以同步撤销已发布版本")
            data.pop("kind")
            result = withdraw_candidate(store, CapabilityRegistry(args.registry), **data)
        else:
            result = store.withdraw(**data)
    elif args.command == "feedback":
        result = store.record_adoption(**read_json(args.file))
    elif args.command == "retry-job":
        result = store.retry_job(**read_json(args.file))
    elif args.command == "export":
        result = store.export_dataset(args.output, dataset_version=args.dataset_version, split=args.split)
    elif args.command == "report":
        result = write_report(store, args.output)
    else:
        result = store.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    rows = result if isinstance(result, list) else result.get("processing", []) if isinstance(result, dict) else []
    return 2 if any(isinstance(row, dict) and row.get("status") == "retry_or_failed" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
