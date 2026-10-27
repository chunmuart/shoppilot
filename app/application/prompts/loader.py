# -*- coding: utf-8 -*-
"""PromptLoader

读取并缓存 app/application/prompts/shoppilot.yml，全项目提示词只从这里取。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import json

import yaml

PROMPTS_PATH = Path(__file__).resolve().parent / "shoppilot.yml"


@lru_cache(maxsize=1)
def _load_default_prompts() -> dict:
    with open(PROMPTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_prompts() -> dict:
    from app.infrastructure.context import ShoppingContext
    snapshot = ShoppingContext.current()
    if snapshot is not None and snapshot.prompt_document_json:
        # 每次返回新对象，调用方不能修改不可变版本的共享正文。
        return json.loads(snapshot.prompt_document_json)
    return json.loads(json.dumps(_load_default_prompts(), ensure_ascii=False))


# 保持离线工具与旧测试的清缓存入口。
load_prompts.cache_clear = _load_default_prompts.cache_clear
