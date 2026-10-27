"""走 SDK 原生结构化生成，验证底层请求计量、校验失败和取消。"""
import asyncio
import pytest
from pydantic import BaseModel
from agentscope.credential import OpenAICredential
from agentscope.exception import StructuredOutputError
from agentscope.message import UserMsg, ToolCallBlock, TextBlock
from agentscope.model import ChatResponse, ChatUsage, OpenAIChatModel
from app.infrastructure.llm import ThrottledChatModel
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.context_usage import context_usage_sink, context_call_kind

class Summary(BaseModel):
    goal: str

@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'invalid', 'error', 'cancel'])
@pytest.mark.parametrize('kind', ['summary', 'business'])
async def test_sdk_structured_request_usage(monkeypatch, stream, outcome, kind):
    throttle = GatewayThrottle(1, 0)
    model = ThrottledChatModel(credential=OpenAICredential(api_key='test'), model='test',
        stream=stream, throttle=throttle, max_retries=0, max_transient_retries=0)
    samples = []
    async def raw(self, model_name, messages, tools, tool_choice, **kwargs):
        assert throttle._semaphore.locked()
        assert tools[0]['function']['name'] == 'generate_structured_output'
        assert self.stream == (False if kind == 'summary' else stream)
        if outcome == 'error': raise RuntimeError('connection failed')
        if outcome == 'cancel': raise asyncio.CancelledError()
        content = [ToolCallBlock(id='summary', name='generate_structured_output', input='{"goal":"背包"}')] if outcome == 'success' else [TextBlock(text='invalid')]
        response = ChatResponse(content=content, is_last=True, usage=ChatUsage(input_tokens=123, output_tokens=17, time=0.1))
        if not self.stream:return response
        async def chunks():
            yield response
        return chunks()
    monkeypatch.setattr(OpenAIChatModel, '_call_api', raw)
    t=context_usage_sink.set(samples.append);k=context_call_kind.set(kind)
    try:
        if outcome == 'success':
            res=await model.generate_structured_output([UserMsg(name='user',content='整理')],Summary)
            assert res.content == {'goal':'背包'}
        else:
            with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else StructuredOutputError if outcome == 'invalid' else RuntimeError):
                await asyncio.wait_for(model.generate_structured_output([UserMsg(name='user',content='整理')],Summary), timeout=2)
        # 当前 SDK 对格式错误依次尝试 forced / auto / none，每个真实调用单独计费。
        assert len(samples)==(3 if outcome == 'invalid' else 1)
        assert samples[0]['kind']==kind
        assert model.stream == stream
        assert samples[0]['input_tokens']==(123 if outcome in ('success','invalid') else None)
        assert samples[0]['output_tokens']==(17 if outcome in ('success','invalid') else None)
        assert not throttle._semaphore.locked()
    finally:
        context_usage_sink.reset(t);context_call_kind.reset(k)

@pytest.mark.asyncio
async def test_structured_budget_denial_never_calls_gateway(monkeypatch):
    from app.infrastructure.budget import TokenBudget, _budget_var
    model=ThrottledChatModel(credential=OpenAICredential(api_key='test'),model='test',stream=False,
        throttle=GatewayThrottle(1,0),max_retries=0)
    async def forbidden(*args, **kwargs):raise AssertionError('预算不足不能请求上游')
    monkeypatch.setattr(OpenAIChatModel,'_call_api',forbidden)
    budget=TokenBudget(10);token=_budget_var.set(budget)
    try:
        with pytest.raises(RuntimeError,match='预算不足'):
            await model.generate_structured_output([UserMsg(name='user',content='整理')],Summary)
        assert budget.used==budget.reserved==0
    finally:_budget_var.reset(token)

@pytest.mark.asyncio
async def test_structured_usage_settles_budget_and_schema_is_reserved(monkeypatch):
    from app.infrastructure.budget import TokenBudget, _budget_var
    model=ThrottledChatModel(credential=OpenAICredential(api_key='test'),model='test',stream=False,
        throttle=GatewayThrottle(1,0),max_retries=0)
    budget=TokenBudget(100000)
    async def raw(self, model_name,messages,tools,tool_choice,**kwargs):
        assert budget.reserved > 1024
        assert kwargs['max_completion_tokens']==1024
        return ChatResponse(content=[ToolCallBlock(id='s',name='generate_structured_output',input='{"goal":"旅行"}')],
            is_last=True,usage=ChatUsage(input_tokens=123,output_tokens=17,time=.1))
    monkeypatch.setattr(OpenAIChatModel,'_call_api',raw)
    token=_budget_var.set(budget)
    try:
        await model.generate_structured_output([UserMsg(name='user',content='整理')],Summary)
        assert budget.used==140 and budget.reserved==0
    finally:_budget_var.reset(token)

@pytest.mark.asyncio
async def test_native_compatibility_retry_records_both_requests(monkeypatch):
    import httpx,openai
    model=ThrottledChatModel(credential=OpenAICredential(api_key='test'),model='test',stream=False,
        throttle=GatewayThrottle(1,0),max_retries=0)
    calls=[];samples=[]
    async def raw(self,model_name,messages,tools,tool_choice,**kwargs):
        calls.append(tool_choice.mode)
        if len(calls)==1:
            raise openai.BadRequestError('tool_choice not supported',response=httpx.Response(400,request=httpx.Request('POST','https://example.invalid')),body=None)
        return ChatResponse(content=[ToolCallBlock(id='s',name='generate_structured_output',input='{"goal":"旅行"}')],
            is_last=True,usage=ChatUsage(input_tokens=123,output_tokens=17,time=.1))
    monkeypatch.setattr(OpenAIChatModel,'_call_api',raw)
    token=context_usage_sink.set(samples.append)
    try:
        await model.generate_structured_output([UserMsg(name='user',content='整理')],Summary)
        assert calls==['generate_structured_output','auto']
        assert [s['input_tokens'] for s in samples]==[None,123]
    finally:context_usage_sink.reset(token)

@pytest.mark.asyncio
async def test_cancel_during_structured_stream_closes_and_preserves_partial_usage(monkeypatch):
    throttle=GatewayThrottle(1,0)
    model=ThrottledChatModel(credential=OpenAICredential(api_key='test'),model='test',stream=True,
        throttle=throttle,max_retries=0)
    samples=[];closed=[]
    async def raw(*args,**kwargs):
        async def chunks():
            try:
                yield ChatResponse(content=[TextBlock(text='生成中')],is_last=False,
                    usage=ChatUsage(input_tokens=123,output_tokens=17,time=.1))
                raise asyncio.CancelledError()
            finally:closed.append(True)
        return chunks()
    monkeypatch.setattr(OpenAIChatModel,'_call_api',raw)
    token=context_usage_sink.set(samples.append)
    try:
        with pytest.raises(asyncio.CancelledError):
            await model.generate_structured_output([UserMsg(name='user',content='整理')],Summary)
        assert closed==[True] and not throttle._semaphore.locked()
        assert len(samples)==1 and samples[0]['input_tokens']==123
    finally:context_usage_sink.reset(token)

@pytest.mark.asyncio
@pytest.mark.parametrize('stream',[False,True])
async def test_success_without_provider_usage_remains_unknown(monkeypatch,stream):
    model=ThrottledChatModel(credential=OpenAICredential(api_key='test'),model='test',stream=stream,
        throttle=GatewayThrottle(1,0),max_retries=0)
    async def raw(*args,**kwargs):
        response=ChatResponse(content=[ToolCallBlock(id='s',name='generate_structured_output',input='{"goal":"旅行"}')],is_last=True,usage=None)
        if not stream:return response
        async def chunks():yield response
        return chunks()
    monkeypatch.setattr(OpenAIChatModel,'_call_api',raw)
    samples=[];token=context_usage_sink.set(samples.append)
    try:
        result=await model.generate_structured_output([UserMsg(name='user',content='整理')],Summary)
        assert result.content=={'goal':'旅行'}
        assert len(samples)==1 and samples[0]['input_tokens'] is None and samples[0]['output_tokens'] is None
    finally:
        context_usage_sink.reset(token);await model.client.close()
