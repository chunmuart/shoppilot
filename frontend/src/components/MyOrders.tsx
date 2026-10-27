import { useCallback, useEffect, useRef, useState } from "react";
import ConfirmationCards from "./ConfirmationCards";
import type { TradeConfirmation } from "../types";
import { money, ProductImage } from "./ProductCards";
import "./buyerWorkspace.css";
import "./myOrders.css";

type Order = {order_id:string;status:string;currency:string;total_amount_major:number;shipping_address:string;created_at:string;cancel_reason:string|null;lines:{sku_id:string;title:string;quantity:number;unit_price_major:number}[]};
type Props = {request:(path:string,method?:string,body?:Record<string,unknown>)=>Promise<any>;confirmations:TradeConfirmation[];busy:boolean;error:string|null;onPrepare:(id:string,reason:string)=>Promise<boolean>;onResolve:(c:TradeConfirmation,approved:boolean)=>Promise<boolean>;onRefresh:()=>Promise<void>};
const labels:Record<string,string>={CONFIRMED:"已确认",CANCELLED:"已取消",DRAFT:"草稿"};
export default function MyOrders({request,confirmations,busy,error,onPrepare,onResolve,onRefresh}:Props){
 const [orders,setOrders]=useState<Order[]>([]),[total,setTotal]=useState(0),[offset,setOffset]=useState(0),[status,setStatus]=useState("");
 const [loading,setLoading]=useState(true),[failure,setFailure]=useState(""),[detail,setDetail]=useState<string|null>(null),[cancel,setCancel]=useState<string|null>(null),[reason,setReason]=useState("");
 const revision=useRef(0);
 const refresh=useCallback(async()=>{const id=++revision.current;setLoading(true);setFailure("");try{
  const data=await request(`/orders?offset=${offset}&limit=10${status?`&status=${status}`:""}`);
  if(id!==revision.current)return;
  if(!Array.isArray(data.orders)||typeof data.total!=="number")throw new Error("订单数据格式无效");
  const lastOffset=Math.max(0,Math.floor((data.total-1)/10)*10);
  if(offset>lastOffset){
   // 取消订单后当前页可能失效，回到仍存在的最后一页再读取。
   ++revision.current;setDetail(null);setOffset(lastOffset);return;
  }
  setOrders(data.orders);setTotal(data.total);
 }catch(e){if(id===revision.current)setFailure(e instanceof Error?e.message:"订单暂时无法读取");}finally{if(id===revision.current)setLoading(false);}},[request,status,offset]);
 useEffect(()=>{void refresh();return()=>{++revision.current;};},[refresh]);
 return <section className="buyer-workspace compact-orders" aria-label="我的订单">

  <div className="orders-toolbar"><div role="group" aria-label="订单状态筛选">{[["","全部"],["CONFIRMED","已确认"],["CANCELLED","已取消"]].map(([value,label])=><button key={value} type="button" aria-pressed={status===value} onClick={()=>{setStatus(value);setOffset(0);setDetail(null);}}>{label}</button>)}</div><button type="button" onClick={()=>void refresh()} disabled={loading}>刷新订单</button></div>
  {failure&&<div className="workspace-error" role="alert">{failure}<button onClick={()=>void refresh()}>重试</button></div>}
  {loading?<p role="status">正在读取订单…</p>:!orders.length?<div className="workspace-empty"><h2>{status?"暂时没有这类订单":"还没有订单"}</h2><p>在选购对话中挑选商品并确认下单后，订单会保存在这里。</p></div>:<div className="order-list">{orders.map(o=><article className="order-card" key={o.order_id}>
   <header><div><small>{new Date(o.created_at).toLocaleString("zh-CN")}</small><h2 title={o.order_id}>{o.order_id}</h2></div><span className={`order-status ${o.status}`}>{labels[o.status]??o.status}</span></header>
   <div className="order-lines">{o.lines.map(line=><div key={line.sku_id}><span className="order-thumbnail"><ProductImage product={{ sku_id: line.sku_id, title: line.title }} /></span><div className="order-line-info"><strong>{line.title}</strong><small>{line.sku_id} · 数量 {line.quantity} · 图片仅示意</small></div><span>{money(line.unit_price_major,o.currency)} / 件</span></div>)}</div>
   <footer><span className="order-total">商品合计 <strong>{money(o.total_amount_major,o.currency)}</strong></span><div><button type="button" aria-expanded={detail===o.order_id} onClick={()=>setDetail(detail===o.order_id?null:o.order_id)}>订单详情</button>{o.status==="CONFIRMED"&&<button type="button" disabled={busy} onClick={()=>{setCancel(o.order_id);setReason("");}}>取消订单</button>}</div></footer>
   {detail===o.order_id&&<div className="order-detail"><p>收货信息：{o.shipping_address}</p>{o.cancel_reason&&<p>取消原因：{o.cancel_reason}</p>}<p>当前为本地演示订单，尚未接入支付和物流；金额不代表实际支付。</p></div>}
   {cancel===o.order_id&&o.status==="CONFIRMED"&&<form className="order-detail" onSubmit={async e=>{e.preventDefault();if(await onPrepare(o.order_id,reason.trim()))setCancel(null);}}><label>取消原因<input aria-label="取消原因" required maxLength={500} value={reason} onChange={e=>setReason(e.target.value)} placeholder="例如：调整了购买计划"/></label><button type="submit" disabled={busy||!reason.trim()}>查看取消确认单</button><button type="button" onClick={()=>setCancel(null)}>保留订单</button></form>}
  </article>)}</div>}
  {total>10&&<nav className="orders-pagination" aria-label="订单分页"><button disabled={loading||offset===0} onClick={()=>setOffset(Math.max(0,offset-10))}>上一页</button><span>共 {total} 单 · 第 {Math.floor(offset/10)+1} 页</span><button disabled={loading||offset+10>=total} onClick={()=>setOffset(offset+10)}>下一页</button></nav>}
  <ConfirmationCards confirmations={confirmations.filter(c=>c.action==="cancel")} busy={busy} error={error} onResolve={async(c,approved)=>{if(await onResolve(c,approved))await refresh();}} onCancelOrder={(id,r)=>void onPrepare(id,r)} onRefresh={()=>void onRefresh()}/>
 </section>;
}
