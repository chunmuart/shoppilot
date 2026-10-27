"""长期记忆修改：按原文原子替换，身份只取当前上下文。"""
from typing import Literal
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk
from app.domain.buyer.preference import BuyerPreference
from app.infrastructure.context import ShoppingContext


def build_update_preference_tool(store, bus):
    async def update_preference_tool(previous_statement: str, kind: Literal["like","dislike"], statement: str, memory_id: str = "", expected_version: int = 0) -> ToolChunk:
        """买家明确要求长期修改偏好时，原子替换旧偏好。本轮临时例外不要保存。

        Args:
            memory_id (`str`): 当前记忆的稳定 ID，必须从最新偏好提示中读取。
            expected_version (`int`): 当前记忆版本，禁止猜测。
            previous_statement (`str`): 当前 buyer-preferences 中旧偏好的完整原文。
            kind (`str`): 新偏好类别，like 为喜欢，dislike 为避免。
            statement (`str`): 新偏好原文，最多 500 字符。
        """
        snapshot=ShoppingContext.current()
        if snapshot is None:
            return ToolChunk(content=[TextBlock(type="text",text="[error] 缺少可信买家上下文")],state=ToolResultState.ERROR)
        session=snapshot.shopping_session_id
        bus.publish(session,"tool.invoke",{"tool":"update_preference_tool","args":{"previous_statement":previous_statement,"kind":kind,"statement":statement}})
        try:
            preference=BuyerPreference(snapshot.buyer_id,kind,statement,source_kind="agent",source_ref=session)
            if getattr(store,"semantic_memory",False):
                if not memory_id or expected_version<1:raise ValueError("请从最新记忆列表读取 ID 和版本再操作")
                updated=await store.replace_by_id(snapshot.buyer_id,memory_id,expected_version,preference)
            else:updated=await store.replace(snapshot.buyer_id,previous_statement,preference)
            if not updated:
                remaining=await store.list_by_buyer(snapshot.buyer_id)
                text="原偏好未找到，没有修改。请按当前原文重试："+ "\n".join(p.statement for p in remaining)
            else:
                current=await store.list_by_buyer(snapshot.buyer_id)
                text="已更新长期偏好，当前保存的规范化记忆："+ "；".join(p.statement for p in current)
            bus.publish(session,"tool.result",{"tool":"update_preference_tool","updated":updated})
            return ToolChunk(content=[TextBlock(type="text",text=text)],state=ToolResultState.SUCCESS)
        except Exception as error:
            bus.publish(session,"tool.result",{"tool":"update_preference_tool","error":"偏好修改失败"})
            return ToolChunk(content=[TextBlock(type="text",text=f"[error] 偏好修改未保存：{error}")],state=ToolResultState.ERROR)
    if getattr(store,"semantic_memory",False):
        implementation=update_preference_tool
        async def update_preference_tool(previous_statement: str, kind: Literal["like","dislike"], statement: str, memory_id: str, expected_version: int) -> ToolChunk:
            return await implementation(previous_statement,kind,statement,memory_id,expected_version)
        update_preference_tool.__doc__=implementation.__doc__
    return update_preference_tool
