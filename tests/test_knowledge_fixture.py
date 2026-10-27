# -*- coding: utf-8 -*-
"""知识库评测数据的规模和元数据契约。"""
from __future__ import annotations

from pathlib import Path
from datetime import date

import pytest

from scripts.eval.knowledge_quality import count_knowledge_chunks, load_knowledge_manifest, validate_knowledge_manifest


_ROOT = Path(__file__).resolve().parents[1]


def test_knowledge_fixture_has_versioned_metadata_and_required_document_count():
    manifest = load_knowledge_manifest(_ROOT / "knowledge")

    assert len(manifest) >= 40
    assert validate_knowledge_manifest(_ROOT / "knowledge", manifest) == []
    assert {entry["region"] for entry in manifest} >= {"GLOBAL", "US", "EU", "JP", "SG", "CN"}


@pytest.mark.asyncio
async def test_knowledge_fixture_chunk_count_is_large_enough_to_make_recall_discriminative():
    chunk_count = await count_knowledge_chunks(_ROOT / "knowledge")

    assert 150 <= chunk_count <= 250


def test_runtime_knowledge_loader_exposes_manifest_metadata():
    from app.infrastructure.rag.category_knowledge import load_knowledge_metadata

    metadata = load_knowledge_metadata(_ROOT / "knowledge")
    policy = metadata["eval-policy-us"]

    assert policy["region"] == "US"
    assert policy["source"]
    assert policy["effective_to"] == "2026-12-31"


def test_knowledge_quality_rejects_repeated_substantive_paragraphs(tmp_path):
    from scripts.eval.knowledge_quality import validate_knowledge_content

    repeated = "这是一段足够长、会被重复内容检查器识别的知识文本，用于验证知识库不是靠复制粘贴凑 chunk 数。"
    (tmp_path / "a.md").write_text(f"# A\n\n{repeated}\n", encoding="utf-8")
    (tmp_path / "b.md").write_text(f"# B\n\n{repeated}\n", encoding="utf-8")

    assert validate_knowledge_content(tmp_path) == ["知识库存在重复实质段落：a.md,b.md"]


def test_policy_without_authoritative_source_or_after_effective_date_is_not_fact_eligible():
    from app.infrastructure.rag.category_knowledge import policy_fact_status

    assert policy_fact_status(
        {"topic": "policy", "source_reference": "合成快照", "source_type": "synthetic_evaluation_fixture", "effective_to": "2026-12-31"},
        today=date(2026, 8, 29),
    ) == "non_authoritative_source"
    assert policy_fact_status(
        {"topic": "policy", "source_reference": "官方公告", "source_type": "official_snapshot", "effective_from": "2025-01-01", "effective_to": "2026-01-01"},
        today=date(2026, 8, 29),
    ) == "expired"


def test_knowledge_relevance_threshold_rejects_an_out_of_domain_hit():
    from app.infrastructure.rag.category_knowledge import has_answerable_knowledge

    class Hit:
        def __init__(self, score: float) -> None:
            self.score = score

    assert has_answerable_knowledge([Hit(0.19)]) is False
    assert has_answerable_knowledge([Hit(0.20)]) is True


@pytest.mark.asyncio
async def test_dataset_validator_includes_knowledge_metadata_and_chunk_gate():
    from scripts.eval.validate_datasets import validate_knowledge_fixture

    assert await validate_knowledge_fixture() == []
