"""专用检索服务的 HTTP 契约；单测不用下载模型。"""

from types import SimpleNamespace
from fastapi.testclient import TestClient
import pytest
from scripts.retrieval_server import create_app, EMBEDDING_MODEL, RERANKER_MODEL


class Model:
    def __init__(self):
        self.model = SimpleNamespace(
            tokenizer=SimpleNamespace(
                encode=lambda text, pair=None: SimpleNamespace(
                    ids=list(text + (pair or ""))
                )
            )
        )

    def embed(self, texts, **kwargs):
        return [SimpleNamespace(tolist=lambda: [0.1, 0.9]) for _ in texts]

    def rerank(self, query, documents, **kwargs):
        return [0.9 if "背包" in text else 0.1 for text in documents]


@pytest.fixture
def client():
    with TestClient(create_app(model_loader=lambda _: (Model(), Model()))) as c:
        yield c


def test_only_embedding_and_reranker_are_served(client):
    response = client.post(
        "/v1/rerank",
        json={
            "model": RERANKER_MODEL,
            "query": "背包",
            "documents": ["水杯", "背包"],
            "top_n": 2,
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["index"] == 1
    assert response.json()["usage"]["input_tokens"] > 0
    assert client.post("/v1/chat/completions", json={}).status_code == 404
    assert client.post(
        "/v1/embeddings", json={"model": EMBEDDING_MODEL, "input": ["背包"], "encoding_format": "float", "dimensions": 512}
    ).json()["data"][0]["embedding"] == [0.1, 0.9]


def test_long_pair_is_rejected_without_silent_truncation(client):
    r = client.post(
        "/v1/rerank",
        json={"model": RERANKER_MODEL, "query": "背包", "documents": ["文" * 512]},
    )
    assert r.status_code == 413


def test_wrong_model_and_oversize_batch_are_rejected(client):
    assert (
        client.post(
            "/v1/rerank", json={"model": "chat", "query": "q", "documents": ["x"]}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/rerank",
            json={"model": RERANKER_MODEL, "query": "q", "documents": ["x"] * 129},
        ).status_code
        == 422
    )


def test_long_embedding_uses_all_chunks_without_dropping_tail(client):
    r = client.post("/v1/embeddings", json={"model":EMBEDDING_MODEL, "input":["前"*600+"后"*600], "encoding_format":"float"})
    assert r.status_code == 200
    assert r.json()["chunk_counts"] == [4]
    assert r.json()["usage"]["prompt_tokens"] == 1200
    assert len(r.json()["data"]) == 1


def test_embedding_limits_total_work_without_truncation(client):
    r=client.post("/v1/embeddings",json={"model":EMBEDDING_MODEL,"input":["文"*32769]})
    assert r.status_code==413
