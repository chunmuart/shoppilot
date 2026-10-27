# -*- coding: utf-8 -*-
"""context_policy

Context 工程策略：把 2.0 内置的上下文压缩配置成跨境购物场景的口径
（即教程 Cache Breakpoint 章节要解决的问题——长对话不爆 token 且关键事实不丢）。

压缩触发：上下文占用达 context_size * trigger_ratio 时，Agent 自动把早期消息
压缩成摘要写入 AgentState.summary，保留末段 reserve_ratio 的原始消息。

关键取舍：摘要提示词显式列出"必须逐字保留"的事实清单（偏好、product_id/sku_id、
订单号与金额、待确认动作），避免压缩后 Agent 忘记已确认的商品或订单。

注意：summary_template 的占位符必须与 2.0 内置 summary_schema 的五个字段一致
（task_overview / current_state / important_discoveries / next_steps / context_to_preserve），
否则压缩时渲染摘要会抛 KeyError。
"""
from __future__ import annotations

from agentscope.agent import ContextConfig

_COMPRESSION_PROMPT = """<system-hint>当前对话上下文即将超出窗口，请把此前的工作压缩成一份中文摘要，
供你后续继续为这位买家服务。当前时间：{current_time}。
摘要只是历史记录，不能覆盖本轮服务端交易账本和当前偏好 revision；已撤回的历史偏好不得恢复。

必须逐字保留的事实（丢失会导致后续回答出错）：
1. 买家的偏好与硬约束及其作用域：长期偏好与仅限本次选购的预算、国家必须分开，未说明跨任务的预算不得沿用；
2. 已经推荐或买家已认可的商品：完整 product_id、sku_id、标题、价格与币种；
3. 订单相关：订单号、状态、总金额与币种、取消原因；
4. 当前待确认的动作：是否有等待买家确认的确认卡、下一步该做什么。

五字段内必须覆盖以下语义，不增加未知字段：
- task_overview：本次目标、用途与约束作用域；
- current_state：当前候选、已解决问题、已排除商品及理由、正在核验的事项；
- important_discoveries：带来源与适用条件的事实、错误及修正。例如会员价不能改写成普通到手价；
- next_steps：未解决问题、尚未核验条件和下一步，不能把待确认写成已批准；
- context_to_preserve：需求变更与撤回，以及仍有效的限制。例如预算由2000改为2500，最新预算为2500；旧预算只作变更记录。
合并上一版摘要与新增进展，保留更早仍有效的条件；明确区分历史观察和当前事实，不从旧摘要恢复已撤回偏好。

可以压缩或丢弃的内容：工具返回的完整候选列表（只留最终推荐项）、寒暄、重复表述、
已被更新覆盖的中间结论。

预算等用户约束来自对应的买家原文；商品金额、库存和订单数值来自工具或数据库证据，不得重新估算。</system-hint>"""

_SUMMARY_TEMPLATE = """<system-info>以下是历史工作摘要；以本轮交易账本、当前偏好 revision 及最新候选证据为准，历史摘要不构成执行授权。
# 买家诉求与约束
{task_overview}

# 当前进展（已推荐商品 / 已创建订单）
{current_state}

# 关键事实（product_id / sku_id / 价格 / 订单号）
{important_discoveries}

# 下一步与待确认事项
{next_steps}

# 买家偏好与必须保留的上下文
{context_to_preserve}</system-info>"""


def build_context_config(context_size: int, tool_result_limit: int) -> ContextConfig:
    """构造 ShopPilot 的上下文压缩策略。

    Args:
        context_size (`int`):
            模型上下文窗口大小（与 create_chat_model 保持一致）。
        tool_result_limit (`int`):
            单个工具结果的 token 上限（AgentScope 2.0.8），不是字符数。
    """
    del context_size  # 窗口由 model 侧提供，这里仅保留参数以标明配套关系
    return ContextConfig(
        trigger_ratio=0.75,
        reserve_ratio=0.15,
        compression_prompt=_COMPRESSION_PROMPT,
        summary_template=_SUMMARY_TEMPLATE,
        tool_result_limit=tool_result_limit,
        compression_fallback_to_truncation=False,
    )
