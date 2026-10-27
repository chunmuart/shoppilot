"""真实 SQLite 验证规范化、持久向量、隔离、更新删除与失败原子性。"""
import json
import asyncio
from types import SimpleNamespace
import pytest
from agentscope.message import TextBlock, UserMsg
from app.domain.buyer.preference import BuyerPreference
from app.infrastructure.semantic_memory import SemanticPreferenceStore,PreferenceDistiller,MemoryUnavailable
from app.application.memory.middleware import BuyerMemoryMiddleware
from app.infrastructure.context import ShoppingContext

class Legacy:
    def __init__(self):self.items=[]
    async def list_by_buyer(self,buyer):return [p for p in self.items if p.buyer_id==buyer]
class Extract:
    async def extract(self,p,existing=()):
        if '临时' in p.statement:return []
        text=p.statement.replace('你好啊，我一直','').replace('！谢谢','')
        return [{'kind':p.kind,'statement':text}]
class Embed:
    fail=False
    async def embed(self,q):
        if self.fail:raise RuntimeError()
        return [1.,0.] if any(x in q for x in ('裙','衣','风')) else [0.,1.]
    async def embed_batch(self,texts):return [await self.embed(t) for t in texts]

def make(tmp_path,legacy=None,embed=None):return SemanticPreferenceStore(tmp_path/'memory.db',legacy or Legacy(),Extract(),embed or Embed(),'test',.5)

@pytest.mark.asyncio
async def test_clean_atomic_vector_persistence_and_semantic_search(tmp_path):
    store=make(tmp_path)
    saved=await store.append(BuyerPreference('a','like','你好啊，我一直喜欢小香风连衣裙！谢谢'))
    assert saved[0].statement=='喜欢小香风连衣裙'
    await store.append(BuyerPreference('a','like','喜欢咖啡'))
    await store.append(BuyerPreference('a','dislike','不要塑料'))
    assert len(await make(tmp_path).list_by_buyer('a'))==3
    found=await store.select(await store.list_by_buyer('a'),'帮我选一身衣服',1)
    assert [p.statement for p in found]==['不要塑料','喜欢小香风连衣裙']
    with store._db() as db:
        assert '你好啊' not in str([tuple(r) for r in db.execute('SELECT * FROM memory_facts')])
    assert await store.list_by_buyer('b')==[]

@pytest.mark.asyncio
async def test_reject_noise_and_embedding_error_without_raw_fallback(tmp_path):
    embed=Embed();store=make(tmp_path,embed=embed)
    with pytest.raises(ValueError):await store.append(BuyerPreference('a','like','临时需要预算300'))
    embed.fail=True
    with pytest.raises(MemoryUnavailable):await store.append(BuyerPreference('a','like','喜欢裙子'))
    assert await store.list_by_buyer('a')==[]

@pytest.mark.asyncio
async def test_replace_delete_no_legacy_or_stale_vector_resurrection(tmp_path):
    old=Legacy();old.items=[BuyerPreference('a','like','喜欢裙子')]
    store=make(tmp_path,old)
    cached=await store.list_by_buyer('a')
    assert not await store.replace('b','喜欢裙子',BuyerPreference('b','like','喜欢咖啡'))
    assert await store.replace('a','喜欢裙子',BuyerPreference('a','like','喜欢咖啡'))
    assert await store.select(cached,'裙子',5)==[]
    assert await store.delete('a','喜欢咖啡')
    assert await make(tmp_path,old).list_by_buyer('a')==[]

@pytest.mark.asyncio
async def test_failed_replace_keeps_original_and_duplicates_idempotent(tmp_path):
    embed=Embed();store=make(tmp_path,embed=embed)
    await store.append(BuyerPreference('a','like','喜欢裙子'))
    await store.append(BuyerPreference('a','like','喜欢裙子'))
    assert len(await store.list_by_buyer('a'))==1
    embed.fail=True
    with pytest.raises(MemoryUnavailable):await store.replace('a','喜欢裙子',BuyerPreference('a','like','喜欢咖啡'))
    assert (await store.list_by_buyer('a'))[0].statement=='喜欢裙子'

@pytest.mark.asyncio
async def test_distiller_requires_source_evidence_and_does_not_store_instruction(tmp_path):
    async def model(**kwargs):return SimpleNamespace(content=[TextBlock(text=json.dumps({'facts':[{'kind':'like','statement':'喜欢贵重物品','evidence':'不存在的证据','durable':True,'confidence':.99}]}))])
    with pytest.raises(MemoryUnavailable):await PreferenceDistiller(model).extract(BuyerPreference('a','like','你好'))

@pytest.mark.asyncio
async def test_agentscope_middleware_injects_query_scoped_memory(tmp_path):
    store=make(tmp_path);await store.append(BuyerPreference('a','like','喜欢裙子'))
    from agentscope.middleware import MiddlewareBase
    mw=BuyerMemoryMiddleware(store)
    assert isinstance(mw,MiddlewareBase)
    from app.infrastructure.context import ShoppingContextSnapshot
    token=ShoppingContext.set(ShoppingContextSnapshot('s','a','zh-CN','CNY'))
    try:
        async def handler(**kwargs):
            assert '喜欢裙子' in kwargs['inputs'][0].get_text_content()
            yield 'ok'
        events=[e async for e in mw.on_reply(None,{'inputs':[UserMsg('a','选衣服')]},handler)]
        assert events==['ok']

    finally: ShoppingContext.reset(token)


async def test_model_change_reindexes_facts_without_changing_identity_or_audit(tmp_path):
    original=make(tmp_path)
    await original.append(BuyerPreference('a','like','喜欢裙子',source_kind='user',source_ref='form'))
    await original.append(BuyerPreference('b','like','喜欢裙子'))
    old=(await original.list_by_buyer('a'))[0]
    audit=await original.audit('a',old.memory_id)
    changed=SemanticPreferenceStore(original.path,Legacy(),Extract(),Embed(),'next-model',.5)
    await changed.append(BuyerPreference('a','like','喜欢风衣'))
    found=await changed.select(await changed.list_by_buyer('a'),'裙子风衣',5)
    assert {p.statement for p in found}=={'喜欢裙子','喜欢风衣'}
    assert next(p for p in found if p.memory_id==old.memory_id)==old
    assert await changed.audit('a',old.memory_id)==audit
    restarted=SemanticPreferenceStore(original.path,Legacy(),Extract(),Embed(),'next-model',.5)
    assert {p.statement for p in await restarted.select(await restarted.list_by_buyer('a'),'裙子风衣',5)}=={'喜欢裙子','喜欢风衣'}
    with changed._db() as db:
        assert {r['model_id'] for r in db.execute("SELECT model_id FROM memory_facts WHERE buyer_id='a'")}=={'next-model'}
        assert db.execute("SELECT model_id FROM memory_facts WHERE buyer_id='b'").fetchone()[0]=='test'


@pytest.mark.parametrize('failure',['unavailable','partial','nan','zero','wrong_dimension'])
async def test_failed_reindex_retains_old_vectors_and_recalls_valid_new_facts(tmp_path,failure):
    original=make(tmp_path)
    await original.append(BuyerPreference('a','like','喜欢裙子'))
    changed=SemanticPreferenceStore(original.path,Legacy(),Extract(),Embed(),'next-model',.5)
    await changed.append(BuyerPreference('a','like','喜欢风衣'))
    await changed.append(BuyerPreference('a','dislike','不要塑料'))
    before=await changed._rows('a')
    async def broken_batch(texts):
        if failure=='unavailable':raise RuntimeError('测试服务不可用')
        return {'partial':[],'nan':[[float('nan'),1]],'zero':[[0,0]],'wrong_dimension':[[1,0,0]]}[failure]
    changed.embedder.embed_batch=broken_batch
    found=await changed.select(await changed.list_by_buyer('a'),'裙子风衣',5)
    assert {p.statement for p in found}=={'喜欢风衣','不要塑料'}
    assert await changed._rows('a')==before


async def test_reindex_does_not_resurrect_deleted_or_overwrite_updated_memory(tmp_path):
    original=make(tmp_path)
    await original.append(BuyerPreference('a','like','喜欢裙子'))
    await original.append(BuyerPreference('a','like','喜欢风衣'))
    old=await original.list_by_buyer('a')
    started=asyncio.Event();release=asyncio.Event()
    class PausedEmbed(Embed):
        async def embed_batch(self,texts):
            started.set()
            await release.wait()
            return await super().embed_batch(texts)
    changed=SemanticPreferenceStore(original.path,Legacy(),Extract(),PausedEmbed(),'next-model',.5)
    task=asyncio.create_task(changed.select(old,'裙子风衣',5))
    try:
        await asyncio.wait_for(started.wait(),1)
        await original.delete_by_id('a',old[0].memory_id,old[0].version)
        await original.replace_by_id('a',old[1].memory_id,old[1].version,BuyerPreference('a','like','喜欢咖啡'))
    finally:
        release.set()
        found=await task
    assert found==[]
    remaining=await original.list_by_buyer('a')
    assert len(remaining)==1 and remaining[0].statement=='喜欢咖啡' and remaining[0].version==2
    with original._db() as db:
        assert db.execute("SELECT model_id FROM memory_facts").fetchone()[0]=='test'


async def test_same_model_dimension_change_rebuilds_incompatible_vectors(tmp_path):
    original=make(tmp_path)
    await original.append(BuyerPreference('a','like','喜欢裙子'))
    class Embed3(Embed):
        async def embed(self,q):return [1.,0.,0.]
    changed=make(tmp_path,embed=Embed3())
    assert [p.statement for p in await changed.select(await changed.list_by_buyer('a'),'裙子',5)]==['喜欢裙子']
    with changed._db() as db:
        assert len(json.loads(db.execute("SELECT vector FROM memory_facts").fetchone()[0]))==3


async def test_reindex_write_error_rolls_back_whole_batch(tmp_path):
    original=make(tmp_path)
    await original.append(BuyerPreference('a','like','喜欢裙子'))
    await original.append(BuyerPreference('a','like','喜欢风衣'))
    changed=SemanticPreferenceStore(original.path,Legacy(),Extract(),Embed(),'next-model',.5)
    await changed.append(BuyerPreference('a','like','喜欢衬衣'))
    before=await changed._rows('a')
    with changed._db() as db:
        db.execute("""CREATE TRIGGER fail_index_update BEFORE UPDATE OF vector ON memory_facts
            WHEN OLD.statement='喜欢风衣' BEGIN SELECT RAISE(ABORT,'模拟写库失败'); END""")
    found=await changed.select(await changed.list_by_buyer('a'),'裙子风衣',5)
    assert [p.statement for p in found]==['喜欢衬衣']
    assert await changed._rows('a')==before
