"""裁剪/BP机制实验：默认只跑诊断；不修改生产配置，不执行模型返回的工具。

python -m scripts.eval.harness.pruning_experiment --output <新目录>
"""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.tool import ToolChoice
from app.infrastructure.context_usage import context_usage_sink, evaluation_evidence_sink
from app.infrastructure.llm import create_chat_model
from app.infrastructure.prompt_cache import (
    cache_attempt, mark_messages, normalized_cache_messages, PruneAwareBoundaries,
)
from app.infrastructure.settings import load_settings
from app.infrastructure.throttle import GatewayThrottle
from scripts.eval.harness.contracts import assert_output_path, source_manifest, write_json, digest
from scripts.eval.harness.pruning_fixture import CASES, capture
from scripts.eval.harness.pruning_report import render

ARMS = ('A', 'B', 'C')
ORDERS = ('ABC', 'ACB', 'BAC', 'BCA', 'CAB', 'CBA')
NONCE_PREFIX = '实验隔离标识：'


def canonical(request, nonce):
    result = deepcopy(request)
    result['messages'] = normalized_cache_messages(result['messages'])
    first = result['messages'][0]['content'][0]
    prefix = NONCE_PREFIX + nonce + '\n'
    if not first['text'].startswith(prefix):
        raise ValueError('隔离标识缺失或被移动')
    first['text'] = first['text'][len(prefix):]
    return result


def request_for(frame, arm, nonce, selector, now):
    body = deepcopy(frame['request'])
    body['messages'] = normalized_cache_messages(body['messages'])
    body['messages'][0]['content'][0]['text'] = NONCE_PREFIX + nonce + '\n' + body['messages'][0]['content'][0]['text']
    records = []
    if arm == 'B':
        body['messages'], _ = mark_messages(body['messages'], 'static_history')
    elif arm == 'C':
        body['messages'], records = selector.select(body, frame['mutable_ids'], now=now)
    expected = deepcopy(frame['request'])
    expected['messages'] = normalized_cache_messages(expected['messages'])
    if canonical(body, nonce) != expected:
        raise ValueError('实验组除标记与注册nonce之外改变了输入')
    return body, records


def answer_checks(answer, expected):
    try:
        data = json.loads(answer)
    except ValueError:
        data = None
    return {'json': isinstance(data, dict), **{key: isinstance(data, dict) and data.get(key) == value
            for key, value in expected.items()}}


class FrozenFormatter(OpenAIChatFormatter):
    frozen: list[dict]
    mode: str = 'passthrough'
    policy: str = 'static'

    async def format(self, msgs):
        result = deepcopy(self.frozen)
        attempt = cache_attempt.get()
        if attempt is not None:
            attempt.marker_count = sum('cache_control' in b for m in result for b in m.get('content') or []
                                       if isinstance(b, dict))
            attempt.reason = 'controlled_replay' if attempt.marker_count else 'passthrough'
        return result


async def call_stream(model, frame, body, native_messages, identity, output, rows, selector=None, records=()):
    """复用原生预算/协议/计量，但绕过显式参数拒绝重试；一次步骤只发一个HTTP请求。"""
    row = {**identity, 'checks': {}, 'error': None, 'usage': [], 'answer': '',
           'markers': list(records), 'archived_results': frame['archived_results'],
           'input_side_cost': None, 'total_cost': None, 'tools_executed': 0}
    request_path = output / 'requests' / ('-'.join(str(identity[k]) for k in ('phase', 'case', 'arm', 'repetition', 'step')) + '.json')
    model.formatter = FrozenFormatter(frozen=body['messages'],
        mode='passthrough' if identity['arm'] == 'A' else 'explicit',
        policy='prune_aware' if identity['arm'] == 'C' else 'static_history')
    seen = []
    def evidence(event):
        if event['kind'] != 'model_request':
            return
        actual = event['payload']
        seen.append(actual)
        write_json(request_path, actual)
        # 该位置位于官方SDK的HTTP调用之前，确认最终工具定义/选择也相同。
        if actual != body:
            raise ValueError('最终HTTP序列化与冻结请求不一致')
        if len(seen) != 1:
            raise ValueError('禁止模型隐式重试')
        if selector is not None:
            selector.record_sent(records, now=time.monotonic(), request=actual)
    usage_token = context_usage_sink.set(row['usage'].append)
    evidence_token = evaluation_evidence_sink.set(evidence)
    start = time.monotonic()
    stream = None
    async def consume():
        nonlocal stream
        try:
            async with asyncio.timeout(45):
                # 不走 __call__ 的 cache_control 拒绝后自动重试路径。
                stream = await model._call_throttled(native_messages, tools=body['tools'],
                    tool_choice=ToolChoice(mode=body['tool_choice']))
                async for part in stream:
                    if any(isinstance(b, ToolCallBlock) for b in part.content):
                        row['error'] = 'unexpected_tool_call'
                    text = ''.join(b.text for b in part.content if isinstance(b, TextBlock))
                    row['answer'] = text if part.is_last else row['answer'] + text
                    yield part
        except Exception as error:
            row['error'] = getattr(error, 'code', type(error).__name__)
            raise
        finally:
            if stream is not None:
                await stream.aclose()
            row['elapsed_ms'] = (time.monotonic() - start) * 1000
            row['checks'] = answer_checks(row['answer'], frame['expected'])
            row['wire_requests'] = len(seen)
            row['request_file'] = str(request_path.relative_to(output))
            row['passed'] = row['error'] is None and all(row['checks'].values()) and len(seen) == 1
            if seen:
                row['marker_indexes'] = [i for i, m in enumerate(seen[0]['messages'])
                    if any('cache_control' in b for b in m.get('content') or [] if isinstance(b, dict))]
                row['wire_sha256'] = digest(json.dumps(seen[0], sort_keys=True).encode())
            rows.append(row)
            with (output / 'attempts.jsonl').open('a') as ledger:
                ledger.write(json.dumps(row, ensure_ascii=False) + '\n')
            write_json(output / 'results.json', rows)
            context_usage_sink.reset(usage_token)
            evaluation_evidence_sink.reset(evidence_token)
    return consume()


async def run(args):
    output = assert_output_path(args.output)
    output.mkdir(parents=True)
    (output / 'requests').mkdir()
    source = source_manifest()
    settings = replace(load_settings(), llm_fallback_model='', llm_max_retries=0,
                       prompt_cache_mode='passthrough', semantic_cache_enabled=False)
    manifest = {'schema_version': 'cache-pruning-diagnostic-v1', 'status': 'running',
        'scope': '诊断，不能作为正式成本收益验收', 'cases': args.cases,
        'repetitions': args.repetitions, 'planned_business_requests': len(args.cases) * 3 * args.repetitions * 6,
        'planned_capture_requests': len(args.cases) * 6,
        'model': settings.llm_model, 'gateway_sha256': digest(settings.llm_base_url.encode()),
        'source': source, 'source_stable': None, 'started_at': datetime.now(timezone.utc).isoformat(),
        'parameters': {'temperature': 0, 'max_completion_tokens': 128, 'concurrency': 1,
                       'min_interval_seconds': 2, 'timeout_seconds': 45, 'product_tokens': 6000,
                       'target_tokens': 48000, 'layout': 'stable_prefix', 'tool_choice': 'auto',
                       'retries': 0, 'fallback': False, 'nonce_length': 32},
        'provider_profile': {'prices': None, 'currency': None, 'ttl': None, 'minimum_cache_tokens': None,
                             'cache_write_observable': None, 'namespace_isolation_verified': False},
        'release_allowed': False,
        'limits': ['仅开发场景，未跑864次正式矩阵或TTL诊断',
                   '工具schema保留且tool_choice=auto，但实验不执行任何工具，返回工具判失败',
                   '合成历史的已读状态由采集阶段真实完成的响应建立',
                   '首系统nonce不能隔离更早的工具前缀；缓存最小长度与TTL未核实',
                   '未知费用不按0或未经核实的model_cost计价']}
    write_json(output / 'manifest.json', manifest)
    rows, trajectories = [], {}
    model = create_chat_model(settings, stream=True, throttle=GatewayThrottle(1, 2))
    model.parameters.temperature = 0
    model.parameters.max_tokens = 128
    model.client.timeout = 45
    try:
        # 完整采集与开发重放分账；准备调用不会冒充A/B/C的实际业务请求。
        for case in args.cases:
            capture_nonce = uuid.uuid4().hex
            async def invoke(frame, native_messages):
                frame['native_messages'] = [m.model_dump(mode='json') for m in native_messages]
                body, records = request_for(frame, 'A', capture_nonce, None, time.monotonic())
                return await call_stream(model, frame, body, native_messages,
                    {'phase': 'capture', 'case': case, 'arm': 'A', 'repetition': 0, 'step': frame['step']},
                    output, rows)
            trajectories[case] = await capture(case, output / 'capture' / case, model, invoke, settings.llm_model)
        # 场景顺序冻结，整条轨迹连续执行，组别顺序按六种排列轮换。
        for case_index, (case, frames) in enumerate(trajectories.items()):
            for repetition in range(args.repetitions):
                for arm in ORDERS[(case_index + repetition) % len(ORDERS)]:
                    nonce = uuid.uuid4().hex
                    selector = PruneAwareBoundaries()
                    for frame in frames:
                        body, records = request_for(frame, arm, nonce, selector, time.monotonic())
                        native = [Msg.model_validate(m) for m in frame['native_messages']]
                        stream = await call_stream(model, frame, body, native,
                            {'phase': 'business', 'case': case, 'arm': arm,
                             'repetition': repetition, 'step': frame['step']},
                            output, rows, selector if arm == 'C' else None, records)
                        try:
                            async for _ in stream:
                                pass
                        except Exception:
                            # 原始失败已结算；下一步仍沿冻结轨迹，不重试/改题。
                            pass
                        render(output, manifest, rows, trajectories)
        manifest['status'] = 'completed'
    except asyncio.CancelledError:
        manifest['status'] = 'interrupted'
        raise
    except Exception as error:
        manifest['status'] = 'failed'
        manifest['error'] = type(error).__name__ + ': ' + str(error)[:200]
    finally:
        await model.client.close()
        manifest['source_stable'] = source_manifest()['sha256'] == source['sha256']
        write_json(output / 'manifest.json', manifest)
        render(output, manifest, rows, trajectories)
    print(json.dumps({'report': str(output / 'report.html'), 'status': manifest['status'],
                      'calls': len(rows), 'release_allowed': False}, ensure_ascii=False))
    return 0 if manifest['status'] == 'completed' and all(r['passed'] for r in rows) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--repetitions', type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if len(args.cases) != len(set(args.cases)):
        parser.error('场景不得重复')
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
