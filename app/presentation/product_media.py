"""公开商品目录的展示媒体；不参与模型输入，也不回写历史商品快照。"""
from functools import lru_cache
import json
from pathlib import Path
from fastapi.responses import JSONResponse

@lru_cache(maxsize=1)
def media_manifest():
    return json.loads((Path(__file__).parent / 'assets/product-media-v1.json').read_text(encoding='utf-8'))

def register_product_media_routes(api):
    @api.get('/commerce/product-media')
    async def product_media():
        # 不含买家信息和凭据，浏览器可短时缓存；素材 URL 含版本可长期缓存。
        return JSONResponse(media_manifest(), headers={'Cache-Control': 'public, max-age=300'})
