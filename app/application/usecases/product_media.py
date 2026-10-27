# -*- coding: utf-8 -*-
"""商品展示媒体：仅映射已生成的示意资源，不推断真实平台商品照片。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProductMedia:
    image_url: str | None
    image_kind: Literal["illustration", "placeholder"]
    image_alt: str


# 这些资源由前端静态目录托管，图像来源与使用边界见 products/README.md。
# 映射在商品层，不代表某个 SKU 的实际颜色、材质或尺寸。
_ILLUSTRATION_FILES = {
    "P1003": "wanderlite.png",
    "P1018": "drypack.png",
    "P1049": "budgetpack.png",
}


def product_media(product_id: str, title: str) -> ProductMedia:
    filename = _ILLUSTRATION_FILES.get(product_id)
    if filename is None:
        return ProductMedia(None, "placeholder", f"{title}：暂无商品图片")
    return ProductMedia(
        f"/products/{filename}", "illustration", f"{title}：AI 生成示意图，非商品实拍",
    )
