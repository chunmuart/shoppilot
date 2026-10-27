# -*- coding: utf-8 -*-
"""知识库评测语料的来源、时效与规模校验。"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from agentscope.rag import ApproxTokenChunker, TextParser


_MANIFEST = "manifest.jsonl"
_REQUIRED = {
    "document_id", "filename", "source", "source_type", "published_at",
    "effective_from", "effective_to", "region", "version", "topic",
}


def load_knowledge_manifest(knowledge_dir: Path) -> list[dict[str, Any]]:
    path = knowledge_dir / _MANIFEST
    if not path.exists():
        raise ValueError(f"知识库元数据不存在：{path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_knowledge_manifest(knowledge_dir: Path, manifest: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for entry in manifest:
        missing = _REQUIRED - set(entry)
        label = entry.get("document_id", "<unknown>")
        if missing:
            problems.append(f"{label} 缺少元数据：{','.join(sorted(missing))}")
            continue
        if entry["document_id"] in seen_ids:
            problems.append(f"document_id 重复：{entry['document_id']}")
        if entry["filename"] in seen_files:
            problems.append(f"filename 重复：{entry['filename']}")
        seen_ids.add(entry["document_id"])
        seen_files.add(entry["filename"])
        if not (knowledge_dir / entry["filename"]).is_file():
            problems.append(f"{label} 对应文件不存在：{entry['filename']}")
        try:
            published = date.fromisoformat(entry["published_at"])
            start = date.fromisoformat(entry["effective_from"])
            end = date.fromisoformat(entry["effective_to"])
            if not published <= end or start > end:
                problems.append(f"{label} 日期范围非法")
        except ValueError:
            problems.append(f"{label} 日期必须使用 ISO 格式")
        if not entry["source"].strip():
            problems.append(f"{label} 缺少来源")
        if entry["topic"] == "policy" and entry["source_type"] not in {"official_snapshot", "synthetic_evaluation_fixture"}:
            problems.append(f"{label} 政策文档 source_type 非法")
    markdowns = {path.name for path in knowledge_dir.glob("*.md")}
    unregistered = markdowns - seen_files
    if unregistered:
        problems.append("未登记的知识文档：" + ",".join(sorted(unregistered)))
    return problems


def validate_knowledge_content(knowledge_dir: Path) -> list[str]:
    """拒绝以复制粘贴段落凑 chunk 数的知识库。"""
    owners: dict[str, set[str]] = defaultdict(set)
    for path in sorted(knowledge_dir.glob("*.md")):
        raw = re.sub(r"(?m)^#+\s*.*$", "", path.read_text(encoding="utf-8"))
        for sentence in re.split(r"(?<=[。！？])", raw):
            normalized = re.sub(r"\s+", "", sentence)
            # 标题、短提示语与公共免责声明不计入内容重复；实质事实才影响召回区分度。
            if len(normalized) < 40 or "合成的演示快照" in normalized:
                continue
            owners[normalized].add(path.name)
    problems = []
    for filenames in owners.values():
        if len(filenames) > 1:
            problems.append("知识库存在重复实质段落：" + ",".join(sorted(filenames)))
    return problems


async def count_knowledge_chunks(knowledge_dir: Path) -> int:
    parser, chunker = TextParser(), ApproxTokenChunker(chunk_size=512, overlap=50)
    total = 0
    for md_file in sorted(knowledge_dir.glob("*.md")):
        sections = await parser.parse(str(md_file), filename=md_file.name)
        total += len(await chunker.chunk(sections))
    return total
