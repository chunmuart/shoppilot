// @vitest-environment jsdom
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {it,expect,vi} from 'vitest';
import ContextWorkspace, {useContextWorkspace} from '../src/components/ContextWorkspace';
function Workspace(props:Parameters<typeof useContextWorkspace>[0]){
 return <ContextWorkspace state={useContextWorkspace(props)}/>;
}
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
async function openContext(host:HTMLElement){
 await act(async()=>host.querySelector<HTMLButtonElement>('.context-meter-trigger')!.click());
}

it('摘要显示、提交版本和幂等标识，审批时不允许整理',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const request=vi.fn(async(path:string)=>path==='/context/compact'?{operation_id:'op',status:'completed',message:'已整理'}:{revision:3,working:{goal:'通勤包',constraints:{budget:{source:'预算300元'}}}});
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>));
  await openContext(host);
  expect(host.textContent).toContain('预算300元');
  await act(async()=>host.querySelector<HTMLButtonElement>('.context-compact-button')!.click());
  expect(host.textContent).toContain('已整理');
  expect(request).toHaveBeenCalledWith('/context/compact','POST',expect.objectContaining({session_id:'s',expected_revision:3,request_id:expect.any(String)}));
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending hasMessages request={request}/>));
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(true);
 }finally{await act(async()=>root.unmount());host.remove();}
});
it('刷新从数据库操作恢复进度并读取完成结果',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 let done=false;const request=vi.fn(async(path:string)=>{if(path.includes('/operations/')){done=true;return{status:'completed',message:'恢复完成'};}return{revision:2,operation:done?null:{status:'running',operation_id:'op'}};});
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>));
  await openContext(host);
  expect(host.textContent).toContain('恢复完成');expect(request).toHaveBeenCalledWith('/context/operations/op');
 }finally{await act(async()=>root.unmount());host.remove();}
});

it('提交等待期间禁止重复整理，完成后再次提交使用最新版本',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 let revision=3,complete:((data:Record<string,unknown>)=>void)|undefined;
 const request=vi.fn(async(path:string)=>{
  if(path==='/context/compact')return new Promise<Record<string,unknown>>(resolve=>{complete=resolve;});
  return {revision};
 });
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>));
  await openContext(host);
  await act(async()=>{host.querySelector<HTMLButtonElement>('.context-compact-button')!.click();host.querySelector<HTMLButtonElement>('.context-compact-button')!.click();});
  expect(request.mock.calls.filter(([path])=>path==='/context/compact')).toHaveLength(1);
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(true);
  await act(async()=>{revision=4;complete!({status:'completed',operation_id:'op',message:'完成'});});
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(false);
  await act(async()=>host.querySelector<HTMLButtonElement>('.context-compact-button')!.click());
  expect(request).toHaveBeenLastCalledWith('/context/compact','POST',expect.objectContaining({expected_revision:4}));
  await act(async()=>complete!({status:'completed',operation_id:'op2',message:'完成'}));
 }finally{await act(async()=>root.unmount());host.remove();}
});

it('切换会话后旧整理结果不会覆盖新会话',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 let complete:((data:Record<string,unknown>)=>void)|undefined;
 const request=vi.fn(async(path:string)=>{
  if(path.includes('/operations/'))return new Promise<Record<string,unknown>>(resolve=>{complete=resolve;});
  return path.endsWith('old')?{revision:1,operation:{status:'running',operation_id:'op'}}:{revision:2,working:{goal:'新的选购'}};
 });
 try{
  await act(async()=>root.render(<Workspace sessionId="old" busy={false} pending={false} hasMessages request={request}/>));
  await openContext(host);
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(true);
  await act(async()=>root.render(<Workspace sessionId="new" busy={false} pending={false} hasMessages request={request}/>));
  await act(async()=>complete!({status:'completed',message:'旧操作完成'}));
  expect(host.textContent).toContain('新的选购');expect(host.textContent).not.toContain('旧操作完成');
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(false);
 }finally{await act(async()=>root.unmount());host.remove();}
});

it('整理提交失败会重新读取服务端状态并允许重试，保留摘要',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const request=vi.fn(async(path:string)=>{
  if(path==='/context/compact')throw new Error('网络暂不可用');
  return {revision:3,working:{goal:'通勤包'}};
 });
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>));
  await openContext(host);
  await act(async()=>host.querySelector<HTMLButtonElement>('.context-compact-button')!.click());
  expect(host.textContent).toContain('网络暂不可用');expect(host.textContent).toContain('通勤包');
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(false);
  expect(request.mock.calls.at(-1)![0]).toBe('/context?session_id=s');
 }finally{await act(async()=>root.unmount());host.remove();}
});

it('圆环默认收起，打开只读浮层不会提交聊天，Escape及外部点击可关闭',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const submit=vi.fn(),request=vi.fn(async()=>({revision:3,working:{goal:'通勤包'}}));
 try{
  await act(async()=>root.render(<form onSubmit={event=>{event.preventDefault();submit();}}>
   <Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>
   <button type="submit">发送</button></form>));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(host.textContent).not.toContain('本次选购摘要');
  await openContext(host);
  expect(host.querySelector('[role="dialog"]')).not.toBeNull();
  expect(host.textContent).toContain('本次选购摘要');
  expect(submit).not.toHaveBeenCalled();
  await act(async()=>document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement).toBe(host.querySelector('.context-meter-trigger'));
  await openContext(host);
  await act(async()=>document.body.dispatchEvent(new Event('pointerdown',{bubbles:true})));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
 }finally{await act(async()=>root.unmount());host.remove();}
});

it.each([
 [32000,128000,3,'25%',true],
 [0,128000,3,'0%',true],
 [200,128000,3,'<1%',true],
 [160000,128000,3,'125%',true],
 [32000,0,3,'0%',false],
 [NaN,128000,3,'0%',false],
 [32000,128000,2,'0%',false],
])('窗口占用基于有效且同版本的已保存估算：%s / %s, revision %s',async(used,window,revision,expected,known)=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const request=vi.fn(async()=>({revision:3,statistics:{after_tokens:1},usage:{
  estimated_input_tokens:used,context_window:window,input_budget:110000,
  output_reserve:8192,safety_reserve:6400,saved_revision:revision,source:'saved_context_estimate',
 }}));
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>));
  expect(host.querySelector('.context-meter-trigger')?.textContent).toBe(expected);
  await openContext(host);
  if(known)expect(host.textContent).toContain('已使用（估算）');
  else {
   expect(host.textContent).toContain('暂时无法估算');
   expect(host.textContent).toContain('默认值 · 尚无统计');
   expect(host.textContent).not.toContain('已使用（估算）');
  }
  const dash=host.querySelector('.context-ring-value')?.getAttribute('stroke-dasharray');
  if(expected==='125%')expect(dash).toBe('100 100');
 }finally{await act(async()=>root.unmount());host.remove();}
});

it('无上下文或接口无usage时显示默认0%，不能拿最近整理的tokens冒充比例',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const request=vi.fn(async()=>({revision:3,statistics:{after_tokens:1200}}));
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages={false} request={request}/>));
  expect(host.querySelector('.context-meter-trigger')?.textContent).toBe('0%');
  expect(host.querySelector('.context-meter-trigger')?.getAttribute('aria-label')).toContain('尚无可用统计');
  await openContext(host);
  expect(host.textContent).toContain('开始对话后');
  expect(host.textContent).toContain('默认值 · 尚无统计');
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(true);
 }finally{await act(async()=>root.unmount());host.remove();}
});

it('生成中仍能查看上轮估算，禁止手动整理，结束后刷新用量',async()=>{
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 let used=32000;
 const request=vi.fn(async()=>({revision:3,usage:{estimated_input_tokens:used,context_window:128000,
  output_reserve:8192,safety_reserve:6400,saved_revision:3,source:'saved_context_estimate'}}));
 try{
  await act(async()=>root.render(<Workspace sessionId="s" busy pending={false} hasMessages request={request}/>));
  await openContext(host);
  expect(host.textContent).toContain('上轮已保存的估算');
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(true);
  expect(host.querySelector<HTMLButtonElement>('.context-meter-trigger')!.disabled).toBe(false);
  used=64000;
  await act(async()=>root.render(<Workspace sessionId="s" busy={false} pending={false} hasMessages request={request}/>));
  expect(host.querySelector('.context-meter-trigger')?.textContent).toBe('50%');
  expect(host.querySelector<HTMLButtonElement>('.context-compact-button')!.disabled).toBe(false);
 }finally{await act(async()=>root.unmount());host.remove();}
});
