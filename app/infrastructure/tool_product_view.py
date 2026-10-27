"""从已保存的单次商品工具结果生成只读小卡，不回传工具原文。"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import unquote, urlsplit


_PRODUCT_LIMIT = 50
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value.strip()[:limit]


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _amount(value):
    # bool 也是 int 的子类；不能把 true 当作价格 1，NaN/Infinity 也不能流入 JSON。
    return type(value) in {int, float} and 0 <= value <= 1_000_000_000 and math.isfinite(value)


def _image_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    decoded = unquote(value)
    if any(char.isspace() or ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in decoded) or "\\" in decoded:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.scheme in {"https", "http"} and parsed.hostname and parsed.port != 0:
            return value
        # 本地商品图片只允许明确的目录来源，排除 // 外站链接与路径穿越。
        if (not parsed.scheme and not parsed.netloc and decoded.startswith("/products/")
                and ".." not in decoded.split("/")):
            return value
    except ValueError:
        pass
    return None


def _product(hit):
    if not isinstance(hit, dict):
        return None
    identifier, title = _identifier(hit.get("product_id")), _text(hit.get("title"), 240)
    price, currency = hit.get("price_major"), hit.get("currency")
    if (not identifier or not title or not _amount(price)
            or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        return None
    card = {"product_id": identifier, "title": title, "price_major": price, "currency": currency}
    # 分页查询可能显式选择另一个 SKU，此时顶层价格属于 sku_id 指定的规格。
    selected = _identifier(hit.get("sku_id")) or _identifier(hit.get("default_sku_id"))
    if selected:
        card["default_sku_id"] = selected
        skus = hit.get("skus")
        matching = next((sku for sku in skus if isinstance(sku, dict) and sku.get("sku_id") == selected), None) if isinstance(skus, list) else None
        spec = _text(matching.get("spec"), 160) if matching else _text(hit.get("spec"), 160)
    else:
        spec = _text(hit.get("spec"), 160)
    if spec:
        card["spec"] = spec
    image = _image_url(hit.get("image_url"))
    if image:
        card["image_url"] = image
        if hit.get("image_kind") in ("illustration", "placeholder"):
            card["image_kind"] = hit["image_kind"]
    return card


def tool_product_view(content):
    """仅解释完整 JSON 对象中的 hits；上下文分页片段不拼接、不冒充完整结果。"""
    unavailable = {"status": "unavailable", "products": [], "count": 0, "totalCount": None,
                   "truncated": False, "notice": "该工具结果没有可展示的商品记录。"}
    if not isinstance(content, str) or len(content) > 2_000_000:
        return unavailable
    try:
        payload = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return unavailable
    if (not isinstance(payload, dict) or not isinstance(payload.get("hits"), list)
            or payload.get("success") is False or payload.get("ok") is False
            or bool(payload.get("error")) or payload.get("status") in ("error", "failed")):
        return unavailable
    hits = payload["hits"]
    # total_candidates 是筛选前后召回池的大小，不能当作此次工具返回的商品数。
    paged = any(key in payload for key in ("total", "offset", "next_offset", "fragment", "incomplete", "next_field_offset"))
    offset = _count(payload.get("offset", 0))
    total = _count(payload.get("total")) if paged else len(hits)
    if total is not None and (offset is None or total < offset + len(hits)):
        total = None
    partial = (total is None or total > len(hits) or offset != 0
               or payload.get("incomplete") is True or "fragment" in payload
               or payload.get("next_offset") is not None or payload.get("next_field_offset") is not None)
    products = []
    for hit in hits:
        card = _product(hit)
        if card is None:
            partial = True
        elif len(products) < _PRODUCT_LIMIT:
            products.append(card)
        else:
            partial = True
            break
    count = len(products)
    notice = "仅展示本次调用保存的商品信息，价格与库存以重新查询为准。"
    if partial:
        notice = (f"本次保存的结果仅可展示 {count} 件商品，分页或不完整字段未展示。"
                  "价格与库存以重新查询为准。")
    elif not count:
        notice = "本次检索没有返回商品。"
    return {"status": "ready", "products": products, "count": count, "totalCount": total,
            "truncated": partial, "notice": notice}
