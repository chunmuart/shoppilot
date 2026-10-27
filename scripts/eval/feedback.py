# -*- coding: utf-8 -*-
"""评测分数发件箱与 BadCase 待审核队列。人工审核前不改金标或正式选集。"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import httpx

_TRACE = re.compile(r'^[0-9a-f]{32}$')


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def valid_trace_id(value) -> bool:
    return isinstance(value, str) and _TRACE.fullmatch(value) is not None and value != '0' * 32


def score_payload(*, manifest_hash: str, case_id: str, trace_id: str, name: str, value: float) -> dict:
    if not valid_trace_id(trace_id):
        raise ValueError('分数必须关联已观察到的有效 OpenTelemetry trace ID')
    if name not in {'eval.score', 'eval.p0_pass', 'eval.task_success'}:
        raise ValueError('未知评测分数名')
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('评测分数必须是 0..1 的有限数字')
    key = _hash([manifest_hash, case_id, trace_id, name])
    return {'id': key, 'traceId': trace_id, 'name': name, 'value': float(value), 'dataType': 'NUMERIC'}


class FeedbackStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS score_outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
            CREATE TABLE IF NOT EXISTS badcases (id TEXT PRIMARY KEY, evidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unreviewed', reviewer TEXT, review_note TEXT, regression_path TEXT);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def enqueue_score(self, payload: dict):
        # 验证入口的固定字段，禁止把地址/聊天正文借发件箱导出。
        if set(payload) != {'id', 'traceId', 'name', 'value', 'dataType'} or payload['dataType'] != 'NUMERIC':
            raise ValueError('分数只允许固定数值协议字段')
        if not isinstance(payload['id'], str) or re.fullmatch(r'[0-9a-f]{64}', payload['id']) is None:
            raise ValueError('分数幂等 ID 必须为 SHA-256')
        score_payload(manifest_hash='', case_id='', trace_id=payload['traceId'], name=payload['name'], value=payload['value'])
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        row = self.db.execute('SELECT payload FROM score_outbox WHERE id=?', (payload['id'],)).fetchone()
        if row and row[0] != serialized:
            raise ValueError('同一分数幂等 ID 不得更改内容')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO score_outbox(id,payload) VALUES(?,?)', (payload['id'], serialized))

    def candidate(self, evidence: dict) -> str:
        if not evidence.get('source_ref') or not evidence.get('case_id') or not evidence.get('kind'):
            raise ValueError('候选必须有可回溯的来源、case ID 和问题分类')
        candidate_id = _hash(evidence)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO badcases(id,evidence) VALUES(?,?)', (candidate_id, json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
        return candidate_id

    def review(self, candidate_id: str, *, accepted: bool, reviewer: str, note: str):
        if type(accepted) is not bool or not reviewer.strip() or not note.strip():
            raise ValueError('审核必须明确决议、审核人和理由')
        with self.db:
            changed = self.db.execute("UPDATE badcases SET status=?,reviewer=?,review_note=? WHERE id=? AND status='unreviewed'",
                ('accepted' if accepted else 'rejected', reviewer, note, candidate_id)).rowcount
            if changed != 1:
                raise ValueError('候选不存在或已审核，不能覆盖原审核')

    def export_proposal(self, candidate_id: str, target: Path) -> dict:
        row = self.db.execute('SELECT evidence,status,reviewer,review_note FROM badcases WHERE id=?', (candidate_id,)).fetchone()
        if row is None or row[1] != 'accepted':
            raise ValueError('仅人工接受的候选可导出回归提案')
        proposal = {'candidate_id': candidate_id, 'status': 'proposal_requires_fixture_review', 'evidence': json.loads(row[0]),
                    'reviewer': row[2], 'review_note': row[3], 'official_dataset_changed': False}
        target.parent.mkdir(parents=True, exist_ok=True)
        # 不能覆盖已存在的正式文件；新提案仍须补可判定断言后才可另行纳入 gold。
        with target.open('x', encoding='utf-8') as handle:
            json.dump(proposal, handle, ensure_ascii=False, indent=2)
        with self.db:
            self.db.execute('UPDATE badcases SET regression_path=? WHERE id=?', (str(target), candidate_id))
        return proposal

    def import_manifest(self, path: Path) -> dict:
        from scripts.eval.learning_import import read_report
        manifest, imported_results, manifest_hash = read_report(path)
        summary = {'queued_scores': 0, 'badcases': 0, 'unlinked_scores': 0}
        if manifest.get('schema_version') == 'harness-eval-v1':
            # 新版结果与 manifest 分文件；实例身份必须包含策略和重复号。
            results = [{**row, 'id': f"{row['case_id']}:{row['strategy']}:{row['repetition']}",
                        'verdict': 'PASS' if row['passed'] else 'FAIL'} for row in imported_results]
        else:
            results = imported_results
        for result in results:
            refs = {event.get('correlation', {}).get('trace_id') for event in result.get('trace_events', [])
                    if valid_trace_id(event.get('correlation', {}).get('trace_id'))}
            if len(refs) == 1 and type(result.get('score')) in (int, float):
                trace_id = next(iter(refs))
                for name, value in [('eval.score', result['score']), ('eval.p0_pass', float(result.get('p0_pass') is True)), ('eval.task_success', float(result.get('verdict') == 'PASS'))]:
                    self.enqueue_score(score_payload(manifest_hash=manifest_hash, case_id=result['id'], trace_id=trace_id, name=name, value=value))
                    summary['queued_scores'] += 1
            else:
                summary['unlinked_scores'] += 1
            if result.get('verdict') != 'PASS':
                self.candidate({'source_ref': str(path.resolve()), 'source_sha256': manifest_hash, 'case_id': result['id'], 'kind': 'agent_failure', 'verdict': result.get('verdict'), 'review_required': True})
                summary['badcases'] += 1
        observations = manifest.get('execution', {}).get('observations', [])
        if isinstance(observations, dict):
            observations = [row for rows in observations.values() for row in rows]
        for index, result in enumerate(observations):
            if not isinstance(result, dict):
                continue
            relevant, retrieved = set(result.get('relevant', [])), set(result.get('canonical_retrieved', result.get('retrieved', [])))
            if (relevant and not relevant <= retrieved) or result.get('unanswerable_pass') is False or result.get('empty_pass') is False:
                self.candidate({'source_ref': str(path.resolve()), 'source_sha256': manifest_hash,
                    'case_id': result.get('case_id') or result.get('id') or f'observation-{index}',
                    'kind': 'retrieval_diagnostic', 'review_required': True})
                summary['badcases'] += 1
        return summary

    async def flush(self, base_url: str, *, public_key: str, secret_key: str, client: httpx.AsyncClient | None = None) -> dict:
        url = urlsplit(base_url)
        if url.username or url.password or url.query or url.fragment or url.scheme not in {'http', 'https'}:
            raise ValueError('分数服务 URL 不能包含凭据或查询参数')
        if url.scheme == 'http' and url.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('远端分数服务必须使用 HTTPS')
        if not public_key or not secret_key:
            return {'status': 'BLOCKED', 'reason': 'missing_credentials', 'sent': 0}
        rows = self.db.execute("SELECT id,payload FROM score_outbox WHERE status='pending' ORDER BY id").fetchall()
        owned = client is None
        client = client or httpx.AsyncClient()
        sent = 0
        try:
            for identity, payload in rows:
                with self.db:
                    self.db.execute('UPDATE score_outbox SET attempts=attempts+1 WHERE id=?', (identity,))
                try:
                    response = await client.post(base_url.rstrip('/') + '/api/public/scores', json=json.loads(payload),
                        auth=httpx.BasicAuth(public_key, secret_key), headers={'Idempotency-Key': identity}, timeout=15, follow_redirects=False)
                    response.raise_for_status()
                    acknowledgment = response.json()
                    if not isinstance(acknowledgment, dict) or acknowledgment.get('id') != identity:
                        raise ValueError('score_ack_mismatch')
                except (httpx.HTTPError, ValueError) as error:
                    # 不保留服务响应正文/URL/认证，避免上游错误回显敏感字段。
                    with self.db:
                        self.db.execute('UPDATE score_outbox SET last_error=? WHERE id=?', (type(error).__name__, identity))
                    continue
                with self.db:
                    self.db.execute("UPDATE score_outbox SET status='sent',last_error=NULL WHERE id=?", (identity,))
                sent += 1
        finally:
            if owned:
                await client.aclose()
        pending = self.db.execute("SELECT count(*) FROM score_outbox WHERE status='pending'").fetchone()[0]
        return {'status': 'DELIVERED' if pending == 0 else 'BLOCKED', 'sent': sent, 'pending': pending,
                'scope': 'HTTP acknowledgement; does not prove remote read visibility'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    ingest = sub.add_parser('import'); ingest.add_argument('--manifest', type=Path, required=True)
    review = sub.add_parser('review'); review.add_argument('--candidate', required=True); review.add_argument('--reviewer', required=True); review.add_argument('--note', required=True); review.add_argument('--decision', choices=['accept', 'reject'], required=True)
    proposal = sub.add_parser('propose'); proposal.add_argument('--candidate', required=True); proposal.add_argument('--output', type=Path, required=True)
    flush = sub.add_parser('flush')
    flush.add_argument('--base-url', help='默认读取 LANGFUSE_BASE_URL；显式地址优先')
    flush.add_argument('--env-file', type=Path, help='仅从本机文件读取 Langfuse 三项配置，不接收命令行密钥')
    args = parser.parse_args(argv); store = FeedbackStore(args.db)
    try:
        if args.command == 'import': result = store.import_manifest(args.manifest)
        elif args.command == 'review': result = store.review(args.candidate, accepted=args.decision == 'accept', reviewer=args.reviewer, note=args.note)
        elif args.command == 'propose': result = store.export_proposal(args.candidate, args.output)
        else:
            import asyncio
            from dataclasses import replace
            from app.infrastructure.langfuse_config import LangfuseConfig
            try:
                config = LangfuseConfig.from_env(env_file=args.env_file)
                if args.base_url:
                    config = replace(config, base_url=args.base_url)
                missing = config.missing_fields()
                if missing:
                    result = {'status': 'BLOCKED', 'reason': 'missing_configuration', 'missing': missing, 'sent': 0}
                else:
                    config.validate()
                    result = asyncio.run(store.flush(config.base_url, public_key=config.public_key, secret_key=config.secret_key))
            except (OSError, ValueError) as error:
                # 配置错误可能包含文件路径或上游内容，只输出错误类型。
                result = {'status': 'BLOCKED', 'reason': 'invalid_configuration', 'error_type': type(error).__name__, 'sent': 0}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get('status') == 'BLOCKED' else 0
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
