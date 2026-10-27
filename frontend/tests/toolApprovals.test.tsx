// @vitest-environment jsdom
import {act} from "react";
import {createRoot} from "react-dom/client";
import {expect,it,vi} from "vitest";
import {ToolApprovalCards} from "../src/components/ToolApprovalCards";
it("展示待执行内容，批准与拒绝只回传操作ID和决议",async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement('div');const root=createRoot(host);const resolve=vi.fn(async()=>{});
 try{
 await act(async()=>root.render(<ToolApprovalCards items={[{id:'reply:call',tool:'remember_preference_tool',label:'保存购物偏好',arguments:'{"statement":"喜欢裙子","kind":"like"}'}]} busy={false} onResolve={resolve}/>));
 expect(host.textContent).toContain('喜欢裙子');expect(resolve).not.toHaveBeenCalled();
 await act(async()=>[...host.querySelectorAll('button')].find(b=>b.textContent==='批准本次操作')!.click());
 expect(resolve).toHaveBeenLastCalledWith('reply:call',true);
 await act(async()=>[...host.querySelectorAll('button')].find(b=>b.textContent==='拒绝')!.click());
 expect(resolve).toHaveBeenLastCalledWith('reply:call',false);
 }finally{await act(async()=>root.unmount());}
});
