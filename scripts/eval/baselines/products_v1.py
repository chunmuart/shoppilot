"""商品上下文的无损入口投影、有界分页与历史引用。"""
from __future__ import annotations
import json
import math

# 仅移除展示/诊断字段，业务描述、全部规格和费用组成保留。
DISPLAY_FIELDS = {'image_url', 'images', 'thumbnail', 'embedding', 'rerank_score', 'debug', 'trace'}

def token_estimate(value) -> int:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return math.ceil(len(raw.encode('utf-8')) / 4 * 1.5)


def business_view(value):
    if isinstance(value, dict):
        return {k: business_view(v) for k, v in value.items() if k not in DISPLAY_FIELDS}
    if isinstance(value, list):
        return [business_view(v) for v in value]
    return value


FIELD_GROUPS = {
    'identity': {'product_id', 'title', 'default_sku_id', 'canonical_product_id'},
    'specs': {'product_id', 'title', 'skus', 'material_tags', 'description', 'highlights', 'dimensions_cm', 'weight_kg'},
    'price': {'product_id', 'title', 'skus', 'price_major', 'currency', 'landed_price', 'ships_to'},
}


def product_page(result, *, offset=0, limit=5, token_limit=3000, product_id='', sku_id='', position=0, fields='all', field_offset=0):
    """只在字段/商品边界分页；单字段过大时明确返回片段与下一偏移。"""
    if offset < 0 or field_offset < 0 or position < 0 or not 1 <= limit <= 5 or fields not in {*FIELD_GROUPS, 'all'}:
        raise ValueError('无效的分页位置、数量或字段组')
    hits = result.get('hits', [])
    if position:
        hits = hits[position-1:position]
    hits = [h for h in hits if (not product_id or h.get('product_id') == product_id)
            and (not sku_id or sku_id == h.get('default_sku_id') or any(s.get('sku_id') == sku_id for s in h.get('skus', [])))]
    page = {'hits': [], 'total': len(hits), 'offset': offset, 'next_offset': None,
            'result_ref': result.get('result_ref'), 'historical': True,
            'available_field_groups': ['all', *FIELD_GROUPS]}
    for index in range(offset, min(len(hits), offset + limit)):
        hit = business_view(hits[index])
        if sku_id and 'skus' in hit:
            hit['skus'] = [s for s in hit['skus'] if s.get('sku_id') == sku_id]
        if fields != 'all':
            hit = {k: v for k, v in hit.items() if k in FIELD_GROUPS[fields]}
        candidate = {**page, 'hits': [*page['hits'], hit]}
        if token_estimate(candidate) > token_limit:
            if page['hits']:
                break
            # 保留结构化身份；巨大业务字段按序列化字符分页，绝不伪装成完整结果。
            raw = json.dumps(hit, ensure_ascii=False)
            span = max(64, token_limit // 2)
            excerpt = raw[field_offset:field_offset+span]
            while token_estimate({**page, 'fragment': excerpt}) > token_limit and len(excerpt) > 32:
                excerpt = excerpt[:len(excerpt)//2]
            page.update(fragment=excerpt, fragment_format='json_text', fragment_product_id=hit.get('product_id'),
                        field_offset=field_offset, next_field_offset=field_offset+len(excerpt) if field_offset+len(excerpt)<len(raw) else None,
                        next_offset=index if field_offset+len(excerpt)<len(raw) else (index+1 if index+1<len(hits) else None),
                        incomplete=True)
            return page
        page['hits'].append(hit)
    following = offset + len(page['hits'])
    page['next_offset'] = following if following < len(hits) else None
    return page


def result_identity(hit, query):
    """报价条件不同的结果不得按 product_id 合并。"""
    price = hit.get('landed_price') or {}
    return (hit.get('product_id'), tuple(s.get('sku_id') for s in hit.get('skus', [])) or (hit.get('sku_id') or hit.get('default_sku_id'),),
            price.get('ship_to', query.get('ship_to')), price.get('currency', hit.get('currency', query.get('currency'))),
            price.get('quantity', query.get('quantity', 1)))
