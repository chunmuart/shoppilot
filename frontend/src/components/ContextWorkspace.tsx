import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { WorkspaceRequest } from './BuyerWorkspace';
import './contextWorkspace.css';

type Options={sessionId:string;busy:boolean;pending:boolean;hasMessages:boolean;request:WorkspaceRequest};
type ContextUsage={
 estimated_input_tokens:number;context_window:number;input_budget:number;
 output_reserve:number;safety_reserve:number;saved_revision:number;
 source:'saved_context_estimate';model?:string;context_max?:number;counter_method?:string;
};
type View={revision:number;strategy?:string;summary?:string;usage?:ContextUsage|null;working?:{goal?:string;latest_request?:string;constraints?:Record<string,{source?:string;value?:string;currency?:string}>;selected?:string[];comparisons?:string[]};statistics?:{status?:string;before_tokens?:number;after_tokens?:number}};
// 在 App 会话层调用，切到其它页面时仍继续恢复和轮询整理操作。
export function useContextWorkspace({sessionId,busy,pending,hasMessages,request}:Options){
 const [view,setView]=useState<View|null>(null),[operation,setOperation]=useState(''),[checking,setChecking]=useState(true),[submitting,setSubmitting]=useState(false);
 const [notice,setNotice]=useState(''),[error,setError]=useState('');
 const generation=useRef(0),readRevision=useRef(0),submitLock=useRef(false);
 const refresh=useCallback(async(current:number)=>{
  const revision=++readRevision.current;
  try{
   const data=await request('/context?session_id='+encodeURIComponent(sessionId));
   if(current!==generation.current||revision!==readRevision.current)return;
   setView(data as View);
   const active=data.operation as {operation_id?:string;status?:string}|undefined;
   setOperation(active?.status==='running'&&active.operation_id?active.operation_id:'');
  }catch(e){
   if(current===generation.current&&revision===readRevision.current&&!(e instanceof Error&&'status' in e&&e.status===404))
    setError(e instanceof Error?e.message:'摘要暂时无法读取');
  }finally{if(current===generation.current&&revision===readRevision.current)setChecking(false);}
 },[request,sessionId]);
 useEffect(()=>{
  const current=++generation.current;
  setView(null);setError('');setNotice('');setOperation('');setSubmitting(false);setChecking(true);submitLock.current=false;
  // 没有浏览器消息缓存时也查询数据库，避免漏掉正在执行的整理。
  void refresh(current);
  return()=>{++generation.current;};
 },[refresh]);
 const previous=useRef({busy,hasMessages});
 useEffect(()=>{
  const before=previous.current;previous.current={busy,hasMessages};
  if(!busy&&hasMessages&&(before.busy||!before.hasMessages))void refresh(generation.current);
 },[busy,hasMessages,refresh]);
 useEffect(()=>{
  if(!operation)return;
  const current=generation.current;
  let stopped=false,timer:ReturnType<typeof setTimeout>;
  const poll=async()=>{
   try{
    const data=await request('/context/operations/'+encodeURIComponent(operation));
    if(stopped||current!==generation.current)return;
    if(data.status!=='running'){
     setOperation('');setError('');setNotice(typeof data.message==='string'?data.message:'整理已结束');
     await refresh(current);return;
    }
   }catch(e){if(!stopped)setError(e instanceof Error?e.message:'读取整理进度失败，正在重试');}
   if(!stopped)timer=setTimeout(poll,1500);
  };void poll();return()=>{stopped=true;clearTimeout(timer);};
 },[operation,request,refresh]);
 const running=checking||submitting||!!operation;
 const compact=async()=>{
  if(!view||busy||pending||running||submitLock.current)return;
  const current=generation.current;
  submitLock.current=true;++readRevision.current;setSubmitting(true);setError('');setNotice('');
  try{
   const data=await request('/context/compact','POST',{session_id:sessionId,request_id:crypto.randomUUID(),expected_revision:view.revision});
   if(current!==generation.current)return;
   if(data.status==='running')setOperation(String(data.operation_id));
   else{setNotice(String(data.message??'整理已结束'));await refresh(current);}
  }catch(e){
   if(current===generation.current){setError(e instanceof Error?e.message:'未能整理，原记录保留');await refresh(current);}
  }finally{if(current===generation.current){submitLock.current=false;setSubmitting(false);}}
 };
 const reload=useCallback(()=>{void refresh(generation.current);},[refresh]);
 return {reload,view,running,checking,compacting:submitting||!!operation,notice,error,compact,busy,pending,hasMessages};
}

export default function ContextWorkspace({state}:{state:ReturnType<typeof useContextWorkspace>}){
 const {view,running,checking,compacting,notice,error,compact,busy,pending,hasMessages}=state;
 const [open,setOpen]=useState(false);
 const root=useRef<HTMLDivElement>(null),trigger=useRef<HTMLButtonElement>(null),panel=useRef<HTMLDivElement>(null);
 const id=useId();
 const close=(restoreFocus=false)=>{setOpen(false);if(restoreFocus)trigger.current?.focus();};
 useEffect(()=>{
  if(!open)return;
  panel.current?.focus();
  const outside=(event:PointerEvent)=>{if(!root.current?.contains(event.target as Node))setOpen(false);};
  const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'){event.preventDefault();setOpen(false);trigger.current?.focus();}};
  document.addEventListener('pointerdown',outside);
  document.addEventListener('keydown',escape);
  return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape);};
 },[open]);
 // 比例来自同一版本的完整模型输入估算，不能用累计消耗或上次整理统计代替。
 const usage=view?.usage;
 const known=!!usage&&usage.source==='saved_context_estimate'&&usage.saved_revision===view?.revision
  &&Number.isFinite(usage.estimated_input_tokens)&&usage.estimated_input_tokens>=0
  &&Number.isFinite(usage.context_window)&&usage.context_window>0;
 const ratio=known?usage.estimated_input_tokens/usage.context_window:null;
 // 圆环用 0% 作为初始显示；known 仍区分缺失统计与实际零用量。
 const percentage=ratio===null?'0%':ratio>0&&ratio<.01?'<1%':Math.round(ratio*100)+'%';
 const ring=ratio===null?0:Math.min(1,ratio)*100;
 const tone=ratio!==null&&ratio>=.8?'high':ratio!==null&&ratio>=.6?'medium':'normal';
 const label=ratio===null?'上下文默认显示 0%，尚无可用统计，点击查看':`上下文使用 ${percentage}，点击查看`;
 const tokens=(value:number)=>Math.round(value).toLocaleString('zh-CN');
 return <div className="context-meter" ref={root} onBlur={(event)=>{
  if(event.relatedTarget&&!event.currentTarget.contains(event.relatedTarget as Node))setOpen(false);
 }}>
  <button ref={trigger} type="button" className={`context-meter-trigger ${tone} ${compacting?'is-compacting':''}`}
   aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={open} aria-controls={open?id:undefined}
   onClick={()=>setOpen(value=>!value)}>
   <svg viewBox="0 0 36 36" aria-hidden="true">
    <circle className="context-ring-track" cx="18" cy="18" r="14" pathLength="100"/>
    <circle className="context-ring-value" cx="18" cy="18" r="14" pathLength="100" strokeDasharray={`${ring} 100`}/>
   </svg>
   <span>{compacting?'···':percentage}</span>
   {(error||notice)&&<i className={error?'has-error':'has-update'} aria-hidden="true"/>}
  </button>
  {open&&<div ref={panel} id={id} className="context-popover" role="dialog" aria-label="上下文使用情况" tabIndex={-1}>
   <header className="context-popover-heading"><div><strong>上下文窗口</strong><span>本次对话的工作空间</span></div>
    <button type="button" className="context-popover-close" aria-label="关闭上下文详情" onClick={()=>close(true)}>×</button>
   </header>
   <div className={`context-usage-overview ${tone}`}>
    <strong>{percentage}<small>{known?'已使用（估算）':'默认值 · 尚无统计'}</small></strong>
    {known&&<span>{tokens(usage.estimated_input_tokens)} / {tokens(usage.context_window)}<small>tokens · 选用窗口预算</small></span>}
   </div>
   {known&&usage.model&&<p className="context-usage-note">{usage.model} · 模型声明上限 {tokens(usage.context_max??usage.context_window)} tokens</p>}
   <div className={`context-usage-bar ${tone}`} aria-hidden="true"><span style={{width:ring+'%'}}/></div>
   <p className="context-usage-note">{checking?'正在读取会话上下文…':known
    ?(busy?'正在生成，显示上轮已保存的估算；本轮结束后更新。':'包含系统提示、工具定义和已保存的对话；不含正在输入的草稿。')
    :hasMessages?'暂时无法估算用量，原对话和整理记录仍保留。':'开始对话后，将显示已保存上下文的估算占用。'}</p>
   {known&&Number.isFinite(usage.output_reserve)&&<div className="context-reserves">
    <span>回答预留 <b>{tokens(usage.output_reserve)}</b></span>
    {Number.isFinite(usage.safety_reserve)&&<span>安全余量 <b>{tokens(usage.safety_reserve)}</b></span>}
   </div>}
   <button type="button" className="context-compact-button" onClick={()=>void compact()} disabled={!view||!hasMessages||busy||pending||running}>
    <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 6h14M5 10h10M7 14h6"/></svg>
    {compacting?'正在整理…':'整理上下文'}
   </button>
   <p className="context-compact-hint">{pending?'请先完成或拒绝待确认操作。':busy?'当前回复完成后即可整理。':'压缩工作上下文，完整聊天和商品历史仍保留。'}</p>
   {notice&&<p className="context-notice" role="status">{notice}</p>}
   {error&&<p className="context-error" role="alert">{error}</p>}
   {hasMessages&&<details className="context-summary"><summary>本次选购摘要</summary>
    {view?.strategy==='legacy'&&view.working?.goal&&<small>最近一次整理的记录，后续补充以对话为准。</small>}
    {view?.working?.goal&&<p>最初需求：{view.working.goal}</p>}
    {view?.working?.latest_request&&<p>最近补充：{view.working.latest_request}</p>}
    {view?.working?.constraints&&<ul>{Object.entries(view.working.constraints).map(([key,value])=><li key={key}>{value.source??value.value}{value.currency&&`（${value.currency}）`}</li>)}</ul>}
    {!!view?.working?.selected?.length&&<p>关注商品：{view.working.selected.join('、')}</p>}
    {!!view?.working?.comparisons?.length&&<p>比较商品：{view.working.comparisons.join('、')}</p>}
    {!view?.working?.goal&&<p>尚未整理本次需求。你可以继续补充，完整对话会保留。</p>}
    <small>价格与库存以重新查询为准；需要修改需求，直接告诉 ShopPilot。</small>
   </details>}
  </div>}
 </div>;
}
