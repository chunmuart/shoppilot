"""模型输入计数的统一口径：保留 SDK 整数接口，校准只在边界应用一次。"""
from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path


def model_identity(model) -> str:
    """无配置的旧模型兼容历史校准；新模型按完整计数合同隔离。"""
    runtime = getattr(model, '_shoppilot_runtime', None)
    base = str(model.model) + '|' + str(getattr(getattr(model, 'client', None), 'base_url', ''))
    if not runtime:
        return base
    formatter = getattr(model, 'formatter', None)
    value = {
        'profile': runtime.get('profile_id'), 'revision': runtime.get('profile_revision'),
        'fingerprint': runtime.get('fingerprint'), 'base': base,
        'counter': runtime.get('counter'), 'reasoning': runtime.get('reasoning_parameters'),
        'counter_method': (getattr(model, '_shoppilot_counter_metadata', {}) or {}).get('method', 'agentscope_estimate'),
        'formatter': type(formatter).__module__ + '.' + type(formatter).__qualname__,
        'cache_mode': getattr(formatter, 'mode', None), 'cache_policy': getattr(formatter, 'policy', None),
    }
    return 'model-v1:' + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execution_identity(model, response_models=()) -> dict:
    """观测中只放配置身份，不回传凭据或网关地址。"""
    runtime = getattr(model, '_shoppilot_runtime', None) or {}
    returned = sorted(set(response_models))
    return {'identity': model_identity(model), 'requested_model': model.model,
            'response_models': returned, 'response_model_matches': returned == [model.model] if returned else None,
            'profile_id': runtime.get('profile_id'), 'profile_revision': runtime.get('profile_revision')}


def input_limits(model, output_tokens=None) -> dict:
    runtime = getattr(model, '_shoppilot_runtime', None) or {}
    window = int(model.context_size)
    configured = getattr(getattr(model, 'parameters', None), 'max_tokens', None)
    # 旧配置维持已有的最小预留；模型卡限制小于 8192 时按真实输出上限预留。
    maximum = runtime.get('max_output_tokens')
    reserve = int(output_tokens or configured or 8192)
    reserve = min(reserve, int(maximum)) if maximum else max(8192, reserve)
    safety = max(4096, math.ceil(window * .05))
    available = window - reserve - safety
    if runtime.get('max_input_tokens'):
        available = min(available, int(runtime['max_input_tokens']))
    return {'context_window': window, 'output_reserve': reserve,
            'safety_reserve': safety, 'input_budget': available}


async def count_model_input(model, messages, tools=None, calibration=None) -> dict:
    raw = await model.count_tokens(messages=messages, tools=tools)
    metadata = dict(getattr(model, '_shoppilot_counter_metadata', {}) or {})
    method = metadata.get('method', 'agentscope_estimate')
    identity = model_identity(model)
    calibration = calibration or {}
    base_factor = 1.0 if method == 'local_chat_template' else 1.5
    factor = calibration.get('factor', base_factor) if calibration.get('identity') == identity else base_factor
    if type(factor) not in (int, float) or not math.isfinite(factor):
        factor = base_factor
    factor = max(base_factor, factor)
    return {**metadata, 'method': method, 'raw_tokens': raw,
            'estimated_input_tokens': math.ceil(raw * factor), 'calibration_factor': factor,
            'identity': identity, 'is_estimate': True}


class CounterUnavailable(ValueError):
    pass


@lru_cache(maxsize=8)
def _load_local_tokenizer(directory: str, manifest_json: str):
    """只读经过散列固定的本地分词文件；绝不下载或执行仓库里的 Python 代码。"""
    root = Path(directory).resolve(strict=True)
    manifest = json.loads(manifest_json)
    if not isinstance(manifest, dict) or not manifest or 'tokenizer_config.json' not in manifest:
        raise CounterUnavailable('missing_manifest')
    # tokenizer、词表和模板都属于计数合同；未固定的新文件也不能悄悄参与加载。
    files = {p.name for p in root.iterdir() if p.is_file()}
    relevant = {n for n in files if n.endswith(('.json', '.model', '.txt', '.jinja', '.tiktoken'))}
    if not relevant.issubset(manifest):
        raise CounterUnavailable('incomplete_manifest')
    for name, expected in manifest.items():
        path = root / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise CounterUnavailable('invalid_manifest_path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise CounterUnavailable('manifest_mismatch')
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(root), local_files_only=True, trust_remote_code=False)
    if not tokenizer.chat_template:
        raise CounterUnavailable('missing_chat_template')
    return tokenizer


async def local_token_count(model, messages, tools):
    """返回原始整数，不乘保守系数；不可精算时由调用方退回 SDK 估算。"""
    runtime = getattr(model, '_shoppilot_runtime', None) or {}
    config = runtime.get('counter') or {}
    if config.get('kind', config.get('method')) != 'local_chat_template':
        raise CounterUnavailable('no_matching_tokenizer')
    if model.model not in config.get('model_ids', []):
        raise CounterUnavailable('model_mismatch')
    if not config.get('directory'):
        raise CounterUnavailable('missing_directory')
    if config.get('reasoning_parameters', {}) != (runtime.get('reasoning_parameters') or {}):
        # 模板必须声明所匹配的思考参数；不能把另一种隐藏模板的长度当作精算。
        raise CounterUnavailable('reasoning_mismatch')
    tokenizer = _load_local_tokenizer(config['directory'], json.dumps(config.get('files', {}), sort_keys=True))
    # formatter 仍由 AgentScope 提供，计数不会触发缓存观察或修改历史消息。
    from app.infrastructure.prompt_cache import cache_attempt, cache_disabled
    from copy import deepcopy
    attempt = cache_attempt.set(None)
    disabled = cache_disabled.set(True)
    try:
        formatted = await model.formatter.format(deepcopy(messages))
    finally:
        cache_disabled.reset(disabled)
        cache_attempt.reset(attempt)
    formatted_tools, _ = model._format_tools(deepcopy(tools), None)
    options = dict(config.get('template_parameters') or {})
    if any(key in options for key in ('tokenize', 'tools', 'add_generation_prompt', 'return_tensors', 'return_dict')):
        raise CounterUnavailable('invalid_template_parameters')
    ids = tokenizer.apply_chat_template(formatted, tools=formatted_tools, tokenize=True,
                                        add_generation_prompt=True, **options)
    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
        raise CounterUnavailable('invalid_tokenizer_result')
    return len(ids)
