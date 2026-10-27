"""展示媒体独立于商品、会话与模型历史；资源必须随工程可发布。"""
import json
from pathlib import Path
import httpx
from fastapi import FastAPI
from app.presentation.product_media import media_manifest, register_product_media_routes

ROOT = Path(__file__).resolve().parents[1]

async def test_public_manifest_is_readonly_and_contains_no_buyer_fields():
    api = FastAPI()
    register_product_media_routes(api)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
        response = await client.get('/commerce/product-media')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'public, max-age=300'
        assert set(response.json()) == {'version', 'kind', 'assets', 'products', 'skus'}
        assert (await client.post('/commerce/product-media')).status_code == 405


def test_manifest_references_packaged_images_and_exact_catalog_skus():
    manifest = media_manifest()
    records = {p['product_id']: p for p in map(json.loads, (ROOT/'data/catalog-v3.jsonl').read_text().splitlines())}
    assert manifest['kind'] == 'illustration'
    assert len(manifest['products']) == 3440
    for identifier, name in manifest['products'].items():
        assert identifier in records
        assert name in manifest['assets']
    for sku, pid in manifest['skus'].items():
        assert sku in {s['sku_id'] for s in records[pid]['skus']}
    for asset in manifest['assets'].values():
        assert (ROOT/'frontend/public'/asset['url'].lstrip('/')).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        if 'cell' in asset:
            assert 0 <= asset['cell'] < 16 and asset['columns'] == asset['rows'] == 4
    # 各平台语种的同类型样例共用概念图，未知商品必须没有映射。
    assert manifest['products']['P10000'] == manifest['products']['P11000'] == 'backpack'
    assert 'P-NO-MEDIA' not in manifest['products']
    assert manifest['products']['P1003'] == 'P1003'
