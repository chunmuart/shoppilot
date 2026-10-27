"""真实HTTP序列化边界：违约调用不交给Agent执行，消耗和响应模型仍完整计量。"""
import json
import httpx
import pytest
from agentscope.tool import ToolChoice
from agentscope.message import ToolCallBlock
from app.infrastructure.model_protocol import ModelProtocolViolation
from app.infrastructure.context_usage import context_usage_sink
from tests.test_prompt_cache import client_model, completion, messages

TOOLS=[{'type':'function','function':{'name':n,'description':'test','parameters':{'type':'object','properties':{}}}} for n in ('lookup','order')]

def called(*names):
    raw=completion()
    raw['model']='served-model'
    raw['choices'][0]['message']={'role':'assistant','content':None,'tool_calls':[
        {'id':str(i),'type':'function','function':{'name':n,'arguments':'{}'}} for i,n in enumerate(names)]}
    raw['choices'][0]['finish_reason']='tool_calls'
    return raw

@pytest.mark.parametrize('tools,choice,names,code',[
    (TOOLS,ToolChoice(mode='none'),['lookup'],'tools_forbidden'),
    ([],None,['lookup'],'tools_forbidden'),
    (TOOLS,None,['lookup','unknown'],'undeclared_tool'),
    (TOOLS,ToolChoice(mode='lookup'),['order'],'wrong_forced_tool'),
])
async def test_illegal_completions_rejected_and_usage_settled(tmp_path,tools,choice,names,code):
    model=await client_model(tmp_path,lambda r:httpx.Response(200,json=called(*names)))
    samples=[];token=context_usage_sink.set(samples.append)
    try:
        with pytest.raises(ModelProtocolViolation) as error:
            await model(messages(),tools=tools,tool_choice=choice)
        assert error.value.code==code
        assert len(samples)==1 and samples[0]['input_tokens']==1500
        assert samples[0]['prompt_cache']['response_model']=='served-model'
        assert samples[0]['prompt_cache']['response_model_matches'] is False
        assert samples[0]['prompt_cache']['protocol_status']==code
        assert not model._throttle._semaphore.locked()
    finally:
        context_usage_sink.reset(token);await model.client.close()

async def test_valid_parallel_tools_are_preserved(tmp_path):
    model=await client_model(tmp_path,lambda r:httpx.Response(200,json=called('lookup','order')))
    try:
        result=await model(messages(),tools=TOOLS)
        assert [b.name for b in result.content if isinstance(b,ToolCallBlock)]==['lookup','order']
    finally:await model.client.close()

@pytest.mark.parametrize('illegal', [False,True,'incomplete'])
async def test_streamed_tool_batch_is_atomic_and_usage_known(tmp_path,illegal):
    raw=called('lookup')
    chunks=[{'id':'s','object':'chat.completion.chunk','created':1,'model':'served-model','choices':[
        {'index':0,'delta':{'tool_calls':[{'index':0,'id':'a','type':'function','function':{'name':'look','arguments':'{'}}]},'finish_reason':None}]},
        {'id':'s','object':'chat.completion.chunk','created':1,'model':'served-model','choices':[
        {'index':0,'delta':{'tool_calls':[{'index':0,'function':{'name':'up','arguments':'}'}}]},'finish_reason':None}]}]
    if illegal is True:
        chunks.append({'id':'s','object':'chat.completion.chunk','created':1,'model':'served-model','choices':[
            {'index':0,'delta':{'tool_calls':[{'index':1,'id':'bad','type':'function','function':{'name':'unknown','arguments':'{}'}}]},'finish_reason':None}]})
    if illegal != 'incomplete':
        chunks.append({'id':'s','object':'chat.completion.chunk','created':1,'model':'served-model','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]})
    chunks.append({'id':'s','object':'chat.completion.chunk','created':1,'model':'served-model','choices':[],'usage':raw['usage']})
    response=lambda r:httpx.Response(200,headers={'content-type':'text/event-stream'},content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
    model=await client_model(tmp_path,response,stream=True)
    emitted=[];samples=[];token=context_usage_sink.set(samples.append)
    try:
        stream=await model(messages(),tools=TOOLS)
        async def consume():
            async for part in stream:emitted.extend(b for b in part.content if isinstance(b,ToolCallBlock))
        if illegal:
            with pytest.raises(ModelProtocolViolation):await consume()
            assert not emitted
        else:
            await consume();assert emitted[-1].name=='lookup'
        assert len(samples)==1 and samples[0]['input_tokens']==1500
        assert not model._throttle._semaphore.locked()
    finally:context_usage_sink.reset(token);await model.client.close()

async def test_truncated_nonstream_tool_is_not_executable(tmp_path):
    raw=called('order');raw['choices'][0]['finish_reason']='length'
    model=await client_model(tmp_path,lambda r:httpx.Response(200,json=raw))
    try:
        with pytest.raises(ModelProtocolViolation) as error:
            await model(messages(),tools=TOOLS)
        assert error.value.code=='incomplete_tool_response'
    finally:await model.client.close()


async def test_required_product_recheck_cannot_be_replaced_by_plain_answer(tmp_path):
    tools = [{'type': 'function', 'function': {'name': 'product_search_tool',
              'parameters': {'type': 'object', 'properties': {}}}}]
    model = await client_model(tmp_path, lambda r: httpx.Response(200, json=completion()), stream=True)
    try:
        with pytest.raises(ModelProtocolViolation) as error:
            await model(messages(), tools=tools, tool_choice=ToolChoice(mode='product_search_tool'))
        assert error.value.code == 'required_tool_missing'
        assert model.stream is True
    finally:
        await model.client.close()
