"""只调用 embedding/reranker 做依赖预检，不请求聊天模型，不输出凭据。"""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from app.infrastructure.settings import load_settings
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.rerank.http_reranker import HttpReranker


def safe_endpoint(value):
    u = urlsplit(value)
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit(
        (u.scheme, host + (f":{u.port}" if u.port else ""), u.path, "", "")
    )


async def check(settings):
    result = {}
    for name, client, call in (
        (
            "embedding",
            OpenAIEmbeddingClient(settings),
            lambda c: c.embed("轻便旅行背包"),
        ),
        (
            "reranker",
            HttpReranker(settings),
            lambda c: c.rerank("轻便旅行背包", ["35L轻便旅行背包", "陶瓷咖啡杯"]),
        ),
    ):
        entry = {
            "endpoint": safe_endpoint(getattr(settings, name + "_base_url")),
            "model": getattr(settings, name + "_model"),
        }
        try:
            values = await asyncio.wait_for(call(client), 30)
            if name == "reranker" and not values[0] > values[1]:
                raise ValueError("语义顺序检查失败")
            entry.update(
                status="PASS",
                **(
                    {"scores": values}
                    if name == "reranker"
                    else {"dimensions": len(values)}
                ),
            )
        except Exception as error:
            entry.update(
                status="BLOCKED",
                error_type=type(error).__name__,
                error_code=getattr(error, "code", None),
            )
        result[name] = entry
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = asyncio.run(check(load_settings()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(e["status"] == "PASS" for e in result.values()) else 1)


if __name__ == "__main__":
    main()
