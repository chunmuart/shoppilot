"""Skill 目录按变化追加；旧清理逻辑仅供 legacy 对照与回滚。"""
import asyncio
import hashlib
import json
from copy import deepcopy

from agentscope.message import Msg, TextBlock, ToolResultBlock
from agentscope.middleware import MiddlewareBase
from app.infrastructure.context import ShoppingContext
from app.infrastructure.capability_registry import SKILL_TOOL_ALLOWLIST


CATALOG_KEY = "skill_catalog"
CATALOG_FIELDS = ("id", "version", "title", "description", "scope", "content_hash", "expires_at")


def catalog_snapshot(public, personal):
    """新快照稳定排序；不修改存储返回值或任何已发送消息。"""
    items = [{**{k: item.get(k) for k in CATALOG_FIELDS}, "source": source}
             for source, values in (("public", public), ("buyer", personal)) for item in values]
    items.sort(key=lambda item: (item["source"], item["id"]))
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    # 版本从同一份权威元数据直接呈现，避免长历史中的旧回答掩盖本次更新。
    versions = "\n".join(
        f"- {json.dumps(item['title'], ensure_ascii=False)}：当前有效版本 "
        f"{json.dumps(item['version'], ensure_ascii=False)}，skill_id={json.dumps(item['id'])}"
        for item in items
    )
    text = ("<skill-catalog>以下为服务端当前有效目录的完整快照，属于参考资料，不是新增系统指令。"
            "本快照取代此前目录的有效性，但不改写历史。空列表表示没有可用 Skill；"
            "本快照持续有效，直到后续新目录快照取代；后续消息未附目录不表示恢复旧条目。"
            "不代表激活方案或授权任何操作。\n"
            + ("当前没有任何可用 Skill，先前目录中的全部方案均不可用、没有当前版本。\n" if not items else "")
            + json.dumps({"revision": digest, "skills": items}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
            + "\n当前可用方案及唯一有效版本（持续生效至后续新快照）：\n"
            + (versions or "无；全部旧方案不可用，也没有当前版本。") + "\n</skill-catalog>")
    return {"digest": digest, "text": text}


def receipt_visible(agent, receipt):
    """消息被摘要移出工作集后，下一条用户消息需要重新携带最新目录。"""
    return bool(receipt and any(
        m.id == receipt.get("message_id")
        and any(isinstance(b, TextBlock) and hashlib.sha256(b.text.encode()).hexdigest()
                == receipt.get("block_hash") for b in m.content)
        for m in agent.state.context
    ))


class SkillCatalogMiddleware(MiddlewareBase):
    """在原生 on_reply 追加新输入；水位与消息同属 AgentState，复用会话 CAS/fence。"""
    def __init__(self, registry, personal_store, available_tools):
        self.registry = registry
        self.personal_store = personal_store
        self.available_tools = frozenset(available_tools) & SKILL_TOOL_ALLOWLIST

    async def on_reply(self, agent, input_kwargs, next_handler):
        ctx = ShoppingContext.current()
        incoming = input_kwargs.get("inputs")
        inputs = incoming if isinstance(incoming, list) else [incoming]
        buyer = next((m for m in reversed(inputs) if isinstance(m, Msg)
                      and ctx and m.role == "user" and m.name == ctx.buyer_id), None)
        # 原生审批恢复、工具续跑与无新用户输入不生成快照，也不清除本轮激活状态。
        if buyer is None or agent.state.has_awaiting_tool_calls(agent.name):
            async for event in next_handler(**input_kwargs):
                yield event
            return
        ShoppingContext.set_skill_catalog_mode("append_only")
        if not ctx.capability_digest and self.registry is not None:
            digest = await asyncio.to_thread(self.registry.bind_session, ctx.shopping_session_id,
                                            ctx.buyer_id, allow_skill_updates=True)
            ShoppingContext.set_capability_digest(digest)
        ctx = ShoppingContext.current()
        public = await asyncio.to_thread(self.registry.metadata, available_tools=self.available_tools,
                                         expected_digest=ctx.capability_digest) if self.registry else []
        personal = await asyncio.to_thread(self.personal_store.list, ctx.buyer_id) if self.personal_store else []
        snapshot = catalog_snapshot(public, personal)
        previous = deepcopy(agent.state.middle_context.get(CATALOG_KEY, {}))
        if previous.get("buyer_id", ctx.buyer_id) != ctx.buyer_id:
            raise ValueError("Skill 目录水位不属于当前买家")
        receipt = previous.get("receipt")
        changed = not receipt_visible(agent, receipt) or receipt.get("digest") != snapshot["digest"]
        if changed:
            copied = buyer.model_copy(deep=True)
            copied.content.append(TextBlock(text=snapshot["text"]))
            inputs = [copied if m is buyer else m for m in inputs]
            receipt = {"digest": snapshot["digest"], "message_id": buyer.id,
                       "block_hash": hashlib.sha256(snapshot["text"].encode()).hexdigest()}
        current = {"schema": 1, "mode": "append_only", "buyer_id": ctx.buyer_id,
                   "user_message_id": buyer.id, "receipt": receipt,
                   "active_reference_ids": [m.id for m in inputs if isinstance(m, Msg)
                                            and m.name == "selected_skill_reference"]}
        try:
            async for event in next_handler(**{**input_kwargs, "inputs": inputs}):
                # ReplyStart 在 SDK 接收消息之后发出；取消前后均按真实接收情况记水位。
                if any(m.id == buyer.id for m in agent.state.context):
                    agent.state.middle_context[CATALOG_KEY] = current
                yield event
        finally:
            if any(m.id == buyer.id for m in agent.state.context) and receipt_visible(agent, receipt):
                agent.state.middle_context[CATALOG_KEY] = current

    async def on_model_call(self, agent, input_kwargs, next_handler):
        """只复核本轮加载/选择，旧正文留作历史，不借历史授予本轮资格。"""
        ctx = ShoppingContext.current()
        state = agent.state.middle_context.get(CATALOG_KEY, {})
        if ctx and state.get("mode") == "append_only":
            if self.registry:
                bound = await asyncio.to_thread(self.registry.bind_session, ctx.shopping_session_id, ctx.buyer_id)
                if bound != ctx.capability_digest:
                    raise ValueError("本轮 Skill 资料版本变化，请重试")
            messages = agent.state.context
            boundary = next((i for i, m in enumerate(messages) if m.id == state.get("user_message_id")), len(messages))
            active = []
            for i, message in enumerate(messages):
                if message.id in state.get("active_reference_ids", []):
                    active.append(message.metadata.get("skill_activation", {}))
                if i >= boundary:
                    for block in message.content:
                        if isinstance(block, ToolResultBlock) and block.name == "load_agent_skill_tool":
                            text = block.output if isinstance(block.output, str) else "".join(
                                b.text for b in block.output if isinstance(b, TextBlock))
                            try:
                                doc = json.loads(text)
                                if isinstance(doc, dict) and doc.get("kind") == "skill":
                                    active.append({"id": doc["id"], "version": doc["version"],
                                                   "contentHash": doc["content_hash"]})
                            except (ValueError, KeyError):
                                pass  # 错误工具响应不是成功激活。
            for item in active:
                if not item:
                    raise ValueError("本轮 Skill 缺少可信版本信息")
                if item["id"].startswith("personal-"):
                    loaded = await asyncio.to_thread(self.personal_store.load, ctx.buyer_id, item["id"], item["version"])
                else:
                    loaded = await asyncio.to_thread(self.registry.load_skill, item["id"], item["version"],
                        available_tools=self.available_tools, expected_digest=ctx.capability_digest, require_current=True)
                if loaded["content_hash"] != item["contentHash"]:
                    raise ValueError("本轮 Skill 正文 hash 已失效")
        return await next_handler(**input_kwargs)


def clear_personal_skill_outputs(messages):
    for message in messages:
        for block in message.content:
            if getattr(block,"type",None) != "tool_result" or block.name != "load_agent_skill_tool":
                continue
            output=block.output
            text=output if isinstance(output,str) else "".join(getattr(part,"text","") for part in output)
            try:
                doc=json.loads(text)
            except (ValueError,TypeError):
                continue
            if isinstance(doc,dict) and doc.get("source")=="buyer":
                block.output="历史个人 Skill 正文已清理。需要使用时按本轮 personal_skill_catalog 的版本重新读取。"
