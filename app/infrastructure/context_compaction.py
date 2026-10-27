"""两阶段压缩：先归档旧工具正文，再由 SDK 判断是否需要模型摘要。"""
from __future__ import annotations
import hashlib
import json
import time
from agentscope.message import TextBlock, ToolResultBlock
from agentscope.middleware import MiddlewareBase
from app.infrastructure.budget import get_budget
from app.infrastructure.context import ShoppingContext
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore


class EvidenceCompactionMiddleware(MiddlewareBase):
    def __init__(self, store: ContextEvidenceStore):
        self.store = store

    async def on_compress_context(self, agent, input_kwargs, next_handler):
        budget = get_budget()
        if budget is not None and budget.exhausted:
            # 下一次 model.__call__ 将直接返回规则回复，摘要也不能新增模型调用。
            return
        ctx = ShoppingContext.current()
        if ctx is None:
            return await next_handler()
        cfg = input_kwargs.get("context_config") or agent.context_config
        prepared = await agent._prepare_model_input()
        before = await agent.model.count_tokens(**prepared)
        if before < cfg.trigger_ratio * agent.model.context_size:
            return
        started = time.monotonic()
        archived = 0
        for message in agent.state.context[:-6]:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if not isinstance(block, ToolResultBlock) or block.metadata.get("context_archive"):
                    continue
                text = block.output if isinstance(block.output, str) else "\n".join(b.text for b in block.output if isinstance(b, TextBlock))
                if len(text) < 1000:
                    continue
                ref = await self.store.save(ctx.buyer_id, ctx.shopping_session_id, "tool_archive", {"tool": block.name, "text": text})
                block.output = [TextBlock(text=json.dumps({"tool": block.name, "result_ref": ref, "historical": True,
                    "notice": "旧工具正文已归档，请用 conversation_fact_lookup 按引用定点回查；当前交易状态以本轮账本为准。"}, ensure_ascii=False))]
                block.metadata["context_archive"] = ref
                archived += 1
        after = await agent.model.count_tokens(**(await agent._prepare_model_input()))
        previous_summary = agent.state.summary
        from app.infrastructure.context_usage import context_call_kind
        kind_token = context_call_kind.set("summary")
        try:
            await next_handler()  # SDK 重新计数；清理已足够时不会再调用摘要模型。
        finally:
            context_call_kind.reset(kind_token)
        await self.store.save(ctx.buyer_id, ctx.shopping_session_id, "compression", {
            "before_tokens": before, "after_tool_cleanup_tokens": after, "archived_results": archived,
            "summary_changed": agent.state.summary != previous_summary,
            "summary_revision": hashlib.sha256((agent.state.summary or "").encode()).hexdigest(),
            "elapsed_ms": round((time.monotonic()-started)*1000),
        })
