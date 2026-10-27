"""冻结口径、配置白名单和文件指纹；评测不能读写真实买家数据。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import platform
from importlib.metadata import version

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / 'eval/harness/v1/suite.json'
SCHEMA_VERSION = 'harness-eval-v1'
POLICY_FIELDS = {'context_summary_mode', 'model_recovery_mode', 'skill_catalog_mode', 'context_strategy', 'context_pruning_timing', 'context_product_tokens',
                 'context_target_tokens', 'context_prompt_layout', 'context_state_mode', 'context_lookup_mode', 'context_prune_low_ratio', 'context_compact_result_rules', 'prompt_cache_mode', 'prompt_cache_policy'}
WATCHED = ('app/application/agents/', 'app/application/harness/', 'app/application/memory/',
           'app/domain/learning.py', 'app/application/learning.py', 'app/infrastructure/learning',
           'app/infrastructure/strategy_extractor.py', 'app/infrastructure/ag_ui_journal.py',
           'tests/test_learning', 'scripts/learning.py', 'eval/learning/',
           'app/application/prompts/', 'app/application/tools/', 'app/infrastructure/context',
           'app/infrastructure/prompt_cache.py', 'app/infrastructure/llm.py',
           'app/infrastructure/buyer_skills.py', 'app/infrastructure/capability_registry.py',
           'tests/test_skill_catalog', 'tests/test_buyer_workspace', 'tests/test_capability_registry',
           'configs/models.yaml', 'app/infrastructure/model_registry.py', 'app/infrastructure/model_connection.py', 'app/infrastructure/model_counting.py', 'tests/test_model_',
           'app/infrastructure/model_protocol.py', 'app/infrastructure/model_recovery.py', 'app/infrastructure/visible_evidence.py',
           'app/infrastructure/harness_middleware.py', 'app/infrastructure/budget.py',
           'app/infrastructure/resilience.py', 'app/infrastructure/throttle.py',
           'app/infrastructure/stream_lifecycle.py', 'app/infrastructure/tracing.py',
           'app/infrastructure/settings.py', 'app/infrastructure/semantic_memory.py',
           'app/infrastructure/persistence/context_evidence.py', 'app/infrastructure/persistence/sql/session_store.py',
           'app/infrastructure/security/', 'app/composition.py',
           'scripts/eval/', 'scripts/model_preflight.py', 'eval/harness/v1/', 'tests/test_harness', 'tests/test_context',
           'tests/test_model_protocol', 'tests/test_visible_evidence',
           'tests/test_layered', 'tests/test_prompt_cache', 'tests/test_native_memory', 'pyproject.toml', 'uv.lock',
           'Makefile', '.github/workflows/harness-eval.yml', 'data/catalog-v1.jsonl')

# 白名单记录可影响执行的本地配置，不序列化 Settings 全量以免泄露凭据/买家标识。
RUNTIME_FIELDS = ('tool_result_limit', 'tool_failure_threshold', 'tool_circuit_reset_seconds',
                  'harness_enabled', 'loop_repeat_threshold', 'output_guard_enabled', 'drift_detect_enabled',
                  'reply_token_budget', 'token_budget_total', 'preference_relevance_enabled',
                  'preference_top_k', 'preference_subagent_inject', 'session_owner_binding',
                  'semantic_cache_enabled', 'llm_max_retries', 'llm_fallback_model')


def execution_environment(settings):
    return {'python': platform.python_version(),
            'packages': {name: version(name) for name in ('agentscope', 'openai', 'pydantic', 'sqlalchemy')},
            'settings': {name: getattr(settings, name) for name in RUNTIME_FIELDS}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def source_manifest(root=ROOT):
    files = [p for directory in ('app', 'scripts/eval', 'tests') for p in (root / directory).rglob('*.py')]
    files += [root / 'configs/models.yaml', root / 'pyproject.toml', root / 'uv.lock', root / 'Makefile', root / '.github/workflows/harness-eval.yml']
    files += [root / 'scripts/model_preflight.py', root / 'scripts/learning.py']
    files += list((root / 'eval/learning').glob('*.json'))
    files += list((root / 'app/application/prompts').glob('*.yml'))
    files += list((root / 'eval/harness/v1').glob('*.json'))
    # 此评测使用冻结目录与代码内费率，不连接外部知识索引。
    files += [root / 'data/catalog-v1.jsonl']
    hashes = {str(p.relative_to(root)): digest(p.read_bytes()) for p in sorted(files) if p.is_file()}
    return {'sha256': digest(json.dumps(hashes, sort_keys=True).encode()), 'files': hashes}


def load_suite(path=SUITE):
    path = Path(path).resolve()
    suite = json.loads(path.read_text())
    if suite.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('未知评测合同版本')
    dataset = json.loads((path.parent / suite['cases_file']).read_text())
    ids = [c['id'] for c in dataset]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'[a-z0-9-]+', i) for i in ids):
        raise ValueError('场景 ID 重复或不安全')
    if not {'current', 'candidate'} <= set(suite['strategies']):
        raise ValueError('必须定义当前基线与候选策略')
    for name, strategy in suite['strategies'].items():
        if not re.fullmatch(r'[a-z0-9_]+', name) or set(strategy['overrides']) - POLICY_FIELDS:
            raise ValueError('策略仅允许 Harness 参数，不允许偷偷更换模型、目录或权限')
    for profile in suite['profiles'].values():
        if profile['repetitions'] < 1 or profile['repetitions'] > 10:
            raise ValueError('重复次数必须在 1..10')
        if set(profile['cases']) - set(ids) or len(profile['cases']) != len(set(profile['cases'])):
            raise ValueError('profile 引用了未知或重复场景')
    development={c['id'] for c in dataset if c['split']=='dev'}
    holdout={c['id'] for c in dataset if c['split']=='holdout'}
    if set(suite['profiles']['release']['cases'])!=holdout or len(holdout)<28 or suite['profiles']['release']['repetitions']<3:
        raise ValueError('release 必须覆盖全部留出场景（至少 28 个）并各重复至少 3 次')
    if set(suite['profiles']['dev']['cases'])!=development or not set(suite['profiles']['smoke']['cases'])<=development:
        raise ValueError('开发/冒烟不能使用留出场景调参')
    suite['_cases'] = dataset
    suite['_suite_sha256'] = digest(path.read_bytes())
    suite['_dataset_sha256'] = digest((path.parent / suite['cases_file']).read_bytes())
    return suite


def affected_paths(paths):
    # 同时支持独立工程仓库和课程父仓库的输出，不把教程路径判为工程修改。
    result = []
    for raw in paths:
        path = raw.strip().replace('\\', '/')
        if '/shoppilot-agent/' in path:
            path = path.split('/shoppilot-agent/', 1)[1]
        if path.startswith(WATCHED):
            result.append(path)
    return sorted(set(result))


def assert_output_path(path):
    path = Path(path).resolve()
    protected = [ROOT / folder for folder in ('app', 'tests', 'data', 'frontend', 'knowledge', 'scripts')]
    if path == ROOT or any(path == p or p in path.parents for p in protected):
        raise ValueError('评测输出不能放入源码或真实数据目录')
    if path.exists():
        raise ValueError('输出目录已存在；请使用新目录，原始失败不可覆盖')
    return path
