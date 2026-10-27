"""600条Skill评测入口：数据校验、真实SQLite流程、真实模型对照分开计分。"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import replace
import html
import json
from pathlib import Path
import random
import statistics
import tarfile

from app.domain.learning import digest
from scripts.eval.harness.contracts import assert_output_path, source_manifest, write_json, ROOT
from scripts.eval.run_learning import evaluate, aggregate
from scripts.eval.skill_suite import DEFAULT_SUITE, load_suite, validate_suite, select_cases, run_contract, skill_document, publish_fixture


def summary_for(suite, rows, selected, mode, profile, repetitions):
    groups = {}
    for group, title in suite["groups"].items():
        contracts = [r for r in rows if r["group"] == group and r["execution"] == "production_sqlite_contract"]
        arms = {a: [r for r in rows if r["group"] == group and r.get("arm") == a] for a in ("baseline", "candidate")}
        groups[group] = {"title": title, "dataset_cases": 100, "selected_cases": sum(c["group"] == group for c in selected),
                         "contracts": {"runs": len(contracts), "passed": sum(r["passed"] for r in contracts)},
                         **{arm: {"runs": len(items), "passed": sum(r["passed"] for r in items)} for arm, items in arms.items()}}
    models = [r for r in rows if r["execution"] == "real_model"]
    metrics = aggregate(models, [], repetitions=repetitions) if models else None
    # 不把同一模板的10个变体和3次重复当成独立样本。
    families = defaultdict(lambda: defaultdict(list))
    for row in models:
        families[row["family"]][row["arm"]].append(float(row["passed"]))
    differences = [statistics.mean(a["candidate"])-statistics.mean(a["baseline"]) for a in families.values()
                   if len(a["candidate"]) == len(a["baseline"]) == 10*repetitions]
    rng = random.Random(20261003)
    boot = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(2000)) if differences else []
    if metrics:
        metrics.pop("paired_scene_ci95", None)
        metrics.pop("success_difference_by_case", None)
        metrics.update(paired_family_count=len(differences), paired_family_ci95=[boot[49], boot[1949]] if boot else None,
                       comparison_scope="只读检索参数与Skill正文执行；非完整购物和自然目录命中")
    executed = {r["case_id"] for r in rows}
    return {"schema_version": "skill-suite-report-v2", "mode": mode, "profile": profile, "groups": groups,
            "dataset_cases": 600, "selected_cases": len(selected), "executed_cases": len(executed),
            "unexecuted_selected_cases": len(selected)-len(executed), "failed_runs": sum(not r["passed"] for r in rows),
            "model_metrics": metrics, "repetitions": repetitions,
            "release_gate": "NOT_CONNECTED_TO_PUBLICATION", "benefit_confirmed": False,
            "limits": suite["limits"]}


def render(folder, suite, summary, rows):
    esc = lambda x: html.escape(str(x))
    body = []
    for g in summary["groups"].values():
        fields = [g["title"], g["dataset_cases"], g["selected_cases"],
                  f'{g["contracts"]["passed"]}/{g["contracts"]["runs"]}' if g["contracts"]["runs"] else "未执行",
                  f'{g["baseline"]["passed"]}/{g["baseline"]["runs"]}' if g["baseline"]["runs"] else "未执行",
                  f'{g["candidate"]["passed"]}/{g["candidate"]["runs"]}' if g["candidate"]["runs"] else "未执行"]
        body.append("<tr>"+"".join("<td>"+esc(v)+"</td>" for v in fields)+"</tr>")
    # 便于用户检查所有原始问题、状态、正确断言；不需要打开巨大的JSON。
    cards = []
    for c in suite["cases"]:
        sample = {k: c[k] for k in ("query", "history", "reference", "fixture", "expected", "rationale") if k in c}
        cards.append(f'<details data-group="{esc(c["group"])}"><summary>{esc(c["id"])} · {esc(c["split"])}</summary><pre>{esc(json.dumps(sample, ensure_ascii=False, indent=2))}</pre></details>')
    failures = [r for r in rows if not r["passed"]]
    (folder/"report.html").write_text("""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Skill 600条评测</title>
<style>body{font:16px/1.7 system-ui;max-width:1120px;margin:40px auto;padding:20px;color:#222}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap;background:#f5f5f2;padding:16px}summary{cursor:pointer;padding:8px}input{padding:10px;width:95%;margin:18px 0}.note{background:#fff6df;padding:16px}</style>
<h1>Skill评测：6类 × 100条</h1><p>60个场景家族，开发240条、留出360条。同一家族的10个变体不跨集合。</p>
<p class="note">数据检查通过 ≠ 模型通过。模型决策与SQLite流程分别计分，未执行项明确保留。尚未接入强制发布门槛，不自动发布。</p>"""
        +"<p>本次档位："+esc(summary["mode"])+" / "+esc(summary["profile"])+"；执行场景 "+esc(summary["executed_cases"])+" / "+esc(summary["selected_cases"])+"；失败运行 "+esc(summary["failed_runs"])+"。</p>"
        +"<table><tr><th>场景</th><th>总数</th><th>本次选取</th><th>数据库流程</th><th>无Skill/旧版</th><th>有Skill/新版</th></tr>"+"".join(body)+"</table>"
        +"<p>通过/运行次数。重复运行不是新增用例。<a href='summary.json'>汇总</a> · <a href='results.json'>逐次结果</a> · <a href='suite.json'>冻结评测集</a> · <a href='manifest.json'>版本证据</a></p>"
        +"<h2>失败样本</h2><pre>"+esc(json.dumps(failures, ensure_ascii=False, indent=2))+"</pre>"
        +"<h2>指标与边界</h2><pre>"+esc(json.dumps(summary["model_metrics"], ensure_ascii=False, indent=2))+"</pre>"
        +"<p>"+"<br>".join(esc(x) for x in suite["limits"])+"</p><h2>全部600条用例</h2><input id='filter' placeholder='搜索场景、国家、币种、断言或关键词'><section id='cases'>"
        +"".join(cards)+"</section><script>document.querySelector('#filter').addEventListener('input',e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('details').forEach(d=>d.hidden=!d.textContent.toLowerCase().includes(q));});</script></html>")


async def run(args):
    suite = load_suite(args.suite)
    inventory = validate_suite(suite)
    selected = select_cases(suite, args.profile)
    repetitions = 1 if args.profile == "smoke" else suite["repetitions"]
    output = assert_output_path(args.output)
    output.mkdir(parents=True)
    frozen = source_manifest()
    manifest = {"status": "running", "source": frozen, "dataset_sha256": digest(suite), "mode": args.mode,
                "profile": args.profile, "selected_ids": [c["id"] for c in selected], "repetitions": repetitions,
                "synthetic": True, "release_gate": "NOT_CONNECTED_TO_PUBLICATION", "external_model_requests": args.mode == "model"}
    write_json(output/"suite.json", suite); write_json(output/"validation.json", inventory)
    with tarfile.open(output/"source.tar.gz", "w:gz") as archive:
        for name in frozen["files"]:
            archive.add(ROOT/name, arcname=name)
    rows = []
    def save():
        summary = summary_for(suite, rows, selected, args.mode, args.profile, repetitions)
        summary.update(status=manifest["status"], source_stable=manifest.get("source_stable"))
        write_json(output/"manifest.json", manifest); write_json(output/"results.json", rows)
        write_json(output/"summary.json", summary); render(output, suite, summary, rows)
        return summary
    save()
    model = None
    try:
        if args.mode in {"contracts", "model"}:
            for case in selected:
                if case["adapter"] != "model_search":
                    try:
                        rows.append(await run_contract(case, output/"isolated"/case["id"]))
                    except Exception as error:
                        rows.append({"case_id": case["id"], "group": case["group"], "family": case["family"], "split": case["split"],
                                     "execution": "production_sqlite_contract", "passed": False, "violations": [type(error).__name__]})
            save()
        if args.mode == "model":
            from app.infrastructure.llm import create_chat_model
            from app.infrastructure.settings import load_settings
            from app.infrastructure.capability_registry import CapabilityRegistry, validate
            config = replace(load_settings(), data_dir=output/"isolated", llm_fallback_model="", llm_max_retries=0,
                             semantic_cache_enabled=False, redis_url="", queue_enabled=False, otlp_endpoint="", otlp_traces_endpoint="", llm_max_concurrency=1)
            documents = {arm: validate(json.loads(path.read_text())) if path else None
                         for arm, path in (("baseline", args.baseline_skill), ("candidate", args.candidate_skill))}
            if any(doc and doc["kind"] != "skill" for doc in documents.values()):
                raise ValueError("该入口接收Skill文档；策略提炼请使用run_learning")
            manifest.update(model=config.llm_model, gateway_sha256=digest(config.llm_base_url),
                            skill_hashes={a: digest(d) if d else None for a, d in documents.items()},
                            candidate_mode="supplied_skill" if args.candidate_skill else "per_case_synthetic_reference",
                            baseline_mode="supplied_skill" if args.baseline_skill else "without_skill")
            write_json(output/"skill-documents.json", documents)
            model = create_chat_model(config, stream=False)
            for repeat in range(repetitions):
                for index, case in enumerate(selected):
                    if case["adapter"] != "model_search": continue
                    for arm in (("baseline", "candidate") if (index+repeat)%2 == 0 else ("candidate", "baseline")):
                        registry = CapabilityRegistry(output/"isolated"/case["id"]/f"{arm}-{repeat}.db")
                        document = documents[arm] or (skill_document(case) if arm == "candidate" else None)
                        reference = None
                        if document:
                            publish_fixture(registry, document)
                            reference = registry.load_skill(document["id"], document["version"], available_tools={"product_search_tool"})
                        result = await evaluate({**case, "strict_tool_errors": True}, registry, config, model, repeat, arm, skill_reference=reference)
                        result.update(group=case["group"], family=case["family"], split=case["split"], execution="real_model",
                                      reference_hash=reference["content_hash"] if reference else None)
                        rows.append(result); save()
                        print(json.dumps({k: result[k] for k in ("case_id", "arm", "passed", "violations")}, ensure_ascii=False), flush=True)
                        await asyncio.sleep(2)
        manifest.update(status="completed", source_stable=source_manifest()["sha256"] == frozen["sha256"])
    except BaseException as error:
        manifest.update(status="interrupted", error_type=type(error).__name__)
        save()
        raise
    finally:
        if model is not None:
            await model.client.close()
    return save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["validate", "contracts", "model"], default="validate")
    parser.add_argument("--profile", choices=["all", "smoke", "dev", "release"], default="all")
    parser.add_argument("--candidate-skill", type=Path)
    parser.add_argument("--baseline-skill", type=Path)
    args = parser.parse_args(argv)
    summary = asyncio.run(run(args))
    print(json.dumps({k: summary[k] for k in ("dataset_cases", "executed_cases", "unexecuted_selected_cases", "failed_runs", "release_gate")}, ensure_ascii=False))
    return 2 if summary["failed_runs"] or summary["source_stable"] is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
