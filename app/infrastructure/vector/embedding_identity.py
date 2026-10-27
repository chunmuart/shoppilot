"""商品向量身份：文本、服务、模型版本必须同时一致，才能复用。"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from app.domain.catalog.product import Product
from app.infrastructure.settings import Settings


def embedding_identity(settings: Settings) -> str:
    url = urlsplit(settings.embedding_base_url.rstrip('/'))
    host = url.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    endpoint = urlunsplit((url.scheme, host + (f':{url.port}' if url.port else ''), url.path, '', ''))
    identity = {'endpoint': endpoint, 'model': settings.embedding_model,
                'version': settings.embedding_version, 'configured_dimension': settings.embedding_dim}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def product_embedding_payload(product: Product, model_key: str) -> dict[str, str]:
    text_hash = hashlib.sha256(product.searchable_text().encode()).hexdigest()
    return {'product_id': product.product_id, 'embedding_key': model_key,
            'text_hash': text_hash, 'embedding_schema': 'product-text-v1'}
