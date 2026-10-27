# -*- coding: utf-8 -*-
"""验证脚本自身的合同检查；不连接真实服务或模型。"""
import json

import pytest

from scripts.verify_ag_ui import ContractViolation, StreamAudit
from tests.test_ag_ui import collect, make_orchestrator


def feed(audit, event):
    audit.accept(json.dumps(event), 1.0)


def started_audit():
    audit = StreamAudit("thread", "run")
    feed(audit, {"type": "RUN_STARTED", "threadId": "thread", "runId": "run"})
    return audit


async def test_verifier_accepts_actual_application_events_and_redacts_report():
    orchestrator, _, _ = make_orchestrator()
    events = await collect(orchestrator)
    audit = StreamAudit("session-test", "run-test")
    for index, event in enumerate(events):
        audit.accept(json.dumps(event), float(index))
    audit.finish()
    report = audit.report(100)
    assert report["finalState"]["productCount"] > 0
    assert report["tools"][0]["argumentsJsonValid"] is True
    assert report["tools"][0]["resultReceived"] is True
    assert report["retrievalEvidence"][0]["recall_strategy"] == "keyword_2gram"
    assert report["retrievalEvidence"][0]["rerank_applied"] is False
    assert report["retrievalEvidence"][0]["hit_count"] == report["finalState"]["productCount"]
    assert report["retrievalSummary"]["allObservedSearchesUseEmbeddingAndRerank"] is False
    assert report["retrievalSummary"]["onlineMainQualityGates"] == "not_evaluated"
    rendered = json.dumps(report, ensure_ascii=False)
    assert "正在查找商品" not in rendered
    assert "normalized_query" not in rendered
    assert "这是根据商品库" not in rendered


def test_verifier_uses_official_required_fields():
    with pytest.raises(ContractViolation, match="official_event_schema_invalid"):
        feed(StreamAudit("thread", "run"), {"type": "RUN_STARTED"})


def test_verifier_rejects_tool_result_before_arguments_end():
    audit = started_audit()
    feed(audit, {"type": "TOOL_CALL_START", "toolCallId": "call", "toolCallName": "product_search_tool"})
    with pytest.raises(ContractViolation, match="tool_result_before_args_end"):
        feed(audit, {"type": "TOOL_CALL_RESULT", "toolCallId": "call", "messageId": "result", "content": "{}"})


def test_verifier_observes_invalid_arguments_without_confusing_end_with_validity():
    audit = started_audit()
    feed(audit, {"type": "TOOL_CALL_START", "toolCallId": "call", "toolCallName": "product_search_tool"})
    feed(audit, {"type": "TOOL_CALL_ARGS", "toolCallId": "call", "delta": "{"})
    feed(audit, {"type": "TOOL_CALL_END", "toolCallId": "call"})
    assert audit.tools["call"]["argumentsEnded"] is True
    assert audit.tools["call"]["argumentsJsonValid"] is False


@pytest.mark.parametrize("tool", ["create_order_tool", "cancel_order_tool", "remember_preference_tool"])
def test_verifier_rejects_write_tool(tool):
    with pytest.raises(ContractViolation, match="out_of_scope_tool_requested"):
        feed(started_audit(), {"type": "TOOL_CALL_START", "toolCallId": "call", "toolCallName": tool})


def test_verifier_rejects_missing_terminal():
    with pytest.raises(ContractViolation, match="terminal_missing"):
        started_audit().finish()


@pytest.mark.parametrize("strategy, reranked, full_chain", [
    ("embedding_only", False, False),
    ("embedding_rerank", True, True),
    ("embedding_rerank", False, False),
])
def test_retrieval_evidence_records_actual_strategy_without_claiming_quality_gates(strategy, reranked, full_chain):
    audit = started_audit()
    feed(audit, {"type": "TOOL_CALL_START", "toolCallId": "call", "toolCallName": "product_search_tool"})
    feed(audit, {"type": "TOOL_CALL_ARGS", "toolCallId": "call", "delta": "{}"})
    feed(audit, {"type": "TOOL_CALL_END", "toolCallId": "call"})
    feed(audit, {
        "type": "TOOL_CALL_RESULT", "toolCallId": "call", "messageId": "result",
        "content": json.dumps({
            "recall_strategy": strategy, "rerank_applied": reranked, "total_candidates": 7,
            "hits": [{"product_id": "product-secret-marker", "title": "不能输出的正文"}],
        }),
    })
    report = audit.report(10)
    assert report["retrievalEvidence"] == [{
        "toolCallId": "call", "recall_strategy": strategy,
        "rerank_applied": reranked, "hit_count": 1, "total_candidates": 7,
    }]
    assert report["retrievalSummary"]["allObservedSearchesUseEmbeddingAndRerank"] is full_chain
    assert report["retrievalSummary"]["onlineMainQualityGates"] == "not_evaluated"
    assert "不能输出的正文" not in json.dumps(report, ensure_ascii=False)
    assert "product-secret-marker" not in json.dumps(report)


def test_missing_retrieval_evidence_does_not_claim_main_chain():
    report = started_audit().report(10)
    assert report["retrievalEvidence"] == []
    assert report["retrievalSummary"]["allObservedSearchesUseEmbeddingAndRerank"] is False
