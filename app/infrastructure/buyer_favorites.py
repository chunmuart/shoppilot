"""买家收藏持久化；商品快照仅用于展示，交易仍重新核验目录与价格。"""
import asyncio
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

class BuyerFavoriteStore:
    def __init__(self,path:Path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS buyer_favorites (buyer_id TEXT NOT NULL, product_id TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(buyer_id,product_id))')

    async def list(self,buyer):
        def read():
            with sqlite3.connect(self.path) as db:
                return [json.loads(r[0]) for r in db.execute('SELECT snapshot FROM buyer_favorites WHERE buyer_id=? ORDER BY created_at DESC,product_id',(buyer,))]
        return await asyncio.to_thread(read)

    async def save(self,buyer,product):
        def write():
            with sqlite3.connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                exists=db.execute('SELECT 1 FROM buyer_favorites WHERE buyer_id=? AND product_id=?',(buyer,product['product_id'])).fetchone()
                if not exists and db.execute('SELECT count(*) FROM buyer_favorites WHERE buyer_id=?',(buyer,)).fetchone()[0]>=100:
                    raise ValueError('最多收藏100件商品，请先整理收藏')
                db.execute('INSERT INTO buyer_favorites VALUES (?,?,?,?) ON CONFLICT(buyer_id,product_id) DO UPDATE SET snapshot=excluded.snapshot',
                    (buyer,product['product_id'],json.dumps(product,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        await asyncio.to_thread(write)

    async def delete(self,buyer,product_id):
        def remove():
            with sqlite3.connect(self.path) as db:db.execute('DELETE FROM buyer_favorites WHERE buyer_id=? AND product_id=?',(buyer,product_id))
        await asyncio.to_thread(remove)
