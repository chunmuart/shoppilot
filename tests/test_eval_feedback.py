# -*- coding: utf-8 -*-
"""真实本地 HTTP 分数接收端，验证幂等重发与审核隔离，不假装云端已验收。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import pytest
from scripts.eval.feedback import FeedbackStore, score_payload


@pytest.mark.parametrize('trace_id', ['', '0'*32, 'not-a-trace', 'f'*31])
def test_score_rejects_missing_or_invalid_trace(trace_id):
    with pytest.raises(ValueError, match='trace ID'):
        score_payload(manifest_hash='snapshot', case_id='case', trace_id=trace_id, name='eval.score', value=1)


async def test_real_local_http_retry_uses_same_score_id_and_survives_restart(tmp_path):
    received = {}; requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, payload['id'], self.headers.get('Idempotency-Key')))
            received[payload['id']] = payload
            # 首次模拟服务已保存后返回 500；客户端重试必须仍只有一个逻辑 score。
            self.send_response(500 if len(requests) == 1 else 200)
            self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'id': payload['id']}).encode())
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    payload = score_payload(manifest_hash='snapshot', case_id='case', trace_id='a'*32, name='eval.score', value=.5)
    db = tmp_path/'feedback.db'
    store = FeedbackStore(db)
    try:
        store.enqueue_score(payload); store.enqueue_score(payload)
        address = f'http://127.0.0.1:{server.server_address[1]}'
        first = await store.flush(address, public_key='local-public', secret_key='local-secret')
        assert first['status'] == 'BLOCKED' and first['pending'] == 1
        store.close(); store = FeedbackStore(db)
        second = await store.flush(address, public_key='local-public', secret_key='local-secret')
        assert second['sent'] == 1 and second['pending'] == 0
        assert len(received) == 1
        assert requests[0] == requests[1] == ('/api/public/scores', payload['id'], payload['id'])
        assert (await store.flush(address, public_key='local-public', secret_key='local-secret'))['sent'] == 0
    finally:
        store.close(); server.shutdown(); server.server_close(); thread.join()


def test_badcase_requires_review_and_export_never_overwrites_gold(tmp_path):
    store = FeedbackStore(tmp_path/'feedback.db')
    try:
        candidate = store.candidate({'source_ref': 'report.json', 'case_id': 'case', 'kind': 'relevance'})
        with pytest.raises(ValueError, match='人工接受'):
            store.export_proposal(candidate, tmp_path/'proposal.json')
        store.review(candidate, accepted=True, reviewer='local-test-reviewer', note='仅单测模拟人工审核')
        output = tmp_path/'proposal.json'
        result = store.export_proposal(candidate, output)
        assert result['official_dataset_changed'] is False
        with pytest.raises(FileExistsError):
            store.export_proposal(candidate, output)
    finally:
        store.close()


def test_import_without_trace_keeps_badcase_but_never_guesses_score_link(tmp_path):
    manifest = {'execution': {'results': [{'id': 'failed', 'score': 0, 'verdict': 'FAIL', 'p0_pass': False, 'trace_events': []}]}}
    path = tmp_path/'report.json';path.write_text(json.dumps(manifest))
    store = FeedbackStore(tmp_path/'feedback.db')
    try:
        summary = store.import_manifest(path)
        assert summary == {'queued_scores': 0, 'badcases': 1, 'unlinked_scores': 1}
        store.import_manifest(path)
        assert store.db.execute('SELECT count(*) FROM badcases').fetchone()[0] == 1
    finally:
        store.close()


async def test_missing_credentials_do_not_send_and_remote_plaintext_is_rejected(tmp_path):
    store = FeedbackStore(tmp_path/'feedback.db')
    try:
        assert (await store.flush('https://example.invalid', public_key='', secret_key=''))['status'] == 'BLOCKED'
        with pytest.raises(ValueError, match='HTTPS'):
            await store.flush('http://example.invalid', public_key='key', secret_key='key')
    finally:
        store.close()


def test_injected_transaction_failure_is_detected_before_proposing_gold():
    from scripts.eval.evidence import evaluate_trace_assertions
    root = Path(__file__).resolve().parents[1]
    proposal = json.loads((root/'eval/proposals/confirmation-preapproval-injection.json').read_text())
    assert not evaluate_trace_assertions([proposal['criterion']], proposal['injected_bad_trace'])[0]['pass']
    assert evaluate_trace_assertions([proposal['criterion']], proposal['safe_trace'])[0]['pass']
    assert proposal['official_dataset_changed'] is False


def test_flush_cli_reads_local_env_without_llm_and_does_not_print_credentials(tmp_path, monkeypatch, capsys):
    from scripts.eval.feedback import main
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(payload)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id': payload['id']}).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = tmp_path / 'feedback.db'
    store = FeedbackStore(db)
    payload = score_payload(manifest_hash='cli-check', case_id='local-http', trace_id='b' * 32, name='eval.score', value=.5)
    store.enqueue_score(payload)
    store.close()
    env_file = tmp_path / '.env'
    env_file.write_text(
        f'LANGFUSE_BASE_URL=http://127.0.0.1:{server.server_port}\n'
        'LANGFUSE_PUBLIC_KEY=pk-cli-local\nLANGFUSE_SECRET_KEY=sk-cli-private\n',
        encoding='utf-8',
    )
    try:
        assert main(['--db', str(db), 'flush', '--env-file', str(env_file)]) == 0
        output = capsys.readouterr().out
        assert json.loads(output)['status'] == 'DELIVERED'
        assert received == [payload]
        assert 'pk-cli-local' not in output and 'sk-cli-private' not in output
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_flush_cli_missing_config_does_not_attempt_delivery(tmp_path, monkeypatch, capsys):
    from scripts.eval.feedback import main
    env_file = tmp_path / '.env'
    env_file.write_text('LANGFUSE_BASE_URL=\nLANGFUSE_PUBLIC_KEY=\nLANGFUSE_SECRET_KEY=\n', encoding='utf-8')

    async def unexpected_delivery(*_args, **_kwargs):
        pytest.fail('缺失配置时不能尝试远端写入')

    monkeypatch.setattr(FeedbackStore, 'flush', unexpected_delivery)
    assert main(['--db', str(tmp_path / 'feedback.db'), 'flush', '--env-file', str(env_file)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'BLOCKED' and output['sent'] == 0
    assert set(output['missing']) == {'LANGFUSE_BASE_URL', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'}
