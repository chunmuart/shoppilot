"""稳定 ID/CAS、删除审计和并发冲突的真实 SQLite 回归。"""
import asyncio
import pytest
from app.domain.buyer.preference import BuyerPreference, MemoryConflict
from tests.test_semantic_memory import make

async def test_stable_id_versions_aba_and_delete_audit(tmp_path):
    store=make(tmp_path)
    await store.append(BuyerPreference('a','like','喜欢裙子',source_kind='agent',source_ref='session-a'))
    p=(await store.list_by_buyer('a'))[0]
    assert p.memory_id and p.version==1
    await store.replace_by_id('a',p.memory_id,1,BuyerPreference('a','like','喜欢咖啡'))
    q=(await store.list_by_buyer('a'))[0]
    assert q.memory_id==p.memory_id and q.version==2
    with pytest.raises(MemoryConflict):await store.delete_by_id('a',p.memory_id,1)
    with pytest.raises(MemoryConflict):await store.replace_by_id('b',p.memory_id,2,BuyerPreference('b','like','喜欢茶'))
    await store.delete_by_id('a',p.memory_id,2)
    await store.append(BuyerPreference('a','like','喜欢裙子'))
    assert (await store.list_by_buyer('a'))[0].memory_id!=p.memory_id
    assert [r['action'] for r in await store.audit('a',p.memory_id)]==['create','update','delete']
    assert await store.audit('b',p.memory_id)==[]
    with store._db() as db:
        assert '喜欢咖啡' not in str([tuple(r) for r in db.execute('SELECT * FROM memory_audit')])

async def test_concurrent_edit_only_one_version_wins(tmp_path):
    store=make(tmp_path);await store.append(BuyerPreference('a','like','喜欢裙子'))
    p=(await store.list_by_buyer('a'))[0]
    results=await asyncio.gather(*[store.replace_by_id('a',p.memory_id,p.version,BuyerPreference('a','like',text)) for text in ('喜欢咖啡','喜欢茶')],return_exceptions=True)
    assert sum(r is True for r in results)==1
    assert sum(isinstance(r,MemoryConflict) for r in results)==1
    assert (await make(tmp_path).list_by_buyer('a'))[0].version==2

async def test_conflicting_append_never_silently_overwrites(tmp_path):
    store=make(tmp_path);await store.append(BuyerPreference('a','like','喜欢裙子'))
    p=(await store.list_by_buyer('a'))[0]
    async def extract(*args,**kwargs):return [{'kind':'dislike','statement':'不要裙子','conflicts':[p.memory_id]}]
    store.distiller.extract=extract
    with pytest.raises(MemoryConflict):await store.append(BuyerPreference('a','dislike','不喜欢裙子了'))
    assert (await store.list_by_buyer('a'))[0]==p
    assert await store.replace_by_id('a',p.memory_id,1,BuyerPreference('a','dislike','不喜欢裙子了'))

def test_scoped_negative_is_not_expanded_to_global_material_filter():
    from app.application.memory.preference_selector import material_exclusion_tags
    assert material_exclusion_tags([BuyerPreference('a','dislike','不喜欢塑料食品盒')])==[]
    assert material_exclusion_tags([BuyerPreference('a','dislike','不要塑料材质')])==['合成聚合物']

async def test_stale_extraction_does_not_ignore_concurrent_new_memory(tmp_path):
    store=make(tmp_path)
    original=store._prepare
    prepared=asyncio.Event();release=asyncio.Event()
    async def prepare(p):
        facts=await original(p)
        if p.statement=='喜欢咖啡':prepared.set();await release.wait()
        return facts
    store._prepare=prepare
    task=asyncio.create_task(store.append(BuyerPreference('a','like','喜欢咖啡')))
    await prepared.wait()
    await store.append(BuyerPreference('a','like','喜欢茶'))
    release.set()
    with pytest.raises(MemoryConflict):await task

async def test_http_id_version_and_history_isolation(tmp_path):
    import httpx
    from tests.test_buyer_workspace import fixture,headers
    api,o,*_,policy=fixture(tmp_path)
    o._preference_store=make(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as client:
        path='/commerce/preferences?buyer_id=alice'
        created=await client.post(path,headers=headers(policy),json={'kind':'like','statement':'喜欢裙子'})
        p=created.json()['preferences'][0]
        changed={**{'kind':'like','statement':'喜欢咖啡'},'memory_id':p['memory_id'],'expected_version':p['version']}
        assert (await client.post(path,headers=headers(policy),json=changed)).status_code==200
        assert (await client.post(path,headers=headers(policy),json=changed)).status_code==409
        deleted=await client.request('DELETE',path,headers=headers(policy),json={'statement':p['statement'],'memory_id':p['memory_id'],'expected_version':1})
        assert deleted.status_code==409
        assert (await client.get(f"/commerce/preferences/{p['memory_id']}/history?buyer_id=bob",headers=headers(policy,'bob'))).json()=={'events':[]}

async def test_semantic_tools_require_ids_and_permission_checks_target_before_asking(tmp_path):
    from agentscope.tool import FunctionTool
    from agentscope.message import ToolCallBlock
    from agentscope.permission import PermissionBehavior
    from app.application.agents.tool_confirmation import MemoryPermissionMiddleware
    from app.application.tools.update_preference_tool import build_update_preference_tool
    from app.application.tools.forget_preference_tool import build_forget_preference_tool
    from app.infrastructure.eventbus import TradeEventBus
    from app.infrastructure.context import ShoppingContext,ShoppingContextSnapshot
    store=make(tmp_path);await store.append(BuyerPreference('a','like','喜欢裙子'))
    for builder in (build_update_preference_tool,build_forget_preference_tool):
        tool=FunctionTool(builder(store,TradeEventBus()))
        assert {'memory_id','expected_version'}<=set(tool.input_schema['required'])
    token=ShoppingContext.set(ShoppingContextSnapshot('s','a','zh-CN','CNY'))
    try:
        async def handler(**kwargs):raise AssertionError('无效目标不能进入执行')
        result=await MemoryPermissionMiddleware(store).on_check_permission(None,{'tool_call':ToolCallBlock(id='c',name='forget_preference_tool',input='{}'),'tool_input':{'memory_id':'foreign','expected_version':1,'statement':'喜欢裙子'}},handler)
        assert result.behavior==PermissionBehavior.DENY
        assert (await store.list_by_buyer('a'))[0].memory_id in result.message
    finally:ShoppingContext.reset(token)


def test_default_prompt_memory_signatures_match_required_tool_arguments(tmp_path):
    import re
    import yaml
    from pathlib import Path
    from agentscope.tool import FunctionTool
    from app.application.tools.update_preference_tool import build_update_preference_tool
    from app.application.tools.forget_preference_tool import build_forget_preference_tool
    from app.infrastructure.eventbus import TradeEventBus
    path=Path(__file__).resolve().parents[1]/"app/application/prompts/shoppilot.yml"
    prompt=yaml.safe_load(path.read_text())["main_agent"]["system_prompt"]
    for builder in (build_update_preference_tool,build_forget_preference_tool):
        tool=FunctionTool(builder(make(tmp_path),TradeEventBus()))
        match=re.search(rf"{tool.name}\(([^)]+)\)",prompt)
        assert match is not None
        documented={part.strip() for part in match[1].split(",")}
        assert set(tool.input_schema["required"])<=documented
