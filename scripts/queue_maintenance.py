# -*- coding: utf-8 -*-
"""队列安全归档；默认仅检查，--apply 才删除已 ACK 且过期的 Stream 消息。"""
import argparse
import asyncio
import json

import redis.asyncio as redis

from app.infrastructure.settings import load_settings
from app.infrastructure.queue.archive import QueueArchive
from app.infrastructure.queue.maintenance import maintain_queue


async def run(args):
    settings = load_settings()
    if not settings.redis_url:
        raise SystemExit("未配置 REDIS_URL，不进行队列清理")
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        cursor_path = settings.data_dir / "queue_maintenance_cursors.json"
        cursors = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
        result = await maintain_queue(client, QueueArchive(settings.data_dir / "queue_archive.db"),
            older_than_seconds=args.older_than_days * 86400, limit=args.limit, apply=args.apply, cursors=cursors)
        if args.apply:
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            cursor_path.write_text(json.dumps(result["next_cursors"]))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-days", type=float, default=7)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))
