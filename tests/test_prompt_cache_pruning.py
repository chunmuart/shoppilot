"""真实AgentScope消息/SQLite/最终HTTP：候选边界与控制实验回归。"""
from copy import deepcopy
from types import SimpleNamespace
import json

import httpx
import pytest
from agentscope.message import Msg, UserMsg
from app.infrastructure.llm import create_chat_model
from app.infrastructure.prompt_cache import PruneAwareBoundaries, normalized_cache_messages, closed_cache_boundaries
from scripts.eval.harness.pruning_fixture import capture
from scripts.eval.harness.pruning_experiment import request_for, canonical, call_stream, answer_checks
from scripts.eval.harness.pruning_report import aggregate
from tests.test_prompt_cache import settings
from app.infrastructure.throttle import GatewayThrottle


@pytest.fixture
async def trajectories(tmp_path):
    model = create_chat_model(settings(tmp_path))
    async def invoke(frame, messages):
        frame['native_messages'] = [m.model_dump(mode='json') for m in messages]
        async def source():
            yield SimpleNamespace(usage={}, finished_reason='stop')
        return source()
    try:
        yield {case: await capture(case, tmp_path / case, model, invoke, model.model)
               for case in ('append', 'middle', 'continuous')}
    finally:
        await model.client.close()


async def test_real_governance_keeps_first_read_and_selected_result(trajectories):
    for case, frames in trajectories.items():
        assert frames[0]['mutable_ids'] == []
        assert frames[1]['mutable_ids'] == ['search-1', 'search-2']
        assert 'search-0' not in {i for f in frames for i in f['mutable_ids']}
        events = [f['step'] for f in frames if f['archived_results']]
        assert events == {'append': [], 'middle': [2], 'continuous': [2,4]}[case]
        for f in frames:
            assert f['expected']['price_major'] == 139
            assert any('P1003-S1' in str(m) for m in f['request']['messages'])


async def test_three_arms_same_content_and_boundary_survives_actual_prune(trajectories):
    frames = trajectories['middle']
    selector = PruneAwareBoundaries(ttl_seconds=300)
    markers = []
    for step, frame in enumerate(frames):
        bodies = []
        for arm in 'ABC':
            nonce = arm * 32
            body, records = request_for(frame, arm, nonce, selector, step * 10)
            bodies.append(canonical(body, nonce))
            if arm == 'C':
                markers.append(records)
                assert len(records) <= 3
                selector.record_sent(records, now=step * 10, request=body)
        assert bodies[0] == bodies[1] == bodies[2]
    p1 = next(m for m in markers[1] if m['role'] == 'P')
    p2 = next(m for m in markers[2] if m['role'] == 'P')
    assert p2['prefix_sha256'] == p1['prefix_sha256']
    assert p2['prior_status'] == 'previously_sent_within_ttl'
    assert p2['selection_reason'] == 'surviving_previously_sent_prefix'
    p3 = next(m for m in markers[3] if m['role'] == 'P')
    assert p3['message_index'] > p2['message_index']
    assert not any(m['provider_cache_confirmed'] for ms in markers for m in ms)


async def test_no_oracle_or_unsent_cache_and_changed_tools_invalidate(trajectories):
    f = trajectories['middle'][1]
    state = deepcopy(f)
    selector = PruneAwareBoundaries()
    body, records = request_for(f, 'C', 'c'*32, selector, 1)
    assert all(m['prior_status'] == 'new' for m in records)
    assert request_for(f, 'C', 'c'*32, selector, 2)[1] == records  # 选择并非已发送
    selector.record_sent(records, now=2, request=body)
    f = deepcopy(f)
    f['request']['tools'][0]['function']['description'] += ' 新版本'
    _, changed = request_for(f, 'C', 'c'*32, selector, 3)
    assert all(m['prior_status'] == 'new' for m in changed)
    assert trajectories['middle'][1] == state


async def test_expired_prefix_not_used_to_claim_preservation(trajectories):
    selector = PruneAwareBoundaries(ttl_seconds=5)
    f = trajectories['middle'][1]
    body, records = request_for(f, 'C', 'a'*32, selector, 0)
    selector.record_sent(records, now=0, request=body)
    _, after = request_for(trajectories['middle'][2], 'C', 'a'*32, selector, 10)
    assert all(m['selection_reason'] != 'surviving_previously_sent_prefix' for m in after)


@pytest.mark.parametrize('tail', [
    [{'role':'assistant','tool_calls':[{'id':'a'},{'id':'b'}]},
     {'role':'tool','tool_call_id':'a','content':'A'}],
    [{'role':'assistant','tool_calls':[{'id':'a'}]},
     {'role':'user','content':'等待审批'}, {'role':'assistant','content':'不能视为闭合'}],
    [{'role':'tool','tool_call_id':'unknown','content':'错误配对'}],
])
def test_no_bp_inside_incomplete_batch(tail):
    messages = [{'role':'system','content':'固定规则'}, *tail]
    assert closed_cache_boundaries(messages) == []
    marked, records = PruneAwareBoundaries().select({'messages':messages}, {'a'}, now=0)
    assert [r['role'] for r in records] == ['S']
    assert normalized_cache_messages(marked) == normalized_cache_messages(messages)


def test_unknown_usage_not_zero_and_critical_answer_assertions():
    row = {'usage': [], 'passed': False, 'elapsed_ms': 2}
    assert aggregate([row])['input_tokens'] is None
    assert aggregate([row])['cache_write_tokens'] is None
    assert aggregate([row])['input_side_cost'] is None
    expected = dict(sku_id='P1003-S1', price_major=139, currency='CNY', budget=180, source='ctx_local')
    assert all(answer_checks(json.dumps(expected), expected).values())
    for key in expected:
        bad = {**expected, key: 'wrong'}
        assert not answer_checks(json.dumps(bad), expected)[key]


@pytest.mark.parametrize('arm', ['A', 'B', 'C'])
async def test_actual_http_matches_and_rejected_bp_never_retries(tmp_path, trajectories, arm):
    model = create_chat_model(settings(tmp_path), throttle=GatewayThrottle(1,0))
    model.parameters.max_tokens = 128
    model.parameters.temperature = 0
    wire = []
    def handler(request):
        wire.append(json.loads(request.content))
        return httpx.Response(400, json={'error': {'message': 'cache_control is not supported', 'param': 'messages'}})
    await model.client.close()
    model.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    folder = tmp_path / ('http-' + arm)
    (folder/'requests').mkdir(parents=True)
    frame = trajectories['middle'][1]
    selector = PruneAwareBoundaries()
    body, records = request_for(frame, arm, 'b'*32, selector, 0)
    rows = []
    try:
        stream = await call_stream(model, frame, body,
            [Msg.model_validate(m) for m in frame['native_messages']],
            dict(phase='business',case='middle',arm=arm,repetition=0,step=1),
            folder,rows,selector,records)
        with pytest.raises(Exception):
            async for _ in stream:
                pass
        assert len(wire) == 1 and wire[0] == body
        assert not rows[0]['passed'] and rows[0]['wire_requests'] == 1
        assert rows[0]['usage'][0]['input_tokens'] is None
        assert rows[0]['tools_executed'] == 0
    finally:
        await model.client.close()

