"""独立评测使用的真实本地多语言模型；不改应用的远端模型配置。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import numpy as np

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
ENCODING_VERSION = 'mean-pooling-onnx-q-content-chunks-128-v1'


def model_files(cache_dir: Path) -> dict[str, str]:
    files = {str(p.relative_to(cache_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(cache_dir.rglob('*')) if p.is_file() and p.suffix in ('.onnx', '.json')}
    if not any(name.endswith('.onnx') for name in files):
        raise ValueError('未找到本地模型文件，先准备模型缓存')
    return files


def model_version(cache_dir: Path) -> str:
    digest = hashlib.sha256(json.dumps(model_files(cache_dir), sort_keys=True).encode()).hexdigest()
    return f'{ENCODING_VERSION}:{digest}'


class LocalMultilingualEmbedding(EmbeddingClient):
    def __init__(self, cache_dir: Path):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(MODEL, cache_dir=str(cache_dir), threads=4)
        self.model.model.tokenizer.no_truncation()
        self.input_texts = 0
        self.encoded_chunks = 0

    async def embed(self, text):
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts):
        if not texts:
            return []
        def encode():
            tokenizer = self.model.model.tokenizer
            def split(text):
                if len(tokenizer.encode(text).ids) <= 128:
                    return [text]
                middle = len(text) // 2
                if middle == 0:
                    raise ValueError('单个字符超过模型分块限制')
                return split(text[:middle]) + split(text[middle:])
            groups = [split(text) for text in texts]
            flat = [part for group in groups for part in group]
            weights = [len(tokenizer.encode(part).ids) for part in flat]
            vectors = list(self.model.embed(flat, batch_size=16))
            result = []; offset = 0
            for group in groups:
                size = len(group)
                value = np.average(vectors[offset:offset + size], axis=0, weights=weights[offset:offset + size])
                norm = float(np.linalg.norm(value))
                if not np.isfinite(value).all() or norm == 0:
                    raise ValueError('本地模型返回无效向量')
                result.append((value / norm).tolist()); offset += size
            self.input_texts += len(texts); self.encoded_chunks += len(flat)
            return result
        return await asyncio.to_thread(encode)
