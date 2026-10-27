"""归档已完成的上下文实验与失败证据；不复制真实买家数据库。"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace
from scripts.eval.report_context import main as report_main


def package(source: Path, output: Path):
    manifest=json.loads((source/'manifest.json').read_text())
    expected=108 if manifest['split']=='dev' else 252
    if manifest.get('status')!='completed' or manifest.get('completed')!=expected or not manifest.get('code_stable'):
        raise ValueError('实验未完整结束或冻结代码变化，不生成正式归档')
    output.mkdir(parents=True,exist_ok=True)
    report_main(SimpleNamespace(input=source,output=output))
    for name in ['manifest.json','results.json','cases.json']:
        if not (source/name).exists():continue
        if (source/name).resolve()!=(output/name).resolve():shutil.copy2(source/name,output/name)
    report=json.loads((output/'report.json').read_text())
    snapshots=[]
    with tarfile.open(output/'failure-evidence.tar.gz','w:gz') as archive:
        for row in report['failures']:
            key=f"{row['case_id']}-{row['strategy']}-{row['repetition']}"
            if not re.fullmatch(r'[a-zA-Z0-9_-]+',key):raise ValueError('非法场景标识')
            folder=source/key
            for name in ['result.json','evidence.db','sessions.db']:
                path=folder/name
                if path.is_file():archive.add(path,arcname=key+'/'+name)
            state={}
            if (folder/'sessions.db').exists():
                with sqlite3.connect('file:'+str((folder/'sessions.db').resolve())+'?mode=ro',uri=True) as db:
                    for (table,) in db.execute("select name from sqlite_master where type='table'"):
                        if not re.fullmatch('[a-zA-Z0-9_]+',table):continue
                        columns=[c[1] for c in db.execute('pragma table_info('+table+')')]
                        if 'state_json' in columns:
                            saved=db.execute('select state_json from '+table+' limit 1').fetchone()
                            if saved:state=json.loads(saved[0])
                            break
            context=state.get('middle_context',{}).get('shoppilot_context',{})
            snapshots.append({'case_id':row['case_id'],'strategy':row['strategy'],'repetition':row['repetition'],
                              'answer':row['answer'],'checks':row['checks'],'association_check':row.get('association_check'),
                              'summary':state.get('summary'),'working':context.get('working'),
                              'summary_revision':context.get('summary_revision'),
                              'last_tool_calls':[{k:block.get(k) for k in ['name','input','state']} for msg in state.get('context',[]) for block in (msg.get('content') if isinstance(msg.get('content'),list) else []) if block.get('type')=='tool_call'][-12:],
                              'database_archive_prefix':key})
    (output/'failure-contexts.json').write_text(json.dumps(snapshots,ensure_ascii=False,indent=2)+'\n')
    digests={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file() and p.name!='checksums.json'}
    digests['grading_source_sha256']=hashlib.sha256(Path(__file__).with_name('report_context.py').read_bytes()).hexdigest()
    (output/'checksums.json').write_text(json.dumps(digests,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();package(args.input,args.output)
