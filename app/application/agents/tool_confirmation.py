"""AgentScope 原生审批适配；客户端只能批准/拒绝，不能修改工具或添加永久规则。"""
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import ToolCallState
from agentscope.event import RequireUserConfirmEvent, UserConfirmResultEvent, ConfirmResult

MEMORY_WRITE_TOOLS = frozenset({'remember_preference_tool','update_preference_tool','forget_preference_tool'})

class MemoryPermissionMiddleware(MiddlewareBase):
    def __init__(self,store=None):self.store=store
    async def on_check_permission(self,agent,input_kwargs,next_handler):
        call=input_kwargs['tool_call']
        if call.name in {'update_preference_tool','forget_preference_tool'} and getattr(self.store,'semantic_memory',False):
            from app.infrastructure.context import ShoppingContext
            from app.application.memory.preference_selector import render_preference_lines
            ctx=ShoppingContext.current()
            if ctx is None:return PermissionDecision(behavior=PermissionBehavior.DENY,message='缺少买家身份')
            facts=await self.store.list_by_buyer(ctx.buyer_id)
            values=input_kwargs['tool_input']
            expected_text=values.get('previous_statement') if call.name=='update_preference_tool' else values.get('statement')
            if not any(p.memory_id==values.get('memory_id') and p.version==values.get('expected_version') and p.statement==expected_text for p in facts):
                return PermissionDecision(behavior=PermissionBehavior.DENY,message='目标记忆或版本已变化，未执行；请按以下最新 ID、版本和原文重新申请：\n'+render_preference_lines(facts))
        if call.name in MEMORY_WRITE_TOOLS and call.state != ToolCallState.ALLOWED:
            return PermissionDecision(behavior=PermissionBehavior.ASK,
                message='需要买家确认本次长期记忆变更',bypass_immune=True)
        return await next_handler(**input_kwargs)


def awaiting_event(agent):
    getter=getattr(agent.state,'get_awaiting_tool_calls',None)
    if getter is None:return None
    calls=[c for c in getter(agent.name) if c.state==ToolCallState.ASKING]
    return RequireUserConfirmEvent(reply_id=agent.state.reply_id,tool_calls=calls) if calls else None


def confirmation_inputs(agent,responses):
    event=awaiting_event(agent)
    if event is None:raise ValueError('该确认已处理或失效，请刷新会话')
    calls={f'{event.reply_id}:{c.id}':c for c in event.tool_calls}
    if not responses or len({r['interrupt_id'] for r in responses})!=len(responses):raise ValueError('确认请求为空或重复')
    if any(r['interrupt_id'] not in calls for r in responses):raise ValueError('确认与当前会话待执行操作不一致')
    return UserConfirmResultEvent(reply_id=event.reply_id,confirm_results=[
        ConfirmResult(confirmed=r['approved'],tool_call=calls[r['interrupt_id']],rules=None) for r in responses])
