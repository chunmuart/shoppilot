"""Agent 生成澄清问题，服务端校验并持久化，前端只按协议渲染。"""

import json
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk
from app.infrastructure.context import ShoppingContext


def build_shopping_form_tool(store, bus):
    async def show_shopping_form(
        title: str, questions: list[dict], context: str = "", description: str = ""
    ) -> ToolChunk:
        """澄清选购需求：由 Agent 编写本次问题，展示后结束本轮等待买家提交。

        买家要求用表单补充条件时也调用本工具。根据上下文决定题目、顺序、控件类型、
        选项、说明及是否必填；没有固定业务题库，不重复询问已经明确的内容。
        只询问影响本次选择的未知条件，不替买家预选答案。不确定时允许留空或提供
        明确的“不确定”选项。数字预算应在题目或 unit 中明确币种、数量及费用范围。
        页面只渲染已注册组件，不接受 HTML/JavaScript。提交结果通过同一会话的
        下一条买家消息继续选购；本工具不代表买家批准订单、记忆或航司规则。

        Args:
            title: 简短的中文表单标题。
            questions: 按展示顺序提供 id、type、label；type 为 text、number、single_select 或 multi_select。选择题提供 options（value、label），可设置 required、help_text、placeholder；数字题可设置 unit、minimum、maximum。
            context: 已知的选购背景，仅用于指代，不作为买家新的答案。
            description: 向买家说明本次为什么需要补充这些信息。
        """
        ctx = ShoppingContext.current()
        if ctx is None:
            return ToolChunk(
                content=[TextBlock(text="缺少买家会话")], state=ToolResultState.ERROR
            )
        try:
            form = await store.create_clarification(
                ctx.buyer_id, ctx.shopping_session_id, title, questions, context, description
            )
        except ValueError as error:
            return ToolChunk(
                content=[TextBlock(text=str(error))], state=ToolResultState.ERROR
            )
        bus.publish(
            ctx.shopping_session_id,
            "ui.surface",
            {"buyer_id": ctx.buyer_id, "form": form},
        )
        return ToolChunk(
            content=[
                TextBlock(
                    text=json.dumps(
                        {
                            "form_id": form["form_id"],
                            "status": "awaiting_input",
                            "notice": "澄清表单已显示；请结束本轮等待买家提交。不得按默认值继续检索或当作已确认约束。",
                        },
                        ensure_ascii=False,
                    )
                )
            ],
            state=ToolResultState.SUCCESS,
        )

    return show_shopping_form
