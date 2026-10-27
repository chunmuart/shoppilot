"""GEPA 离线搜索 Prompt / 公共 Skill 候选优化，绝不自动导入或发布。

运行：uv run --extra optimization python -m scripts.optimize_prompts --output .pytest_cache/gepa-run
候选仍需现有全链路成对评测和 prompt_release / capability_review 的人工门禁。
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import math
import re
from dataclasses import replace
from pathlib import Path
import yaml


def validate_cases(cases):
    seen_ids, seen_queries = set(), set()
    for case in cases:
        if (
            not isinstance(case.get("id"), str)
            or not case["id"]
            or case.get("split") not in {"train", "val", "holdout"}
            or not isinstance(case.get("expected"), dict)
        ):
            raise ValueError("样本需要 id / train,val,holdout split / expected")
        query = case.get("query", "")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("样本需求不能为空")
        normalized = re.sub(r"\s+", "", query).casefold()
        if case["id"] in seen_ids or normalized in seen_queries:
            raise ValueError("样本 ID 或需求重复；禁止训练与留出泄漏")
        seen_ids.add(case["id"])
        seen_queries.add(normalized)
    if {c["split"] for c in cases} != {"train", "val", "holdout"}:
        raise ValueError("必须分别提供训练、选择与留出集")


def reserve_output(output):
    # 新建专属目录，不覆盖既有报告、生产数据或上次候选。
    output.mkdir(parents=True, exist_ok=False)


def optimize_candidate(
    seed, cases, evaluator, output, *, max_calls=24, proposer=None, reflection_lm=None
):
    from gepa import optimize
    from gepa.core.adapter import EvaluationBatch

    validate_cases(cases)
    if not isinstance(seed, str) or not seed.strip() or not 4 <= max_calls <= 1000:
        raise ValueError("候选不能为空，评估预算为 4 到 1000 次")
    reserve_output(output)
    records = []

    class Adapter:
        propose_new_texts = None

        def evaluate(self, batch, candidate, capture_traces=False):
            text = candidate.get("instruction")
            if (
                set(candidate) != {"instruction"}
                or not isinstance(text, str)
                or not 1 <= len(text) <= 32000
            ):
                raise ValueError("候选只能修改 instruction 正文")
            results = []
            for case in batch:
                result = evaluator(case, text)
                score = result.get("score")
                if (
                    type(score) not in (int, float)
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ValueError("评测分数必须为 0 到 1 的有限值")
                if result.get("violations"):
                    result = {**result, "score": 0.0}
                results.append(result)
                records.append(
                    {
                        "id": case["id"],
                        "split": case["split"],
                        "candidate_hash": hashlib.sha256(text.encode()).hexdigest(),
                        **result,
                    }
                )
            traces = [
                {
                    "Inputs": c["query"],
                    "Outputs": r.get("output"),
                    "Feedback": r.get("feedback"),
                }
                for c, r in zip(batch, results)
            ]
            return EvaluationBatch(
                outputs=results,
                scores=[r["score"] for r in results],
                trajectories=traces if capture_traces else None,
            )

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            return {key: eval_batch.trajectories for key in components_to_update}

    adapter = Adapter()
    reflection_errors = []

    def checked_reflection(messages):
        try:
            return reflection_lm(messages)
        except Exception as error:
            reflection_errors.append(type(error).__name__)
            raise

    try:
        result = optimize(
            seed_candidate={"instruction": seed},
            trainset=[c for c in cases if c["split"] == "train"],
            valset=[c for c in cases if c["split"] == "val"],
            adapter=adapter,
            max_metric_calls=max_calls,
            reflection_minibatch_size=min(2, sum(c["split"] == "train" for c in cases)),
            custom_candidate_proposer=proposer,
            reflection_lm=checked_reflection if reflection_lm else None,
            run_dir=str(output / "engine"),
            seed=20260918,
            use_wandb=False,
            use_mlflow=False,
            cache_evaluation=False,
            skip_perfect_score=False,
            raise_on_exception=True,
        )
        if reflection_errors:
            raise RuntimeError(
                "反思调用失败，不能把未产生有效候选算作优化完成："
                + ",".join(reflection_errors)
            )
        candidate = result.best_candidate["instruction"]
        # 留出仅在搜索结束后执行，按场景交错评估，不反馈给优化器。
        baseline_scores = []
        candidate_scores = []
        for i, case in enumerate(c for c in cases if c["split"] == "holdout"):
            variants = [("baseline", seed), ("candidate", candidate)]
            if i % 2:
                variants.reverse()
            pair = {
                name: adapter.evaluate([case], {"instruction": text}).scores[0]
                for name, text in variants
            }
            baseline_scores.append(pair["baseline"])
            candidate_scores.append(pair["candidate"])
        report = {
            "scope": "isolated_search_tool_harness",
            "metric": "tool_argument_constraint_preservation",
            "publication": "manual_review_required",
            "candidate": candidate,
            "candidate_changed": candidate != seed,
            "comparison_kind": "candidate_vs_baseline"
            if candidate != seed
            else "baseline_repeatability",
            "interpretation": "仍需完整 Agent 发布门禁"
            if candidate != seed
            else "正文未变化，分数差异仅代表重复生成波动，不是优化收益",
            "max_optimization_metric_calls": max_calls,
            "total_evaluations_including_holdout": len(records),
            "dataset_sha256": hashlib.sha256(
                json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "holdout": {
                "baseline_scores": baseline_scores,
                "candidate_scores": candidate_scores,
                "paired_mean_delta": sum(
                    b - a for a, b in zip(baseline_scores, candidate_scores)
                )
                / len(baseline_scores),
            },
            "full_agent_release_gate": "NOT_RUN",
            "records": records,
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        return report
    except BaseException as error:
        (output / "failure.json").write_text(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(error).__name__,
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise


class SearchEvaluator:
    """使用真实 AgentScope Agent + 当前 product_search_tool；隔离只读检索工具范围。"""

    def __init__(self, settings, runner, *, public_skill=False, base_prompt=""):
        from app.infrastructure.llm import create_chat_model

        self.reflection_usage = []
        self.runner = runner
        self.model = create_chat_model(settings, stream=False)
        self.public_skill, self.base_prompt = public_skill, base_prompt

    async def run(self, case, text):
        if self.public_skill and len(text) > 12000:
            return {
                "score": 0.0,
                "violations": ["Skill 超过注册表正文上限"],
                "feedback": "保持正文在 12000 字符以内",
                "usage": [],
            }
        from agentscope.agent import Agent, ReActConfig
        from agentscope.message import UserMsg
        from agentscope.tool import Toolkit, FunctionTool
        from app.application.tools.product_search_tool import build_product_search_tool
        from app.application.usecases.catalog_search import CatalogSearchUseCase
        from app.infrastructure.persistence.in_memory_repositories import (
            InMemoryProductRepository,
        )
        from app.infrastructure.eventbus import TradeEventBus, observe_run_events
        from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
        from app.infrastructure.context_usage import (
            context_usage_sink,
            context_call_kind,
        )

        usage = []
        events = []
        usage_token = context_usage_sink.set(usage.append)
        kind_token = context_call_kind.set("optimization_evaluation")
        context_token = ShoppingContext.set(
            ShoppingContextSnapshot(
                "gepa-" + case["id"], "gepa-isolated", "zh-CN", "CNY"
            )
        )
        try:
            tool = build_product_search_tool(
                CatalogSearchUseCase(InMemoryProductRepository(), hybrid_enabled=True),
                TradeEventBus(),
            )
            system = (
                self.base_prompt
                + "\n<reviewed-public-skill>\n"
                + text
                + "\n</reviewed-public-skill>"
                if self.public_skill
                else text
            )
            agent = Agent(
                name="offline_search",
                system_prompt=system,
                model=self.model,
                toolkit=Toolkit(tools=[FunctionTool(tool, is_read_only=True)]),
                react_config=ReActConfig(max_iters=3),
            )
            with observe_run_events(events.append):
                reply = await asyncio.wait_for(
                    agent.reply(UserMsg("buyer", case["query"])), timeout=90
                )
            calls = [
                e.payload.get("args", {})
                for e in events
                if e.type == "tool.invoke"
                and e.payload.get("tool") == "product_search_tool"
            ]
            violations = []
            if not calls:
                violations.append("没有执行商品查询")
            for args in calls:
                for key, value in case["expected"].items():
                    if key == "query_contains":
                        if not all(
                            word in args.get("normalized_query", "") for word in value
                        ):
                            violations.append("丢失商品身份词")
                    elif (
                        set(args.get(key) or []) != set(value)
                        if key.endswith("_material_tags") and isinstance(value, list)
                        else args.get(key) != value
                    ):
                        violations.append(
                            f"{key} 未保留需求：预期 {value!r}，得到 {args.get(key)!r}"
                        )
            return {
                "score": 0.0 if violations else 1.0,
                "violations": violations,
                "feedback": "；".join(violations) or "查询参数完整保留买家约束",
                "output": {"tool_calls": calls},
                "usage": usage,
            }
        finally:
            ShoppingContext.reset(context_token)
            context_usage_sink.reset(usage_token)
            context_call_kind.reset(kind_token)

    def __call__(self, case, text):
        return self.runner.run(self.run(case, text))

    def reflect(self, messages):
        from agentscope.message import Msg, TextBlock
        from app.infrastructure.context_usage import (
            context_call_kind,
            context_usage_sink,
        )

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        async def call():
            usage_token = context_usage_sink.set(self.reflection_usage.append)
            token = context_call_kind.set("optimization_reflection")
            try:
                response = await self.model(
                    [
                        Msg(
                            name=m.get("role", "user"),
                            role=m.get("role", "user"),
                            content=[TextBlock(text=m["content"])],
                        )
                        for m in messages
                    ]
                )
                return "".join(
                    block.text
                    for block in response.content
                    if isinstance(block, TextBlock)
                )
            finally:
                context_call_kind.reset(token)
                context_usage_sink.reset(usage_token)

        return self.runner.run(call())


def skill_candidate(source, body, report_path):
    from app.infrastructure.capability_registry import validate

    original = validate(source)
    if original["kind"] != "skill":
        raise ValueError("此入口只优化公共 Skill 正文")
    result = {
        **original,
        "body": body,
        "version": "gepa-" + hashlib.sha256(body.encode()).hexdigest()[:16],
        "evidence": [
            *original["evidence"][:11],
            {
                "reference": str(report_path),
                "summary": "离线搜索参数约束评测；不是全 Agent 发布证明，仍需人工审核。",
            },
        ],
    }
    return validate(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", type=Path, default=Path("eval/optimization_search.jsonl")
    )
    parser.add_argument(
        "--prompt", type=Path, default=Path("app/application/prompts/shoppilot.yml")
    )
    parser.add_argument(
        "--public-skill",
        type=Path,
        help="公共 Skill 规范 JSON，仅优化 body，不连接个人 Skill 数据库",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=24)
    args = parser.parse_args()
    from app.infrastructure.settings import load_settings
    from app.infrastructure.prompt_registry import read_prompt

    document, source_hash = read_prompt(args.prompt)
    seed = document["sub_agents"]["search"]["system_prompt"]
    skill_source = None
    if args.public_skill:
        from app.infrastructure.capability_registry import validate

        skill_source = validate(json.loads(args.public_skill.read_text()))
        if skill_source["kind"] != "skill":
            parser.error("--public-skill 仅支持 kind=skill")
        seed = skill_source["body"]
    cases = [
        json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()
    ]
    with asyncio.Runner() as runner:
        evaluator = SearchEvaluator(
            replace(
                load_settings(),
                data_dir=args.output / "isolated",
                semantic_cache_enabled=False,
            ),
            runner,
            public_skill=bool(args.public_skill),
            base_prompt=document["sub_agents"]["search"]["system_prompt"],
        )
        report = optimize_candidate(
            seed,
            cases,
            evaluator,
            args.output,
            max_calls=args.max_calls,
            reflection_lm=evaluator.reflect,
        )
    report["reflection_usage"] = evaluator.reflection_usage
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    if args.public_skill:
        (args.output / "candidate-skill.json").write_text(
            json.dumps(
                skill_candidate(
                    skill_source, report["candidate"], args.output / "report.json"
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        document["sub_agents"]["search"]["system_prompt"] = report["candidate"]
        (args.output / "candidate.yml").write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        )
        read_prompt(args.output / "candidate.yml")
    (args.output / "source.json").write_text(
        json.dumps(
            {
                "prompt_sha256": source_hash,
                "skill_sha256": hashlib.sha256(
                    args.public_skill.read_bytes()
                ).hexdigest()
                if args.public_skill
                else None,
                "scope": "search_only",
                "model": load_settings().llm_model,
            }
        )
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"candidate", "records"}},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
