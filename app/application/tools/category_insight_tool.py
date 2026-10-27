# -*- coding: utf-8 -*-
"""category_insight_tool

品类洞察工具（RAG）：回答"这个品类当前热卖什么、看哪些属性、价格区间、有什么坑"
这类选购常识问题，与 product_search_tool（出具体商品清单）分工明确。

注意：本模块不能用 `from __future__ import annotations`（AgentScope schema 生成依赖运行时注解）。
"""
import json
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.rag import KnowledgeBase
from agentscope.tool import ToolChunk

from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.rag.category_knowledge import (
    has_answerable_knowledge,
    keyword_fallback_insights,
    policy_fact_status,
)


def _chunk_text(content) -> str:
    """Chunk.content 是 TextBlock / DataBlock 而非纯字符串，统一归一为可序列化文本。"""
    if isinstance(content, str):
        return content
    text = getattr(content, "text", None)
    if text is not None:
        return text
    if isinstance(content, dict):
        return content.get("text") or str(content)
    return str(content)


def build_category_insight_tool(
    knowledge_base: KnowledgeBase,
    bus: TradeEventBus,
    fallback_knowledge_dir: Path | None = None,
):
    async def category_insight_tool(question: str, top_k: int = 3) -> ToolChunk:
        """查询品类洞察知识库：热卖款型、关键属性判断口径、价格区间、避坑点、跨境通则。

        适用于"这个品类怎么挑""现在流行什么""多少钱算合理""有什么坑"这类选购常识问题；
        需要具体商品清单与价格时用 product_search_tool。

        Args:
            question (`str`):
                自然语言问题，建议带上品类词，如"旅行装备怎么挑材质"、"美国免税额度多少"。
            top_k (`int`):
                返回知识片段数量，默认 3。
        """
        session_id = ShoppingContext.current_session_id()
        bus.publish(
            session_id,
            "tool.invoke",
            {"tool": "category_insight_tool", "args": {"question": question, "top_k": top_k}},
        )
        try:
            from app.infrastructure.rag.knowledge_retrieval import search_knowledge
            results = await search_knowledge(knowledge_base, question, top_k)
        except Exception as err:  # noqa: BLE001 —— 知识库不可用时如实降级，不编造洞察
            fallback = (
                keyword_fallback_insights(question, fallback_knowledge_dir, top_k)
                if fallback_knowledge_dir is not None
                else []
            )
            if fallback:
                for insight in fallback:
                    insight["policy_fact_status"] = policy_fact_status(insight["metadata"])
                bus.publish(session_id, "tool.result", {
                    "tool": "category_insight_tool",
                    "hit_count": len(fallback),
                    "abstained": False,
                    "retrieval_mode": "keyword_fallback",
                    "degraded_reason": str(err),
                    "policy_fact_statuses": [insight["policy_fact_status"] for insight in fallback],
                })
                return ToolChunk(
                    content=[TextBlock(type="text", text=json.dumps({
                        "insights": fallback,
                        "retrieval_mode": "keyword_fallback",
                    }, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                )
            bus.publish(session_id, "tool.result", {"tool": "category_insight_tool", "error": str(err)})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] 品类知识库不可用：{err}")],
                state=ToolResultState.ERROR,
            )

        if not has_answerable_knowledge(results):
            reason = "当前知识库没有足够相关且可验证的资料，不能据此作确定性回答"
            bus.publish(
                session_id,
                "tool.result",
                {"tool": "category_insight_tool", "hit_count": 0, "abstained": True, "reason": reason},
            )
            return ToolChunk(
                content=[TextBlock(type="text", text=json.dumps({"insights": [], "unanswerable": True, "reason": reason}, ensure_ascii=False))],
                state=ToolResultState.SUCCESS,
            )

        insights = []
        for item in results:
            metadata = {
                key: item.chunk.metadata[key]
                for key in ("source_reference", "source_type", "published_at", "effective_from", "effective_to", "region", "version", "topic")
                if item.chunk.metadata and key in item.chunk.metadata
            }
            insights.append({
                "content": _chunk_text(item.chunk.content),
                "source": item.chunk.metadata.get("source", item.document_id)
                if item.chunk.metadata
                else item.document_id,
                "score": round(item.score, 4),
                "metadata": metadata,
                "policy_fact_status": policy_fact_status(metadata),
            })
        bus.publish(
            session_id,
            "tool.result",
            {
                "tool": "category_insight_tool", "hit_count": len(insights), "abstained": False,
                "policy_fact_statuses": [insight["policy_fact_status"] for insight in insights],
            },
        )
        return ToolChunk(
            content=[TextBlock(type="text", text=json.dumps({"insights": insights}, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    return category_insight_tool
