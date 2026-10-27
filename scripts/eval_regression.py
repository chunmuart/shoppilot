# -*- coding: utf-8 -*-
"""评测回归脚本

用法（先启动服务）：
    uv run uvicorn app.presentation.server:app --port 8000
    uv run python scripts/eval_regression.py [--cases eval/cases.yaml] [--only case_id]

流程：逐 case 顺序打 POST /commerce/intents（同 case 多轮复用会话）→
LLM judge 按 Rubric（P0 数字事实 / P1 行为命中 / P2 表达）逐条打分 →
输出 eval/report-{时间戳}.md。

case 可选 prior_context 字段：告知 judge 本会话之前已成立的事实（如跨会话写入的长期偏好），
否则 judge 只看本会话 transcript，会把"正确应用了历史偏好"误判为"无据添加"。

评分口径：P0 任一不过 = 该 case 直接 FAIL；总分 = P0 0.5 / P1 0.35 / P2 0.15 加权。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.infrastructure.transient import is_transient_error  # noqa: E402
from app.infrastructure.runtime_version import app_source_fingerprint  # noqa: E402
from scripts.eval.contracts import JudgeOutputError, validate_judge_output  # noqa: E402
from scripts.eval.evidence import evaluate_trace_assertions  # noqa: E402
from scripts.eval.metrics_evidence import collect_case_metrics, release_metrics
from time import perf_counter
from scripts.eval.http_actions import BuyerAPIClient, capture_confirmations, execute_confirmation_action, validate_http_actions  # noqa: E402
from scripts.eval.run_manifest import (  # noqa: E402
    SPLITS, build_manifest, finish_manifest, manifest_report, public_endpoint, select_cases, write_manifest,
)

BASE_URL = "http://127.0.0.1:8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUN_NAMESPACE = uuid.uuid4().hex[:10]

# judge 不经模型层闸门（直连 httpx），自己退避重试，避免主模型限流时整轮评测报废
_JUDGE_MAX_RETRIES = 3
_JUDGE_RETRY_BASE_SECONDS = 8.0

JUDGE_SYSTEM_PROMPT = """你是严格的电商 Agent 评测员。给你一段"买家多轮提问与 Agent 回复"的对话记录、
商品库事实表（ground truth）、本轮真实工具返回事实，以及分级评分细则
（P0 数字事实与安全底线 / P1 行为与命中 / P2 表达）。
部分 case 会额外给出"会话前置事实"（如买家在早先会话里已写入的长期偏好）：这些事实真实有效，
即使它不出现在本段对话记录里，Agent 引用或应用它也不算编造。
逐条判断细则是否满足：静态商品属性以商品库事实表为基准；到手价、运费、关税、订单状态等运行时事实
以本轮真实工具返回为权威依据，Agent 原样使用工具返回不能判为编造。行为类细则以对话记录与会话前置事实为依据，
拿不准按不通过处理。
每条细则先在 reason 里完成推理，再给出 pass 定论；pass 必须与 reason 的最终结论一致。
每条 reason 必须以“结论：通过”或“结论：不通过”结束，且一个 criterion 只能输出一次。
只输出 JSON（字段顺序固定：criterion → reason → pass）：
{"p0": [{"criterion": "...", "reason": "...", "pass": true/false}],
 "p1": [...], "p2": [...]}"""


def build_ground_truth() -> str:
    """从种子商品数据与汇率表生成事实表，供 judge 校验数字事实。"""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from app.domain.catalog.exchange_rate import ExchangeRateTable
    from app.infrastructure.persistence.seed_products import build_seed_products

    products = build_seed_products()

    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "## 商品属性事实表",
        "| product_id | 标题 | 品牌 | 品类 | 产地 | 材质标签 | 重量kg | 尺寸cm | 配送国家 | 亮点 |",
        "|---|---|---|---|---|---|---:|---|---|---|",
    ]
    for product in products:
        highlights = "; ".join(
            f"{highlight.label}:{highlight.detail}"
            for highlight in product.highlights
        )
        lines.append(
            f"| {cell(product.product_id)} | {cell(product.title)} | {cell(product.brand)} "
            f"| {cell(product.category)} | {cell(product.origin_country)} "
            f"| {cell(', '.join(product.material_tags))} | {product.weight_kg} "
            f"| {cell(json.dumps(product.dimensions_cm, ensure_ascii=False, sort_keys=True))} "
            f"| {cell(', '.join(product.ships_to))} | {cell(highlights)} |",
        )

    lines.extend([
        "",
        "## SKU 价格与库存事实表",
        "| product_id | sku | 价格 | 库存 |",
        "|---|---|---|---:|",
    ])
    for product in products:
        for sku in product.skus:
            lines.append(
                f"| {cell(product.product_id)} | {cell(f'{sku.sku_id}({sku.spec})')} "
                f"| {cell(sku.price)} | {sku.stock} |",
            )
    rates = ", ".join(f"1 {cur} = {rate} CNY" for cur, rate in ExchangeRateTable().rates_to_cny.items())
    lines.append("")
    lines.append(f"系统汇率表（到手价工具按此折算目标币种，折算后的价格属于工具返回，不算自行估算）：{rates}")
    return "\n".join(lines)


def build_tool_fact_evidence(events: list[dict]) -> str:
    """只提取真实工具结果，供 Judge 对账运行时价格、库存与订单状态。"""
    results = [
        event.get("payload") or {}
        for event in events
        if event.get("type") in {"tool.result", "eval.http_action.result", "eval.order.snapshot"}
    ]
    return json.dumps(results, ensure_ascii=False, sort_keys=True)


async def call_judge(
    client: httpx.AsyncClient,
    transcript: str,
    rubric: dict,
    ground_truth: str,
    prior_context: str = "",
    tool_fact_evidence: str = "[]",
) -> dict:
    prior_block = f"## 会话前置事实\n{prior_context}\n\n" if prior_context else ""
    payload = {
        # judge 可独立指定模型：主模型切新版/被限流时，评分基准不跟着飘
        "model": os.environ.get("EVAL_JUDGE_MODEL") or os.environ.get("LLM_MODEL", "qwen-plus"),
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"## 商品库事实表\n{ground_truth}\n\n"
                    f"## 本轮真实工具返回事实\n{tool_fact_evidence}\n\n"
                    f"{prior_block}"
                    f"## 对话记录\n{transcript}\n\n"
                    f"## 评分细则\n{json.dumps(rubric, ensure_ascii=False, indent=2)}"
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    last_error: Exception | None = None
    for attempt in range(_JUDGE_MAX_RETRIES):
        try:
            response = await client.post(
                f"{os.environ['LLM_BASE_URL'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            raw_content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(raw_content)
        except Exception as err:  # noqa: BLE001
            if not is_transient_error(err) or attempt == _JUDGE_MAX_RETRIES - 1:
                raise
            last_error = err
            delay = _JUDGE_RETRY_BASE_SECONDS * (2**attempt)
            print(f"   judge 遇限流，{delay:.0f}s 后重试：{err}", flush=True)
            await asyncio.sleep(delay)
            continue

        try:
            return validate_judge_output(parsed, rubric)
        except JudgeOutputError as err:
            last_error = err
            if attempt == _JUDGE_MAX_RETRIES - 1:
                raise
            print(f"   judge 输出协议不合法，要求重答：{err}", flush=True)
            payload = {
                **payload,
                "messages": [
                    *payload["messages"],
                    {"role": "assistant", "content": raw_content},
                    {
                        "role": "user",
                        "content": (
                            f"上次输出不满足协议：{err}。请修正后重新输出完整 JSON；"
                            "不得删减 criterion，也不得省略 reason 末尾的显式结论。"
                        ),
                    },
                ],
            }
    raise last_error if last_error else RuntimeError("judge 重试耗尽")


def score_case(judged: dict, rubric: dict) -> tuple[float, bool]:
    """返回 (加权得分, p0全过)。Judge 输出必须先满足完整契约。"""
    judged = validate_judge_output(judged, rubric)

    def ratio(items: list) -> float:
        return sum(1 for item in items if item.get("pass")) / len(items) if items else 1.0

    p0_ratio, p1_ratio, p2_ratio = ratio(judged.get("p0", [])), ratio(judged.get("p1", [])), ratio(judged.get("p2", []))
    weighted = 0.5 * p0_ratio + 0.35 * p1_ratio + 0.15 * p2_ratio
    return round(weighted, 3), p0_ratio == 1.0


def build_judge_rubric(rubric: dict, assertions_by_level: dict | None) -> dict:
    """从 Judge 输入中移除可由 trace 直接证明的条目，避免重复裁判和随机翻转。"""
    reduced: dict[str, list[str]] = {}
    for level in ("p0", "p1", "p2"):
        deterministic = {
            assertion.get("criterion")
            for assertion in (assertions_by_level or {}).get(level, [])
        }
        unknown = deterministic - set(rubric.get(level, []))
        if unknown:
            raise JudgeOutputError(f"deterministic.{level} 的 criterion 不在 rubric 中：{sorted(unknown)}")
        reduced[level] = [criterion for criterion in rubric.get(level, []) if criterion not in deterministic]
    return reduced


def apply_trace_evidence(
    judged: dict,
    rubric: dict,
    assertions_by_level: dict | None,
    events: list[dict],
) -> dict:
    """用可观测工具/状态事件覆盖同名 Judge 条目，保留非程序化表达评估。"""
    judge_rubric = build_judge_rubric(rubric, assertions_by_level)
    try:
        # 兼容旧调用方传入包含确定性条目的完整 Judge 输出。
        normalized = validate_judge_output(judged, rubric)
    except JudgeOutputError:
        normalized = validate_judge_output(judged, judge_rubric)
    for level, assertions in (assertions_by_level or {}).items():
        if level not in {"p0", "p1", "p2"} or not isinstance(assertions, list):
            raise JudgeOutputError("deterministic 只能包含 p0/p1/p2 断言列表")
        deterministic_items = evaluate_trace_assertions(assertions, events)
        replacements = {item["criterion"]: item for item in deterministic_items}
        unknown = replacements.keys() - set(rubric[level])
        if unknown:
            raise JudgeOutputError(f"deterministic.{level} 的 criterion 不在 rubric 中：{sorted(unknown)}")
        judged_by_criterion = {item["criterion"]: item for item in normalized[level]}
        normalized[level] = [
            replacements.get(criterion) or judged_by_criterion[criterion]
            for criterion in rubric[level]
        ]
    return validate_judge_output(normalized, rubric)


def deterministic_p0_p1_pass(
    resolved: dict,
    assertions_by_level: dict | None,
) -> bool:
    """确定性 P0/P1 是硬门禁，不允许被其他评分项的分数抵消。"""
    for level in ("p0", "p1"):
        criteria = {
            assertion["criterion"]
            for assertion in (assertions_by_level or {}).get(level, [])
        }
        if any(
            item["criterion"] in criteria and not item.get("pass")
            for item in resolved.get(level, [])
        ):
            return False
    return True


def verify_fixed_trace_stability(
    judged: dict,
    rubric: dict,
    assertions_by_level: dict | None,
    events: list[dict],
    *,
    repeats: int = 5,
) -> tuple[float, bool, str]:
    """同一份 Judge 输出与 trace 重放必须得到同一个终态。"""
    if repeats < 2:
        raise ValueError("稳定性重放次数至少为 2")
    verdicts: set[tuple[float, bool, str]] = set()
    for _ in range(repeats):
        resolved = apply_trace_evidence(judged, rubric, assertions_by_level, events)
        score, p0_pass = score_case(resolved, rubric)
        hard_evidence_pass = deterministic_p0_p1_pass(resolved, assertions_by_level)
        verdicts.add(
            (score, p0_pass, "PASS" if p0_pass and hard_evidence_pass and score >= 0.7 else "FAIL"),
        )
    if len(verdicts) != 1:
        raise JudgeOutputError(f"固定 trace 重放出现 PASS/FAIL 翻转：{sorted(verdicts)}")
    return next(iter(verdicts))


def exit_code_for_results(results: list[dict]) -> int:
    """只有非空且全部通过的回归结果才返回成功退出码。"""
    return 0 if results and all(result.get("verdict") == "PASS" for result in results) else 1


class SessionEventCollector:
    """订阅与请求同一个会话的 WebSocket 事件，供回归评测保留行为证据。"""

    def __init__(self, session_id: str, buyer_id: str, token: str | None = None) -> None:
        self._session_id = session_id
        self._buyer_id = buyer_id
        self._token = token
        self.events: list[dict] = []
        self._websocket = None
        self._receiver: asyncio.Task | None = None

    async def __aenter__(self) -> "SessionEventCollector":
        import websockets

        event_url = f"{BASE_URL.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1)}/commerce/events"
        protocols = ["shoppilot-events"]
        if self._token:
            protocols.append(f"shoppilot-auth.{self._token}")
        self._websocket = await websockets.connect(event_url, open_timeout=10, subprotocols=protocols)
        await self._websocket.send(json.dumps({"shopping_session_id": self._session_id, "buyer_id": self._buyer_id}))

        async def receive() -> None:
            try:
                async for message in self._websocket:
                    self.events.append(json.loads(message))
            except asyncio.CancelledError:
                raise

        self._receiver = asyncio.create_task(receive())
        # 服务端先完成订阅再开始请求，避免首个 tool.invoke 因连接竞态丢失。
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        # /commerce/intents 返回前已完成工具调用；留出一个调度周期接收队列尾部事件。
        await asyncio.sleep(0.05)
        if self._receiver:
            self._receiver.cancel()
            try:
                await self._receiver
            except asyncio.CancelledError:
                pass
        if self._websocket:
            await self._websocket.close()


class CaseExecutionError(RuntimeError):
    def __init__(self, error: Exception, transcript: str, events: list[dict]) -> None:
        super().__init__(f"{type(error).__name__}: {error}")
        self.transcript = transcript
        self.trace_events = list(events)


async def run_case(client: httpx.AsyncClient, case: dict, ground_truth: str) -> dict:
    actions = validate_http_actions(case)
    session_id = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    buyer_id = f"{case.get('buyer_id') or 'eval-buyer-' + case['id']}-{_RUN_NAMESPACE}"
    token = None
    if os.getenv("IDENTITY_MODE", "demo") == "hmac":
        from app.infrastructure.identity import IdentityPolicy
        token = IdentityPolicy(mode="hmac", secret=os.getenv("IDENTITY_HMAC_SECRET", "")).issue(buyer_id, ttl_seconds=3600)
    api_client = BuyerAPIClient(client, BASE_URL, token)
    transcript_lines: list[str] = []
    collector = SessionEventCollector(session_id, buyer_id, token)
    try:
        return await _run_case_with_events(api_client, client, case, ground_truth, session_id, buyer_id, transcript_lines, collector, actions)
    except Exception as err:
        raise CaseExecutionError(err, "\n\n".join(transcript_lines), collector.events) from err


async def _run_case_with_events(client, judge_client, case, ground_truth, session_id, buyer_id, transcript_lines, collector, actions):
    inspect_confirmations = bool(actions) or any(assertion.get("kind") == "no_orders_before_http_confirmation"
                                                for assertions in (case.get("deterministic") or {}).values() for assertion in assertions)
    last_order_id = None
    case_started = perf_counter()
    async with collector:
        for turn_index, query in enumerate(case["queries"], start=1):
            if "{{confirmed_order_id}}" in query:
                if last_order_id is None:
                    raise ValueError("后续查询需要已真实确认的订单号，不能由模型猜测")
                query = query.replace("{{confirmed_order_id}}", last_order_id)
            response = await client.post(
                f"{BASE_URL}/commerce/intents",
                json={
                    "shopping_session_id": session_id,
                    "buyer_id": buyer_id,
                    "locale": "zh-CN",
                    "currency": "CNY",
                    "raw_query": query,
                },
                timeout=600,
            )
            response.raise_for_status()
            final_text = response.json()["final_text"]
            transcript_lines.append(f"[买家] {query}\n[Agent] {final_text}")
            # 将真实事件按对话轮次切开，才能程序证明“确认前”没有调用下单工具。
            await asyncio.sleep(0.05)
            collector.events.append({"type": "eval.turn.complete", "payload": {"turn_index": turn_index}})
            if inspect_confirmations:
                confirmations = await capture_confirmations(client, BASE_URL, buyer_id, session_id, collector.events, turn_index)
                for action in actions:
                    if action["after_turn"] != turn_index:
                        continue
                    if action["expected_payload"].get("order_id") == "{{confirmed_order_id}}":
                        if last_order_id is None:
                            raise ValueError("取消确认必须绑定先前真实 HTTP 确认返回的订单号")
                        action = {**action, "expected_payload": {**action["expected_payload"], "order_id": last_order_id}}
                    resolved = await execute_confirmation_action(client, BASE_URL, buyer_id, session_id, action, confirmations, collector.events)
                    if resolved.get("order"):
                        last_order_id = resolved["order"]["order_id"]
                    transcript_lines.append(f"[评测模拟用户页面点击] action={action['action']}，approved={action['approved']}；服务端决议={resolved['confirmation']['status']}，意向单状态={(resolved.get('order') or {}).get('status', '未创建')}。")
                    confirmations = await capture_confirmations(client, BASE_URL, buyer_id, session_id, collector.events, turn_index)

    case_metrics = collect_case_metrics(collector.events, expected_turns=len(case["queries"]), elapsed_ms=(perf_counter() - case_started) * 1000)
    transcript = "\n\n".join(transcript_lines)
    judge_rubric = build_judge_rubric(case["rubric"], case.get("deterministic"))
    if any(judge_rubric[level] for level in ("p0", "p1", "p2")):
        judged = await call_judge(
            judge_client,
            transcript,
            judge_rubric,
            ground_truth,
            case.get("prior_context", ""),
            build_tool_fact_evidence(collector.events),
        )
    else:
        judged = {"p0": [], "p1": [], "p2": []}
    score, p0_all_pass, verdict = verify_fixed_trace_stability(
        judged, case["rubric"], case.get("deterministic"), collector.events,
    )
    judged = apply_trace_evidence(judged, case["rubric"], case.get("deterministic"), collector.events)
    return {
        "id": case["id"],
        "metrics": case_metrics,
        "description": case["description"],
        "score": score,
        "p0_pass": p0_all_pass,
        "verdict": verdict,
        "judged": judged,
        "transcript": transcript,
        "trace_events": collector.events,
        "buyer_id": buyer_id,
        "session_id": session_id,
    }


def render_report(results: list[dict]) -> str:
    lines = [
        f"# ShopPilot 评测回归报告（{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
        f"总览：{sum(1 for r in results if r['verdict'] == 'PASS')}/{len(results)} PASS，"
        f"平均分 {sum(r['score'] for r in results) / len(results):.3f}",
        "",
        "| case | 描述 | 得分 | P0 | 结果 |",
        "|------|------|------|-----|------|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['description']} | {r['score']} | "
            f"{'通过' if r['p0_pass'] else '不通过'} | {r['verdict']} |",
        )
    lines.append("")
    for r in results:
        lines.append(f"## {r['id']}（{r['verdict']}，{r['score']}）")
        for level in ("p0", "p1", "p2"):
            for item in r["judged"].get(level, []):
                mark = "PASS" if item.get("pass") else "FAIL"
                lines.append(f"- [{level.upper()}][{mark}] {item['criterion']}：{item.get('reason', '')}")
        lines.append("")
        lines.append("<details><summary>对话记录</summary>\n")
        lines.append(r["transcript"])
        lines.append("\n</details>\n")
        if r.get("trace_events"):
            invoked = [
                event.get("payload", {}).get("tool")
                for event in r["trace_events"]
                if event.get("type") == "tool.invoke"
            ]
            lines.append(f"工具证据：{', '.join(tool for tool in invoked if tool) or '无'}\n")
    return "\n".join(lines)


async def _guard_semantic_cache(allow: bool, *, strict: bool = False) -> dict:
    """语义缓存开着时拒绝跑回归。

    实测踩过：一条 case 的错误回复进了缓存，之后改 prompt 重跑，回复一字不差——
    评测彻底失去了检验能力。回归必须评 Agent 真实行为。
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{BASE_URL}/health")
            response.raise_for_status()
            health = response.json()
        if not isinstance(health, dict) or type(health.get("semantic_cache")) is not bool:
            raise ValueError("/health 缺少明确的 semantic_cache 布尔状态")
    except Exception as err:  # noqa: BLE001 —— 拿不到 health 不阻断，后续请求自会报错
        if strict:
            raise RuntimeError(f"release 前置检查失败，无法证明服务和缓存状态：{err}") from err
        print(f"警告：无法读取 /health（{err}），跳过缓存检查", flush=True)
        return {"status": "UNVERIFIED", "semantic_cache": None}
    if health.get("semantic_cache") and not allow:
        raise SystemExit(
            "拒绝跑回归：服务端语义缓存处于开启状态，评分会变成评缓存。\n"
            "请用 SEMANTIC_CACHE_ENABLED=0 重启服务后重试，例如：\n"
            "  SEMANTIC_CACHE_ENABLED=0 docker compose -f docker/docker-compose.yaml up -d app worker\n"
            "确认要带缓存跑则加 --allow-semantic-cache。",
        )
    # 仅记录健康端点已有的白名单字段，不复制 URL、密钥或完整服务配置。
    result = {key: health[key] for key in ("status", "model", "semantic_cache", "database", "redis", "queue") if key in health}
    if isinstance(health.get("runtime"), dict):
        result["runtime"] = {"app_source_sha256": health["runtime"].get("app_source_sha256")}
    if isinstance(health.get("prompt_registry"), dict):
        result["prompt_registry"] = {key: health["prompt_registry"].get(key) for key in
            ("deployment_id", "baseline", "candidate", "candidate_bps", "experiment", "pinned_version", "effective_version")}
    return result


def compare_runtime_identity(local_app_hash: str, health: dict) -> dict:
    runtime = health.get("runtime") or {}
    remote_hash = runtime.get("app_source_sha256") if isinstance(runtime, dict) else None
    if not isinstance(remote_hash, str) or len(remote_hash) != 64 or any(char not in "0123456789abcdef" for char in remote_hash):
        remote_hash = None
    matching = local_app_hash == remote_hash if remote_hash is not None else None
    return {
        "local_app_source_sha256": local_app_hash, "server_app_source_sha256": remote_hash,
        "matching": matching, "status": "unverified" if matching is None else "matched" if matching else "mismatch",
        "scope": "app 源文件相对路径与内容；服务端在进程启动时计算，不证明依赖、数据卷或二进制字节一致",
    }


def actual_trace_strategies(results: list[dict]) -> dict[str, list[str]]:
    """只采信运行时工具结果；未观察到的召回链不从配置猜测。"""
    return {
        result["id"]: sorted({
            str((event.get("payload") or {})["recall_strategy"])
            for event in result.get("trace_events", [])
            if event.get("type") == "tool.result" and (event.get("payload") or {}).get("recall_strategy")
        })
        for result in results
    }


async def main(argv: list[str] | None = None) -> None:
    global BASE_URL, _RUN_NAMESPACE
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(PROJECT_ROOT / "eval" / "cases.yaml"))
    parser.add_argument("--only", default=None, help="只跑指定 case id")
    parser.add_argument("--split", choices=SPLITS, default="all", help="仅执行该 split；--only 进一步缩小后属于诊断，不代表完整 release")
    parser.add_argument("--dry-run", action="store_true", help="只校验选集并写 NOT_RUN 证据，不请求 Agent、Judge 或健康端点")
    parser.add_argument("--base-url", default=BASE_URL, help="评测服务地址，正式评测应使用独立数据目录启动的实例")
    parser.add_argument(
        "--report-dir",
        default=str(PROJECT_ROOT / "eval"),
        help="报告输出目录（默认写入项目 eval 目录）",
    )
    parser.add_argument(
        "--allow-semantic-cache",
        action="store_true",
        help="允许在语义缓存开启的环境下跑（不推荐，评的会是缓存而不是 Agent）",
    )
    args = parser.parse_args(argv)
    BASE_URL = args.base_url.rstrip("/")
    _RUN_NAMESPACE = uuid.uuid4().hex[:10]
    try:
        raw = yaml.safe_load(Path(args.cases).read_text(encoding="utf-8"))
        cases, selection = select_cases(raw["cases"], args.split, args.only)
        for case in cases:
            validate_http_actions(case)
    except (ValueError, OSError, KeyError, TypeError) as err:
        parser.error(str(err))
    if args.split == "release" and args.allow_semantic_cache:
        parser.error("release 不允许使用 --allow-semantic-cache")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"agent-{args.split}-report-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.md"
    manifest = build_manifest(
        runner="agent_regression", dataset=Path(args.cases), selection=selection, judge_prompt=JUDGE_SYSTEM_PROMPT,
        parameters={"base_url": public_endpoint(BASE_URL), "allow_semantic_cache": args.allow_semantic_cache,
                    "dry_run": args.dry_run, "score_threshold": 0.7,
                    "buyer_namespace": _RUN_NAMESPACE,
                    "identity_mode": os.getenv("IDENTITY_MODE", "demo"),
                    "gate_scope": "release" if args.split == "release" and selection["complete_split"] and not args.only else "diagnostic",
                    "server_code_identity": "见 service_runtime；本地工作区 hash 不自动代表被测服务版本"},
    )
    local_app_hash = app_source_fingerprint()
    manifest["service_runtime"] = compare_runtime_identity(local_app_hash, {})
    if args.dry_run:
        path = write_manifest(manifest, report_path)
        print(f"仅校验选集：split={args.split}，{len(cases)} 条；NOT_RUN，不代表通过。证据：{path}")
        return
    try:
        health = await _guard_semantic_cache(args.allow_semantic_cache, strict=args.split == "release")
    except (Exception, SystemExit) as err:
        finish_manifest(manifest, actual_strategies={}, gate="BLOCK", status="ERROR", preflight_error=str(err))
        write_manifest(manifest, report_path)
        report_path.write_text("# Agent 回归前置检查失败\n" + manifest_report(manifest), encoding="utf-8")
        raise SystemExit(1) from err
    manifest["server_health"] = health
    manifest["service_runtime"] = compare_runtime_identity(local_app_hash, health)

    results = []
    ground_truth = build_ground_truth()
    async with httpx.AsyncClient() as client:
        for case in cases:  # 顺序执行：memory-recall 依赖 memory-write
            print(f"== 评测 {case['id']} ...", flush=True)
            try:
                result = await run_case(client, case, ground_truth)
            except Exception as err:  # noqa: BLE001 —— 单条失败不中断整轮回归
                result = {
                    "id": case["id"], "description": case["description"],
                    "score": 0.0, "p0_pass": False, "verdict": "ERROR",
                    "judged": {}, "transcript": (err.transcript + "\n\n" if isinstance(err, CaseExecutionError) else "") + f"执行异常：{err}",
                    "trace_events": err.trace_events if isinstance(err, CaseExecutionError) else [],
                }
            print(f"   -> {result['verdict']}（{result['score']}）", flush=True)
            results.append(result)
            manifest["execution"] = {"status": "RUNNING", "gate": "INCOMPLETE", "actual_strategies": actual_trace_strategies(results), "results": list(results), "completed_count": len(results)}
            write_manifest(manifest, report_path)

    result_code = exit_code_for_results(results)
    stable = finish_manifest(
        manifest, actual_strategies=actual_trace_strategies(results),
        gate="PASS" if result_code == 0 else "BLOCK",
        status="ERROR" if any(result["verdict"] == "ERROR" for result in results) else "COMPLETED",
        results=results,
        release_metrics=release_metrics(results),
        observed_model_fallbacks=[event["payload"] for result in results for event in result.get("trace_events", []) if event.get("type") == "model.fallback"],
    )
    write_manifest(manifest, report_path)
    report = render_report(results) + manifest_report(manifest)
    report_path.write_text(report, encoding="utf-8")
    print(f"\n报告已写入：{report_path}")
    print(report.split("\n\n")[1])
    print(f"最终门禁：{manifest['execution']['gate']}")
    if not stable:
        print("阻断原因：运行期间代码或数据输入发生变化，需在固定版本上重跑。")
    raise SystemExit(result_code if stable else 1)


if __name__ == "__main__":
    asyncio.run(main())
