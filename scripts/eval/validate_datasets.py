# -*- coding: utf-8 -*-
"""标注集自检 —— 见教程 13-2 §2.4。

标注集是评测的基准。基准本身错了，后面所有指标都不可信，而且错法很隐蔽：
把一个「会被硬约束挡掉」的商品标成 relevant，不会报错，只会让 Recall 上限
悄悄低于 1，然后你对着一个永远达不到满分的指标反复调检索。

所以标注集在拿去评测前必须先过这一关。跑测脚本不强制依赖它，但改完标注就该跑：

    uv run python scripts/eval/validate_datasets.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.infrastructure.persistence.in_memory_repositories import (  # noqa: E402
    InMemoryProductRepository,
)
from scripts.eval.data_quality import (  # noqa: E402
    validate_catalog_distribution,
    validate_catalog_products,
    validate_catalog_raw_records,
)
from scripts.eval.eval_quality import validate_official_eval_fixture as _validate_official_eval_fixture  # noqa: E402
from scripts.eval.knowledge_quality import (  # noqa: E402
    count_knowledge_chunks,
    load_knowledge_manifest,
    validate_knowledge_content,
    validate_knowledge_manifest,
)

_PRODUCT_DATASET = Path("eval/product_recall.jsonl")
_CATEGORY_DATASET = Path("eval/category_recall.jsonl")
_KNOWLEDGE_DIR = Path("knowledge")
_FORMAL_EVAL_DIR = Path("eval") / "v1"
_CATALOG_FIXTURE = Path("data") / "catalog-v2.jsonl"


def _load(path: Path) -> list[tuple[int, dict]]:
    cases = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.strip():
            cases.append((lineno, json.loads(raw)))
    return cases


def _check_common(lineno: int, case: dict) -> list[str]:
    problems = []
    relevant = case.get("relevant") or []
    if not relevant:
        problems.append(f"L{lineno} [{case.get('query')}] relevant 为空")
    if len(set(relevant)) != len(relevant):
        problems.append(f"L{lineno} [{case.get('query')}] relevant 有重复")
    return problems


async def validate_products() -> list[str]:
    products = {p.product_id: p for p in await InMemoryProductRepository().list_all()}
    problems: list[str] = []

    for lineno, case in _load(_PRODUCT_DATASET):
        query = case["query"]
        problems.extend(_check_common(lineno, case))
        if case.get("kind") not in (None, "lexical", "semantic"):
            problems.append(f"L{lineno} [{query}] kind 取值非法：{case['kind']}")

        for pid in case.get("relevant", []):
            product = products.get(pid)
            if product is None:
                problems.append(f"L{lineno} [{query}] {pid} 不在商品库")
                continue

            # 价格约束：只在币种与约束口径一致时判定，避免在评测里重算汇率
            limit = case.get("price_max_major")
            if limit is not None:
                cheapest = min(sku.price.to_major_units() for sku in product.skus)
                currency = product.skus[0].price.currency
                if currency == "CNY" and cheapest > limit:
                    problems.append(
                        f"L{lineno} [{query}] {pid} 最低价 {cheapest}{currency} > 上限 {limit}"
                        f"（标注自相矛盾：会被硬约束挡掉的商品不该标为 relevant）",
                    )

            ship_to = case.get("ship_to")
            if ship_to and ship_to not in product.ships_to:
                problems.append(
                    f"L{lineno} [{query}] {pid} ships_to={product.ships_to} 不含 {ship_to}"
                    f"（标注自相矛盾）",
                )
    return problems


async def validate_catalog_fixture() -> list[str]:
    """校验运行时商品数据，而不只校验 query 金标引用是否存在。"""
    products = await InMemoryProductRepository().list_all()
    problems = validate_catalog_products(products) + validate_catalog_distribution(products)
    try:
        records = _load(_CATALOG_FIXTURE)
        problems.extend(validate_catalog_raw_records([record for _, record in records]))
    except (OSError, json.JSONDecodeError) as err:
        problems.append(f"无法读取版本化商品数据：{err}")
    return problems


async def validate_knowledge_fixture() -> list[str]:
    """把来源/时效元数据与 chunk 规模纳入统一发版前校验。"""
    try:
        manifest = load_knowledge_manifest(_KNOWLEDGE_DIR)
    except ValueError as err:
        return [str(err)]
    problems = validate_knowledge_manifest(_KNOWLEDGE_DIR, manifest)
    problems.extend(validate_knowledge_content(_KNOWLEDGE_DIR))
    chunk_count = await count_knowledge_chunks(_KNOWLEDGE_DIR)
    if not 150 <= chunk_count <= 250:
        problems.append(f"知识 chunk 数 {chunk_count} 不在 [150, 250] 范围内")
    return problems


def validate_official_eval_fixture() -> list[str]:
    """正式集的规模、分桶和 split 隔离，复用统一数据校验入口。"""
    problems, _ = _validate_official_eval_fixture(_FORMAL_EVAL_DIR)
    return problems


def validate_categories() -> list[str]:
    docs = {p.name for p in _KNOWLEDGE_DIR.glob("*.md")}
    problems: list[str] = []
    for lineno, case in _load(_CATEGORY_DATASET):
        problems.extend(_check_common(lineno, case))
        for name in case.get("relevant", []):
            if name not in docs:
                problems.append(
                    f"L{lineno} [{case['query']}] 知识文档不存在：{name}"
                    f"（标注单位应为 knowledge/*.md 的文件名）",
                )
    return problems


async def main() -> None:
    product_problems = await validate_products()
    category_problems = validate_categories()
    catalog_problems = await validate_catalog_fixture()
    knowledge_problems = await validate_knowledge_fixture()
    formal_eval_problems = validate_official_eval_fixture()

    print(f"{_PRODUCT_DATASET}：{len(_load(_PRODUCT_DATASET))} 条")
    print(f"{_CATEGORY_DATASET}：{len(_load(_CATEGORY_DATASET))} 条")

    problems = product_problems + category_problems + catalog_problems + knowledge_problems + formal_eval_problems
    if problems:
        print(f"\n发现 {len(problems)} 处问题：")
        for message in problems:
            print("  -", message)
        sys.exit(1)
    print("\n标注集自检通过")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
