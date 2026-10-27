"""按应用当前模型配置补齐商品向量；已有相同文本和模型的向量直接复用。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex


async def sync() -> dict:
    settings = load_settings()
    index = QdrantProductIndex(settings)
    report = {'model': settings.embedding_model, 'model_version': settings.embedding_version,
              'model_key': index.embedding_key, 'collection': settings.qdrant_collection}
    try:
        await bootstrap_product_index(InMemoryProductRepository(), OpenAIEmbeddingClient(settings), index, report=report)
    finally:
        await index.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('报告已存在，请使用新路径保留上次结果')
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(sync())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.get('complete') else 1)


if __name__ == '__main__':
    main()
