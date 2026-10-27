"""本机专用检索模型服务：稠密 embedding + cross-encoder reranker，没有聊天模型接口。

显式启动后供原 HTTP 客户端访问；不会自动改动 .env 或替换远端服务。
模型权重首次下载，默认仅监听 127.0.0.1。依赖 uv sync --extra retrieval。
"""

from contextlib import asynccontextmanager
import asyncio
import math
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
RERANKER_MODEL = "Xenova/bge-reranker-base-int8"


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    input: list[str] = Field(min_length=1, max_length=32)
    encoding_format: Literal["float"] = "float"
    dimensions: Literal[512] | None = None


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    query: str = Field(min_length=1, max_length=1024)
    documents: list[str] = Field(min_length=1, max_length=128)
    top_n: int | None = Field(default=None, ge=1, le=128)


def load_models(cache_dir):
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    from fastembed.common.model_description import ModelSource

    if RERANKER_MODEL not in {
        m["model"] for m in TextCrossEncoder.list_supported_models()
    }:
        TextCrossEncoder.add_custom_model(
            model=RERANKER_MODEL,
            sources=ModelSource(hf="Xenova/bge-reranker-base"),
            model_file="onnx/model_int8.onnx",
            license="mit",
            description="BGE 中文/英文专用 cross-encoder，ONNX int8",
        )
    embedding = TextEmbedding(EMBEDDING_MODEL, cache_dir=str(cache_dir), threads=4)
    reranker = TextCrossEncoder(RERANKER_MODEL, cache_dir=str(cache_dir), threads=4)
    # 禁止底层静默截断；超窗显式报错，调用方才能记录降级。
    embedding.model.tokenizer.no_truncation()
    reranker.model.tokenizer.no_truncation()
    return embedding, reranker


def create_app(cache_dir=Path(".cache/retrieval-models"), model_loader=load_models):
    @asynccontextmanager
    async def lifespan(app):
        app.state.models = await asyncio.to_thread(model_loader, cache_dir)
        app.state.lock = asyncio.Lock()
        yield

    app = FastAPI(title="ShopPilot 专用检索模型", lifespan=lifespan)

    def token_count(model, texts, query=None):
        tokens = model.model.tokenizer
        counts = [
            len(
                tokens.encode(
                    text if query is None else query,
                    pair=None if query is None else text,
                ).ids
            )
            for text in texts
        ]
        if max(counts) > 512:
            raise HTTPException(413, "单条文本/查询文档对超过512 token，未执行截断")
        return sum(counts)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "embedding": EMBEDDING_MODEL,
            "reranker": RERANKER_MODEL,
            "chat_model": False,
        }

    @app.post("/v1/embeddings")
    async def embeddings(req: EmbeddingRequest):
        if req.model != EMBEDDING_MODEL:
            raise HTTPException(400, "未配置该 embedding 模型")
        async with app.state.lock:
            model = app.state.models[0]
            if sum(len(t) for t in req.input) > 32768:
                raise HTTPException(413, "单批向量文本超过32768字符，请分页")
            # 长文按真实 tokenizer 长度分块，不丢弃后半段；一条输入仍对应一条向量。
            def split_text(text):
                if len(model.model.tokenizer.encode(text).ids) <= 512:
                    return [text]
                middle = len(text)//2
                if not middle:
                    raise HTTPException(413, "单个字符超出模型窗口")
                return split_text(text[:middle]) + split_text(text[middle:])
            chunks = [split_text(text) for text in req.input]
            flattened = [text for group in chunks for text in group]
            lengths = [len(model.model.tokenizer.encode(text).ids) for text in flattened]
            usage = sum(lengths)
            vectors = await asyncio.to_thread(lambda: list(model.embed(flattened, batch_size=16)))
            import numpy as np
            pooled, offset = [], 0
            for group in chunks:
                size = len(group)
                # 单块维持原始向量，确保短商品文本的冻结评测不因长文兼容而变动。
                if size == 1:
                    vector = vectors[offset].tolist()
                else:
                    value = np.average([v.tolist() for v in vectors[offset:offset+size]], axis=0,
                                       weights=lengths[offset:offset+size])
                    norm = np.linalg.norm(value)
                    vector = (value / norm if norm else value).tolist()
                pooled.append(vector)
                offset += size
        return {
            "model": EMBEDDING_MODEL,
            "data": [{"index": i, "embedding": vector} for i, vector in enumerate(pooled)],
            "usage": {"prompt_tokens": usage, "total_tokens": usage},
            "chunk_counts": [len(group) for group in chunks],
        }

    @app.post("/v1/rerank")
    async def rerank(req: RerankRequest):
        if req.model != RERANKER_MODEL:
            raise HTTPException(400, "未配置该 reranker 模型")
        async with app.state.lock:
            model = app.state.models[1]
            usage = token_count(model, req.documents, req.query)
            scores = await asyncio.to_thread(
                lambda: list(model.rerank(req.query, req.documents, batch_size=8))
            )
        if len(scores) != len(req.documents) or not all(
            math.isfinite(s) for s in scores
        ):
            raise HTTPException(502, "精排模型输出无效")
        results = sorted(
            [{"index": i, "relevance_score": float(s)} for i, s in enumerate(scores)],
            key=lambda x: -x["relevance_score"],
        )
        return {
            "model": RERANKER_MODEL,
            "results": results[: req.top_n or len(results)],
            "usage": {"input_tokens": usage},
        }

    return app


def main():
    import argparse
    import uvicorn

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=18081)
    p.add_argument("--cache-dir", type=Path, default=Path(".cache/retrieval-models"))
    args = p.parse_args()
    uvicorn.run(create_app(args.cache_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
