# -*- coding: utf-8 -*-
"""正式知识评测的拒答与政策来源门禁。"""
from types import SimpleNamespace

from scripts.eval.run_category_recall import run_dataset


class FakeKnowledgeBase:
    async def search(self, queries, top_k):
        return self.hits_by_query[queries[0]][:top_k]

    def __init__(self, hits_by_query):
        self.hits_by_query = hits_by_query


def _hit(source: str, score: float, metadata: dict | None = None):
    return SimpleNamespace(
        document_id=source,
        score=score,
        chunk=SimpleNamespace(metadata={"source": source, **(metadata or {})}),
    )


async def test_knowledge_runner_scores_score_based_abstention_and_invalid_policy_rejection():
    knowledge_base = FakeKnowledgeBase({
        "没有证据的问题": [_hit("noise.md", 0.19)],
        "过期政策": [_hit("policy.md", 0.9, {
            "topic": "policy", "source_reference": "合成快照",
            "source_type": "synthetic_evaluation_fixture", "effective_from": "2025-01-01",
            "effective_to": "2026-12-31",
        })],
    })
    cases = [
        {"query": "没有证据的问题", "relevant": [], "expected_unanswerable": True},
        {"query": "过期政策", "relevant": ["policy.md"], "expected_behavior": "拒绝确定事实", "kind": "conflict_or_expired"},
    ]

    observations = []
    aggregate = await run_dataset(knowledge_base, cases, top_k=3, observations=observations)

    assert aggregate.empty_accuracy == 1.0
    assert aggregate.policy_rejection_accuracy == 1.0
    assert aggregate.recall == 1.0
    assert len(observations) == 2
    assert observations[0]["unanswerable_pass"] is True
    assert observations[1]["policy_pass"] is True
