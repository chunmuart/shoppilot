"""本机停服后迁移一个明确的旧买家到固定 ID；先备份，遇到目标已有数据拒绝混合。"""
import argparse,sqlite3,json,hashlib
from pathlib import Path
from datetime import datetime,timezone

FILES=['shoppilot.db','ag_ui_runs.db','buyer_memory.db','buyer_skills.db','context_evidence.db','capabilities.db','prompts/registry.sqlite3','buyer_favorites.db']
def quoted(value):return '"'+value.replace('"','""')+'"'
def convert(value,source,target):
    if isinstance(value,dict):return {k:convert(v,source,target) for k,v in value.items()}
    if isinstance(value,list):return [convert(v,source,target) for v in value]
    return target if value==source else value

def migrate(data,source,target):
    if source==target:raise ValueError('源用户不能与目标相同')
    paths=[data/f for f in FILES if (data/f).exists()]
    backup=data/'backups'/('buyer-migration-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True)
    for path in paths:
        dest=backup/path.relative_to(data);dest.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path) as original,sqlite3.connect(dest) as saved:original.backup(saved)
    report={'source':source,'target':target,'backup':str(backup),'tables':{}}
    db=sqlite3.connect(':memory:')
    try:
        for i,p in enumerate(paths):db.execute(f'ATTACH DATABASE ? AS d{i}',(str(p),))
        db.execute('BEGIN IMMEDIATE')
        for i,p in enumerate(paths):
            schema=f'd{i}'
            for (table,) in db.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                full=schema+'.'+quoted(table)
                cols=[r[1] for r in db.execute(f'PRAGMA {schema}.table_info({quoted(table)})')]
                owner='buyer_id' if 'buyer_id' in cols else 'buyer' if table=='context_evidence' else 'owner_id' if table=='session_write_claims' else None
                if owner:
                    if db.execute(f'SELECT 1 FROM {full} WHERE {quoted(owner)}=? LIMIT 1',(target,)).fetchone():raise ValueError(f'{p.name}/{table}: 目标已有记录，未自动合并')
                    count=db.execute(f'UPDATE {full} SET {quoted(owner)}=? WHERE {quoted(owner)}=?',(target,source)).rowcount
                    if count:report['tables'][p.name+'/'+table]=count
                # 消息中的可信 name、嵌套买家字段需一起更新；不替换自然语言正文的子串。
                for col in cols:
                    if col not in ('state_json','input_json','projection_json','messages_json','payload','result'):continue
                    for rowid,raw in db.execute(f'SELECT rowid,{quoted(col)} FROM {full} WHERE {quoted(col)} LIKE ?',('%'+source+'%',)).fetchall():
                        try:parsed=json.loads(raw)
                        except (ValueError,TypeError):continue
                        changed=convert(parsed,source,target)
                        if changed==parsed:continue
                        encoded=json.dumps(changed,ensure_ascii=False)
                        db.execute(f'UPDATE {full} SET {quoted(col)}=? WHERE rowid=?',(encoded,rowid))
                        if table=='context_evidence' and col=='payload':db.execute(f'UPDATE {full} SET sha256=? WHERE rowid=?',(hashlib.sha256(encoded.encode()).hexdigest(),rowid))
                if table=='agui_runs':
                    for run_id,raw in db.execute(f'SELECT run_id,input_json FROM {full} WHERE buyer_id=?',(target,)).fetchall():
                        body=json.loads(raw);props=body.get('forwardedProps') or {}
                        identity={'threadId':body['threadId'],'buyerId':target,'message':body['messages'][-1],'locale':props.get('locale','zh-CN'),'currency':props.get('currency','CNY')}
                        for key in ('selectedSkill',):
                            if key in props:identity[key]=props[key]
                        if body.get('resume'):identity['resume']=body['resume']
                        digest=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                        db.execute(f'UPDATE {full} SET fingerprint=? WHERE run_id=?',(digest,run_id))
        db.commit()
    except BaseException:
        db.rollback();raise
    finally:db.close()
    (backup/'migration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-dir',type=Path,default=Path('data'));p.add_argument('--source',required=True);p.add_argument('--target',default='pao-coder');args=p.parse_args();print(json.dumps(migrate(args.data_dir,args.source,args.target),ensure_ascii=False,indent=2))
