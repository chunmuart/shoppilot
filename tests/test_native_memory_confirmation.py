"""使用真实 AgentScope Agent、权限引擎和状态序列化验证挂起/恢复。"""
import pytest
from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock,ToolCallBlock,UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool,Toolkit,ToolChunk
from agentscope.state import AgentState
from app.infrastructure.llm import ThrottledChatModel
from app.infrastructure.throttle import GatewayThrottle
from app.application.agents.tool_confirmation import MemoryPermissionMiddleware,awaiting_event,confirmation_inputs

class Model(ThrottledChatModel):
    def __init__(self):
        super().__init__(credential=OpenAICredential(api_key='test',base_url='http://test/v1'),model='test',throttle=GatewayThrottle(3,0),max_transient_retries=0)
        self.calls=0
    async def _invoke_upstream(self,*args,**kwargs):
        self.calls+=1
        content=[ToolCallBlock(id='call-a',name='remember_preference_tool',input='{"statement":"喜欢裙子"}')] if self.calls==1 else [TextBlock(text='操作已处理')]
        return ChatResponse(content=content,is_last=True)

@pytest.mark.parametrize('approved',[True,False])
async def test_native_pause_persist_resume_and_no_replay(approved):
    writes=[]
    async def remember_preference_tool(statement:str)->ToolChunk:
        """保存测试偏好。

        Args:
            statement (`str`): 偏好。
        """
        writes.append(statement)
        return ToolChunk(content=[TextBlock(text='已保存')])
    model=Model(); toolkit=Toolkit(tools=[FunctionTool(remember_preference_tool)])
    agent=Agent('test','测试',model,toolkit=toolkit,middlewares=[MemoryPermissionMiddleware()])
    try:
        events=[e async for e in agent.reply_stream(UserMsg('buyer','记住喜欢裙子'))]
        pending=awaiting_event(agent)
        assert pending and not writes
        restored=Agent('test','测试',model,toolkit=toolkit,middlewares=[MemoryPermissionMiddleware()],state=AgentState.model_validate_json(agent.state.model_dump_json()))
        responses=({'interrupt_id':f'{pending.reply_id}:call-a','approved':approved},)
        incoming=confirmation_inputs(restored,responses)
        assert incoming.confirm_results[0].rules is None
        assert incoming.confirm_results[0].tool_call.input=='{"statement":"喜欢裙子"}'
        with pytest.raises(ValueError):confirmation_inputs(restored,({'interrupt_id':'foreign:call-a','approved':True},))
        events=[e async for e in restored.reply_stream(incoming)]
        assert writes==(['喜欢裙子'] if approved else [])
        assert awaiting_event(restored) is None
        with pytest.raises(ValueError):confirmation_inputs(restored,responses)
    finally:await model.client.close()

async def test_agui_interrupt_and_journal_decision_identity(tmp_path):
    from app.application.agents.ag_ui_adapter import AGUIRunAdapter
    from app.infrastructure.ag_ui_journal import AGUIJournal,JournalConflict
    from agentscope.event import RequireUserConfirmEvent
    from ag_ui.core import RunAgentInput
    from tests.test_ag_ui_journal import body
    request=body();events=[]
    adapter=AGUIRunAdapter(RunAgentInput.model_validate(request),events.append)
    adapter.start()
    adapter.on_agent_event(RequireUserConfirmEvent(reply_id='reply',tool_calls=[ToolCallBlock(id='call',name='remember_preference_tool',input='{"statement":"裙子"}')]))
    adapter.finish('')
    end=events[-1].model_dump(mode='json',by_alias=True)
    assert end['outcome']['type']=='interrupt'
    journal=AGUIJournal(tmp_path/'journal.db');await journal.initialize()
    await journal.reserve(request,'b1','owner')
    await journal.append('r1','owner',[e.model_dump(mode='json',by_alias=True,exclude_none=True) for e in events])
    restored=AGUIJournal(tmp_path/'journal.db');await restored.initialize()
    assert (await restored.run('r1','b1'))['state']['toolApprovals'][0]['id']=='reply:call'
    resume=body('r2');resume['resume']=[{'interruptId':'reply:call','status':'resolved','payload':{'approved':True}}]
    await restored.reserve(resume,'b1','owner2')
    changed={**resume,'resume':[{'interruptId':'reply:call','status':'resolved','payload':{'approved':False}}]}
    with pytest.raises(JournalConflict):await restored.reserve(changed,'b1','owner3')
