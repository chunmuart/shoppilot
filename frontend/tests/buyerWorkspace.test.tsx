// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import App from "../src/App";

let host: HTMLDivElement, root: Root;
let personal: any[], preferences: any[], requests: any[], failure: boolean;
let sessions: {id:string;title:string;updatedAt:number}[];
let contextOperation: {operation_id:string;status:string}|null;
let finishCompaction: ((response:Response)=>void)|undefined;
beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear();
  localStorage.clear(); localStorage.setItem("shoppilot.access-token", "test-token");
  personal=[]; preferences=[]; requests=[]; failure=false;
  sessions=[];
  contextOperation=null; finishCompaction=undefined;
  vi.stubGlobal("scrollTo",vi.fn()); Element.prototype.scrollIntoView=vi.fn();
  vi.stubGlobal("fetch",vi.fn(async (input: string, init?: RequestInit) => {
    const url=new URL(String(input),"http://test"), path=url.pathname, method=init?.method ?? "GET";
    const body=init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, body, query:url.searchParams, headers:init?.headers });
    if (path.endsWith("/sessions")) return Response.json({sessions});
    if (path.startsWith("/commerce/ag-ui/sessions/")) {
      const id=decodeURIComponent(path.split("/").at(-1)!);
      return Response.json({run:{runId:`run-${id}`,threadId:id,status:"completed",state:{}},
        messages:[{id:`message-${id}`,role:"assistant",content:`已从数据库恢复：${id}`}],activities:[],productHistory:[],events:[]});
    }
    if (path.endsWith("/confirmations")) return Response.json({confirmations:[]});
    if (path.endsWith("/favorites")) return Response.json({products:[]});
    if (path.endsWith("/product-media")) return Response.json({});
    if (path.endsWith("/orders")) return Response.json({orders:[], total:0});
    if (path === "/commerce/context") return Response.json({revision:3,operation:contextOperation});
    if (path === "/commerce/context/compact") {
      contextOperation={operation_id:"compact-1",status:"running"};
      return Response.json(contextOperation);
    }
    if (path === "/commerce/context/operations/compact-1")
      return new Promise<Response>(resolve=>{finishCompaction=resolve;});
    if (path === "/commerce/skills") return Response.json({capability_digest:"c".repeat(64),skills:personal});
    if (path.startsWith("/commerce/my-skills")) {
      if (method==="GET") return Response.json({skills:personal});
      if (failure) return Response.json({detail:"Skill 已被更新，请刷新后再编辑"},{status:409});
      if (method==="DELETE") { personal=[]; return Response.json({deleted:true}); }
      const version=method==="PUT" ? String(Number(personal[0].version)+1) : "1";
      personal=[{id:"personal-test",version,scope:"shopping",content_hash:version==="1" ? "a".repeat(64) : "b".repeat(64),expires_at:null,...body}];
      return Response.json({skill:personal[0]});
    }
    if (path === "/commerce/preferences") {
      if (method==="GET") return Response.json({preferences});
      if (method==="DELETE") { preferences=preferences.filter(p=>p.statement!==body.statement); return Response.json({deleted:true}); }
      if (body.previous_statement) preferences=preferences.filter(p=>p.statement!==body.previous_statement);
      preferences.push({kind:body.kind,statement:body.statement});return Response.json({saved:true});
    }
    if (path === "/commerce/ag-ui/run") return new Response([
      {type:"RUN_STARTED",threadId:body.threadId,runId:body.runId},
      {type:"RUN_FINISHED",threadId:body.threadId,runId:body.runId},
    ].map(event=>"data: "+JSON.stringify(event)+"\n\n").join(""),{headers:{"Content-Type":"text/event-stream"}});
    throw new Error("未预期请求："+path);
  }));
  host=document.createElement("div");document.body.append(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();vi.unstubAllGlobals();});
async function mount(){await act(async()=>root.render(<App/>));}
function button(text:string){return [...host.querySelectorAll("button")].find(b=>b.textContent?.trim()===text)!;}
async function click(element:Element){expect(element).toBeTruthy();await act(async()=>element.dispatchEvent(new MouseEvent("click",{bubbles:true})));}
async function fill(id:string,value:string){
  await act(async()=>{
    const element=host.querySelector<HTMLInputElement|HTMLTextAreaElement>("#"+id)!;
    const prototype=element.tagName==="TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype,"value")!.set!.call(element,value);
    element.dispatchEvent(new Event("input",{bubbles:true}));
  });
}
async function saveSkill(){
  await click(button("我的 Skill"));await click(button("新建 Skill"));await fill("personal-skill-title","我的周末清单");
  await fill("personal-skill-description","准备周末出游时使用");await fill("personal-skill-body","先问行程与预算，最后用清单回答。");
  await act(async()=>host.querySelector(".workspace-editor form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
}

it("从真实页面保存个人 Skill，刷新恢复，再通过斜杠菜单发给官方 AG-UI SDK",async()=>{
  await mount();await saveSkill();
  expect(host.textContent).toContain("Skill 已保存");
  expect(personal[0].body).toContain("先问行程");
  const write=requests.find(r=>r.path==="/commerce/my-skills"&&r.method==="POST");
  expect(write.query.get("buyer_id")).toBeTruthy();
  expect(write.headers.Authorization).toBe("Bearer test-token");
  await act(async()=>root.unmount());root=createRoot(host);await mount();
  await click(button("我的 Skill"));expect(host.textContent).toContain("我的周末清单");
  await click(host.querySelector('[aria-label="ShopPilot 环球好物首页"]')!);await fill("query","/");
  const option=host.querySelector('[role="option"]')!;
  expect(option.textContent).toContain("我的周末清单");await click(option);
  await click(host.querySelector('[aria-label="发送选购需求"]')!);
  const run=requests.find(r=>r.path==="/commerce/ag-ui/run");
  expect(run.body.forwardedProps.selectedSkill).toEqual({id:"personal-test",version:"1",contentHash:"a".repeat(64)});
  expect(run.body.messages[0].content).not.toContain("先问行程与预算");
});

it("Skill 编辑携带原版本，冲突保留正文，删除后斜杠菜单不再显示",async()=>{
  await mount();await saveSkill();
  await click(host.querySelector(".workspace-item-main")!);await fill("personal-skill-body","修改后的独有步骤");
  failure=true;
  await act(async()=>host.querySelector(".workspace-editor form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  expect(host.querySelector<HTMLTextAreaElement>("#personal-skill-body")!.value).toBe("修改后的独有步骤");
  expect(host.querySelector('[role="alert"]')!.textContent).toContain("已被更新");
  expect(requests.find(r=>r.method==="PUT").body.expected_version).toBe("1");
  failure=false;
  await act(async()=>host.querySelector(".workspace-editor form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  expect(personal[0].version).toBe("2");
  await click(host.querySelector('[aria-label="删除 Skill：我的周末清单"]')!);await click(button("确认删除"));
  expect(personal).toEqual([]);await click(host.querySelector('[aria-label="ShopPilot 环球好物首页"]')!);await fill("query","/");
  expect(host.querySelector('[role="option"]')).toBeNull();
});

it("长期偏好支持添加、精确编辑、删除，并从服务器重新加载",async()=>{
  await mount();await click(button("长期偏好"));
  await fill("preference-statement","喜欢黑色");
  await act(async()=>host.querySelector(".workspace-editor form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  await click(host.querySelector('[aria-label="编辑偏好：喜欢黑色"]')!);await fill("preference-statement","喜欢蓝色");
  await act(async()=>host.querySelector(".workspace-editor form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  expect(preferences).toEqual([{kind:"like",statement:"喜欢蓝色"}]);
  expect(requests.find(r=>r.body?.previous_statement)?.body.previous_statement).toBe("喜欢黑色");
  await click(host.querySelector('[aria-label="ShopPilot 环球好物首页"]')!);await click(button("长期偏好"));
  expect(host.textContent).toContain("喜欢蓝色");
  await click(host.querySelector('[aria-label="删除偏好：喜欢蓝色"]')!);
  expect(preferences).toEqual([]);
  const deletion=requests.find(r=>r.method==="DELETE");
  expect(deletion.body).toEqual({statement:"喜欢蓝色"});
  expect(deletion.query.has("statement")).toBe(false);
  expect(host.textContent).toContain("下一轮不再带入");
});


it("刷新后保留偏好页面并重新读取已保存的服务端偏好", async () => {
  sessionStorage.setItem("shoppilot.workspace.view", "preferences");
  preferences=[{kind:"like", statement:"喜欢小香风连衣裙"}];
  await mount();
  expect(host.textContent).toContain("喜欢小香风连衣裙");
  expect(host.querySelector('[aria-label="长期偏好"]')).not.toBeNull();
  const readsBeforeReload=requests.filter(r=>r.path==="/commerce/preferences"&&r.method==="GET").length;
  preferences=[{kind:"like",statement:"喜欢小香风连衣裙"},{kind:"dislike",statement:"不要塑料"}];
  await act(async()=>root.unmount());
  root=createRoot(host);
  await mount();
  expect(host.textContent).toContain("喜欢小香风连衣裙");
  expect(host.textContent).toContain("不要塑料");
  expect(requests.filter(r=>r.path==="/commerce/preferences" && r.method==="GET").length).toBeGreaterThan(readsBeforeReload);
});

it("整理中切到 Skill 页面，后台完成后解除全局忙碌状态", async () => {
  await mount();await fill("query","帮我挑通勤包");
  await click(host.querySelector('[aria-label="发送选购需求"]')!);
  await click(host.querySelector(".context-meter-trigger")!);
  await click(button("整理上下文"));
  expect(host.querySelector('[aria-label="停止生成"]')).toBeNull();
  expect(host.querySelector<HTMLButtonElement>('[aria-label="发送选购需求"]')!.disabled).toBe(true);
  await click(button("我的 Skill"));
  expect(button("新建 Skill").disabled).toBe(true);
  expect(host.querySelector("#personal-skill-title")).toBeNull();
  expect(finishCompaction).toBeTypeOf("function");
  await act(async()=>{
    contextOperation=null;
    finishCompaction!(Response.json({status:"completed",message:"整理完成"}));
  });
  expect(button("新建 Skill").disabled).toBe(false);
  await click(host.querySelector('[aria-label="ShopPilot 环球好物首页"]')!);
  await click(host.querySelector(".context-meter-trigger")!);
  expect(button("整理上下文").disabled).toBe(false);
});

it("在偏好页刷新也会恢复当前会话的整理状态", async () => {
  sessionStorage.setItem("shoppilot.workspace.view","preferences");
  contextOperation={operation_id:"compact-1",status:"running"};
  await mount();
  expect(requests.some(r=>r.path==="/commerce/context")).toBe(true);
  expect(host.querySelector<HTMLTextAreaElement>("#preference-statement")!.matches(":disabled")).toBe(true);
  await act(async()=>{
    contextOperation=null;
    finishCompaction!(Response.json({status:"completed",message:"恢复完成"}));
  });
  expect(host.querySelector<HTMLTextAreaElement>("#preference-statement")!.matches(":disabled")).toBe(false);
});

it("侧栏点击搜索才显示输入，按 ID 找到历史并读取服务端正文，不启动新运行", async () => {
  sessions=[{id:"session-latest",title:"通勤耳机",updatedAt:200},{id:"session-backpack",title:"周末轻便背包",updatedAt:100}];
  await mount();
  expect(host.querySelector(".sidebar input")).toBeNull();
  const trigger=host.querySelector<HTMLButtonElement>(".sidebar-search-trigger")!;
  trigger.focus();await click(trigger);
  const input=host.querySelector<HTMLInputElement>('[role="combobox"][aria-label="搜索选购标题或会话 ID"]')!;
  expect(document.activeElement).toBe(input);
  await act(async()=>{
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(input," SESSION-BACKPACK ");
    input.dispatchEvent(new Event("input",{bubbles:true}));
  });
  expect(host.querySelectorAll('.conversation-search [role="option"]')).toHaveLength(1);
  await act(async()=>input.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true})));
  expect(host.querySelector('.conversation-search')).toBeNull();
  expect(host.textContent).toContain("已从数据库恢复：session-backpack");
  expect(requests.some(r=>r.path==="/commerce/ag-ui/sessions/session-backpack" && r.method==="GET")).toBe(true);
  expect(requests.some(r=>r.path==="/commerce/ag-ui/run")).toBe(false);
  expect(host.querySelectorAll('.sidebar-history button')).toHaveLength(2);
});

it("从手机导航打开搜索时关闭抽屉，关闭搜索恢复导航按钮焦点和滚动", async () => {
  await mount();
  const toggle=host.querySelector<HTMLButtonElement>('[aria-label="打开导航"]')!;
  toggle.focus();await click(toggle);
  expect(host.querySelector('.sidebar.is-open')).not.toBeNull();
  await click(host.querySelector('.sidebar-search-trigger')!);
  expect(host.querySelector('.sidebar.is-open')).toBeNull();
  expect(document.activeElement).toBe(host.querySelector('.conversation-search input'));
  expect(document.body.style.overflow).toBe("hidden");
  await act(async()=>document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true})));
  expect(host.querySelector('.conversation-search')).toBeNull();
  expect(document.activeElement).toBe(toggle);
  expect(document.body.style.overflow).toBe("");
});

it("最近选购承担会话定位，自动化标识不占独立导航，工具过程仍在对话流", async () => {
  sessions=[{id:"automated",title:"每周清单",updatedAt:200},{id:"manual",title:"手动比较",updatedAt:100}];
  const original=globalThis.fetch;
  vi.stubGlobal("fetch",vi.fn(async (input:string,init?:RequestInit) => String(input).includes('/automations/session-origins')
    ? Response.json({session_ids:['automated']}) : original(input,init)));
  await mount();
  expect(button('我的选购')).toBeUndefined();
  expect(host.querySelector('.conversation-toolbar')).toBeNull();
  const automated=host.querySelector('.history-short.current')!;
  expect(automated.textContent).toContain('每周清单');
  expect(automated.getAttribute('aria-current')).toBe('page');
  expect(automated.querySelector('[aria-label="自动化会话"]')).not.toBeNull();
  await click(button('长期偏好'));
  expect(host.querySelector('.history-short.current')).toBeNull();
  await click(automated);
  expect(host.querySelector('.history-short.current')).not.toBeNull();
  expect(host.textContent).toContain('已从数据库恢复：automated');
  expect(requests.filter(r=>r.path==='/commerce/ag-ui/run')).toHaveLength(0);
});


it("Skill 方法库默认可见，按需编辑且收起不丢草稿", async () => {
  await mount(); await click(button("我的 Skill"));
  expect(host.textContent).toContain("我的方法库");
  expect(host.querySelector(".workspace-editor")).toBeNull();
  await click(button("新建 Skill")); await fill("personal-skill-title", "未保存的旅行方法");
  await click(host.querySelector('[role="dialog"] [aria-label="关闭"]')!);
  expect(host.querySelector(".workspace-editor")).toBeNull();
  await click(button("新建 Skill"));
  expect(host.querySelector<HTMLInputElement>("#personal-skill-title")!.value).toBe("未保存的旅行方法");
  expect(requests.some(r=>r.path==="/commerce/my-skills" && r.method==="POST")).toBe(false);
});

it("订单与收藏共享导航，收藏刷新恢复，旧历史入口回到选购", async () => {
  sessionStorage.setItem("shoppilot.workspace.view", "favorites");
  await mount();
  expect(host.querySelector('.nav-item.active')?.textContent).toBe("订单与收藏");
  expect(host.querySelector('#favorites-tab')?.getAttribute('aria-selected')).toBe('true');
  expect(host.querySelector('.composer-dock')).toBeNull();
  expect([...host.querySelectorAll('.nav-item')].some(b=>b.textContent?.includes('对话历史')||b.textContent?.includes('心选收藏'))).toBe(false);
  await click(button('我的订单'));
  expect(requests.some(r=>r.path==='/commerce/orders')).toBe(true);
  await act(async()=>root.unmount()); root=createRoot(host);
  sessionStorage.setItem('shoppilot.workspace.view','history'); await mount();
  expect(host.querySelector('#chat-panel')).not.toBeNull();
  expect(host.querySelector('.sidebar-search-trigger')).not.toBeNull();
});
