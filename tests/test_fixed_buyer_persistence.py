"""固定买家的收藏持久化、隔离与离线身份迁移。"""
import sqlite3,json
import httpx
from fastapi import FastAPI
from app.infrastructure.buyer_favorites import BuyerFavoriteStore
from app.presentation.favorites import register_favorite_routes
from scripts.migrate_local_buyer import migrate

async def test_favorite_api_persists_after_reopen_and_isolates(tmp_path):
    path=tmp_path/'favorites.db';api=FastAPI()
    register_favorite_routes(api,lambda:BuyerFavoriteStore(path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        url='/commerce/favorites/P1001?buyer_id=pao-coder'
        assert (await c.put(url,json={'product':{'product_id':'P1001','title':'背包'}})).status_code==200
        assert (await c.get('/commerce/favorites?buyer_id=pao-coder')).json()['products'][0]['title']=='背包'
        assert (await c.get('/commerce/favorites?buyer_id=other')).json()['products']==[]
        await c.delete('/commerce/favorites/P1001?buyer_id=other')
        assert len(await BuyerFavoriteStore(path).list('pao-coder'))==1
        await c.delete(url)
        assert await BuyerFavoriteStore(path).list('pao-coder')==[]

def test_migrate_only_selected_identity_keeps_history_and_backup(tmp_path):
    db=sqlite3.connect(tmp_path/'shoppilot.db')
    db.execute('CREATE TABLE conversation_sessions(session_id TEXT,buyer_id TEXT)')
    db.executemany('INSERT INTO conversation_sessions VALUES (?,?)',[('s','old'),('test-s','test-buyer')])
    db.execute('CREATE TABLE agent_session_states(session_id TEXT,state_json TEXT)')
    db.execute('INSERT INTO agent_session_states VALUES (?,?)',('s',json.dumps({'name':'old','content':'旧账号 old 的文字不改'})))
    db.commit();db.close()
    report=migrate(tmp_path,'old','pao-coder')
    with sqlite3.connect(tmp_path/'shoppilot.db') as db:
        assert db.execute('SELECT buyer_id FROM conversation_sessions WHERE session_id="s"').fetchone()[0]=='pao-coder'
        assert db.execute('SELECT buyer_id FROM conversation_sessions WHERE session_id="test-s"').fetchone()[0]=='test-buyer'
        state=json.loads(db.execute('SELECT state_json FROM agent_session_states').fetchone()[0])
        assert state=={'name':'pao-coder','content':'旧账号 old 的文字不改'}
    assert (tmp_path/'backups').exists() and report['tables']['shoppilot.db/conversation_sessions']==1
