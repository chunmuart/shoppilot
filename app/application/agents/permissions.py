# -*- coding: utf-8 -*-
"""permissions

订单工具只准备持久确认资源，实际交易由买家点击业务确认卡后提交。
记忆写工具由 MemoryPermissionMiddleware 强制进入 AgentScope 原生 ASK，
恢复旧会话的 allow 规则也不能跳过该审批。计划工具与调度工具继续显式放行。

不用 BYPASS/DONT_ASK 全局模式：保持权限引擎生效，只精准放行已知工具，
未来接入 Bash/文件类危险工具时仍受默认策略保护。
"""
from __future__ import annotations

from agentscope.agent import Agent
from agentscope.permission import PermissionBehavior, PermissionRule

# 对话层已有确认卡语义的业务写工具 + 内置计划工具 + 调度/记忆工具
_AUTO_ALLOWED_TOOLS = (
    "show_shopping_form",
    "create_order_tool",
    "cancel_order_tool",
    "task_dispatch",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "TaskGet",
)


def allow_business_tools(agent: Agent) -> Agent:
    """给 Agent 的权限上下文追加业务工具 allow 规则（幂等，兼容恢复的持久化状态）。"""
    allow_rules = agent.state.permission_context.allow_rules
    for tool_name in _AUTO_ALLOWED_TOOLS:
        rules = allow_rules.setdefault(tool_name, [])
        if any(rule.source == "projectSettings" for rule in rules):
            continue
        rules.append(
            PermissionRule(
                tool_name=tool_name,
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source="projectSettings",
            ),
        )
    return agent
