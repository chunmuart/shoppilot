// @vitest-environment jsdom
import {act} from "react";
import {createRoot} from "react-dom/client";
import {expect,it,vi} from "vitest";
import MyOrders from "../src/components/MyOrders";
import type {TradeConfirmation} from "../src/types";
it("订单筛选、详情、取消先准备确认单，不能直接扣改账本",async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 const request=vi.fn(async(path:string)=>({orders:path.includes("CANCELLED")?[]:[{order_id:"GBX-1",status:"CONFIRMED",currency:"CNY",total_amount_major:129,shipping_address:"测试收货地",created_at:"2026-09-09T00:00:00Z",cancel_reason:null,lines:[{sku_id:"s1",title:"背包",quantity:1,unit_price_major:129}]}],total:1}));
 const prepare=vi.fn(async()=>true),resolve=vi.fn(async()=>true);
 const btn=(s:string)=>[...host.querySelectorAll('button')].find(b=>b.textContent===s)!;
 try{
 await act(async()=>root.render(<MyOrders request={request} confirmations={[]} busy={false} error={null} onPrepare={prepare} onResolve={resolve} onRefresh={async()=>{}}/>));
 expect(host.textContent).toContain("GBX-1");
 await act(async()=>btn("订单详情").click());expect(host.textContent).toContain("测试收货地");
 await act(async()=>btn("取消订单").click());
 const input=host.querySelector('input')!;
 await act(async()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,'改变计划');input.dispatchEvent(new Event('input',{bubbles:true}));});
 await act(async()=>host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
 expect(prepare).toHaveBeenCalledWith('GBX-1','改变计划');expect(resolve).not.toHaveBeenCalled();
 await act(async()=>btn("已取消").click());expect(request.mock.calls.at(-1)![0]).toContain('status=CANCELLED');expect(host.textContent).toContain('暂时没有这类订单');
 }finally{await act(async()=>root.unmount());host.remove();}
});

it("取消筛选结果最后一页的唯一订单后，自动回到仍有订单的一页",async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 let total=11;
 const request=vi.fn(async(path:string)=>{
  const offset=Number(new URL(path,"http://test").searchParams.get("offset"));
  return {total,orders:Array.from({length:Math.max(0,Math.min(10,total-offset))},(_,i)=>({
   order_id:"GBX-"+(offset+i),status:"CONFIRMED",currency:"CNY",total_amount_major:99,
   shipping_address:"测试收货地",created_at:"2026-09-09T00:00:00Z",cancel_reason:null,lines:[]
  }))};
 });
 const confirmation:TradeConfirmation={
  confirmation_id:"c",operation_id:"op",action:"cancel",buyer_id:"test",session_id:"s",
  snapshot_hash:"hash",status:"pending",expired:false,expires_at:new Date(Date.now()+60000).toISOString(),result:null,
  payload:{order_id:"GBX-10",reason:"调整计划",items:[],total_amount_minor:9900,currency:"CNY",
   amount_scope:"merchandise_only",order_kind:"purchase_intent",
   shipping_address:{recipient_name:"测试",country:"CN",state:"",city:"",address_line:"测试地址",postal_code:"",phone:""}}
 };
 const btn=(s:string)=>[...host.querySelectorAll("button")].find(b=>b.textContent===s)!;
 try{
  await act(async()=>root.render(<MyOrders request={request} confirmations={[confirmation]} busy={false} error={null}
   onPrepare={async()=>true} onResolve={async()=>{total=10;return true;}} onRefresh={async()=>{}}/>));
  await act(async()=>btn("已确认").click());
  await act(async()=>btn("下一页").click());
  expect(host.querySelector(".order-card h2")!.textContent).toBe("GBX-10");
  await act(async()=>btn("确认取消意向单").click());
  expect(host.querySelector(".order-card h2")?.textContent).toBe("GBX-0");
  expect(host.querySelectorAll(".order-card")).toHaveLength(10);
  expect(request.mock.calls.at(-1)![0]).toContain("offset=0");
 }finally{await act(async()=>root.unmount());host.remove();}
});
