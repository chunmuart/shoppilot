"""连接成功与返回型号一致是两个证据，不能把缺失标识算作通过。"""
import json
from datetime import datetime

import httpx
import pytest

from app.infrastructure.model_connection import probe_connection


@pytest.mark.asyncio
@pytest.mark.parametrize('reported,aliases,expected', [
    (['qwen-test', 'qwen-test'], [], True),
    (['other-model', 'other-model'], [], False),
    (['qwen-test', 'other-model'], [], False),
    ([None, None], [], None),
    (['qwen-test', None], [], None),
    (['qwen-test-20261008', 'qwen-test-20261008'], ['qwen-test-20261008'], True),
    (['private-test-key', 'private-test-key'], [], None),
    (['<invalid-model>', '<invalid-model>'], [], None),
])
async def test_probe_records_identity_per_request_without_leaking_key(reported, aliases, expected):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        index = len(requests)
        requests.append(body)
        if index == 0:
            delta = {'tool_calls': [{'index': 0, 'id': 'probe', 'type': 'function',
                'function': {'name': 'connection_probe', 'arguments': '{"value":"ok"}'}}]}
        else:
            delta = {'content': 'ok'}
        event = {'choices': [{'delta': delta, 'finish_reason': 'stop'}]}
        if reported[index] is not None:
            event['model'] = reported[index]
        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')

    result = await probe_connection({'model_id': 'qwen-test', 'base_url': 'http://127.0.0.1/v1',
        'probe_model_aliases': aliases}, 'private-test-key', allowed_local_hosts=('127.0.0.1',),
        transport=httpx.MockTransport(handler))
    assert result['model_identity_matches'] is expected
    assert result['requested_model'] == 'qwen-test'
    assert result['streaming'] and result['tool_calling'] and result['tool_result']
    assert result['context_verified'] is False
    assert 'private-test-key' not in json.dumps(result)
    assert '<invalid-model>' not in json.dumps(result)
    assert datetime.fromisoformat(result['tested_at']).tzinfo is not None
    assert len(requests) == 2
