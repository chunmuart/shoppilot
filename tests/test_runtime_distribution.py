# -*- coding: utf-8 -*-
"""发布形态与评测命令的回归契约。"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
import re
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_category_recall_help_is_renderable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/eval/run_category_recall.py", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "品类知识库召回评测" in result.stdout


def test_docker_image_packages_catalog_outside_mutable_data_volume() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY data/catalog-v3.jsonl ./catalog/catalog-v3.jsonl" in dockerfile


def test_compose_passes_reranker_configuration_to_app_and_worker() -> None:
    compose = (PROJECT_ROOT / "docker/docker-compose.yaml").read_text(encoding="utf-8")

    assert compose.count("RERANKER_BASE_URL: ${RERANKER_BASE_URL-}") == 2
    assert compose.count("RERANKER_MODEL: ${RERANKER_MODEL-}") == 2


def test_qdrant_server_matches_locked_client_minor_version() -> None:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    client = next(package for package in lock["package"] if package["name"] == "qdrant-client")
    compose = (PROJECT_ROOT / "docker/docker-compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"image: qdrant/qdrant:v(\d+\.\d+)\.\d+", compose)

    assert match is not None
    assert match.group(1) == ".".join(client["version"].split(".")[:2])


def test_compose_shares_context_and_independent_embedding_configuration():
    compose = yaml.safe_load((PROJECT_ROOT / "docker/docker-compose.yaml").read_text())
    required = {
        "CONTEXT_STRATEGY": "${CONTEXT_STRATEGY:-layered}",
        "CONTEXT_PRUNING_TIMING": "${CONTEXT_PRUNING_TIMING:-pressure}",
        "CONTEXT_PRODUCT_TOKENS": "${CONTEXT_PRODUCT_TOKENS:-6000}",
        "CONTEXT_TARGET_TOKENS": "${CONTEXT_TARGET_TOKENS:-48000}",
        "EMBEDDING_BASE_URL": "${EMBEDDING_BASE_URL-}",
        "EMBEDDING_API_KEY": "${EMBEDDING_API_KEY-}",
        "EMBEDDING_DIM": "${EMBEDDING_DIM:-1024}",
    }
    for service in ("app", "worker"):
        environment = compose["services"][service]["environment"]
        for key, value in required.items():
            assert environment.get(key) == value, (service, key)


def test_empty_optional_embedding_configuration_falls_back_to_llm(monkeypatch,tmp_path):
    from app.infrastructure.settings import load_settings
    monkeypatch.setenv("LLM_BASE_URL","https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY","placeholder")
    monkeypatch.setenv("DATA_DIR",str(tmp_path))
    for key in ("EMBEDDING_BASE_URL","EMBEDDING_API_KEY"):
        monkeypatch.setenv(key,"")
    settings=load_settings()
    assert settings.embedding_base_url==settings.llm_base_url
    assert settings.embedding_api_key==settings.llm_api_key
