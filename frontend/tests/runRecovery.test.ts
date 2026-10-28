import { describe, expect, it } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";

const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
const sse = (runId: string, items: Array<[number, Record<string, unknown>]>) => new Response(items.map(([seq, event]) =>
  `id: ${runId}:${seq}\ndata: ${JSON.stringify(event)}\n\n`).join(""), { headers: { "Content-Type": "text/event-stream" } });
const waitUntil = async (predicate: () => boolean) => {
  const deadline = Date.now() + 2000;
  while (!predicate()) { if (Date.now() > deadline) throw new Error("等待测试状态超时"); await new Promise((resolve) => setTimeout(resolve, 5)); }
};
const store = () => {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
};

it("重开本次新建的会话也查数据库，恢复超100条原文和运行记录", async () => {
  const storage=store();
  const full=Array.from({length:120},(_,index)=>({id:`old-${index}`,role:index%2 ? 'assistant':'user',content:`历史消息${index}`}));
  let request:any, loads=0;
  const client=new CommerceClient({url:'/commerce/ag-ui/run',storage,fetch:async(url,init)=>{
    if(init.method==='POST') {
      request=JSON.parse(String(init.body));
      return sse(request.runId,[[1,{type:'RUN_STARTED',threadId:request.threadId,runId:request.runId}],
        [2,{type:'RUN_FINISHED',threadId:request.threadId,runId:request.runId}]]);
    }
    loads++;
    return json({messages:full,productHistory:[],events:[{id:'persisted:1',type:'TOOL_CALL_START',timestamp:1234}],
      run:{runId:request.runId,threadId:request.threadId,status:'completed',messages:full.slice(-100),state:{products:[],toolApprovals:[]}}});
  }});
  await client.submit('新建但列表尚未刷新');
  const saved=client.getSnapshot().sessionId;
  client.reset(); client.setSession(saved);
  await waitUntil(()=>loads>0 && client.getSnapshot().messages.length===120);
  expect(client.getSnapshot().messages[0].content).toBe('历史消息0');
  expect(client.getSnapshot().events[0]).toMatchObject({id:'persisted:1',label:'调用工具',timestamp:1234});
  expect(client.getSnapshot().historyError).toBeNull();
});

it("完整历史续聊不消失，不把全部页面历史发给模型，也不保留被最终回答替换的草稿",async()=>{
  const full=Array.from({length:120},(_,index)=>({id:`old-${index}`,role:index%2 ? 'assistant':'user',content:`历史消息${index}`}));
  const client=new CommerceClient({url:'/commerce/ag-ui/run',fetch:async(url,init)=>{
    if(init.method==='POST') {
      const body=JSON.parse(String(init.body));expect(body.messages).toHaveLength(100);
      return sse(body.runId,[[1,{type:'RUN_STARTED',threadId:body.threadId,runId:body.runId}],
        [2,{type:'TEXT_MESSAGE_START',messageId:'draft',role:'assistant'}],
        [3,{type:'TEXT_MESSAGE_CONTENT',messageId:'draft',delta:'草稿'}],
        [4,{type:'TEXT_MESSAGE_END',messageId:'draft'}],
        [5,{type:'MESSAGES_SNAPSHOT',messages:[...body.messages,{id:'final',role:'assistant',content:'最终答复'}]}],
        [6,{type:'RUN_FINISHED',threadId:body.threadId,runId:body.runId}]]);
    }
    if(url.includes('/sessions?')) return json({sessions:[{id:'s',title:'长会话',updatedAt:1}]});
    return json({messages:full,run:{runId:'r',threadId:'s',status:'completed',messages:full.slice(-100),state:{}}});
  }});
  await client.initialize();await client.submit('继续');
  expect(client.getSnapshot().messages).toHaveLength(122);
  expect(client.getSnapshot().messages[0].content).toBe('历史消息0');
  expect(client.getSnapshot().messages.at(-1)?.content).toBe('最终答复');
  expect(client.getSnapshot().messages.some(m=>m.id==='draft')).toBe(false);
});

it("快速切换 A→B→A 时拒绝第一次迟到响应，恢复失败后再次打开可重试",async()=>{
  let firstResolve!:(response:Response)=>void, loads=0;
  const result=(id:string,content:string)=>json({run:{runId:id,threadId:id,status:'completed',
    messages:[{id,role:'assistant',content}],state:{}}});
  const client=new CommerceClient({url:'/commerce/ag-ui/run',fetch:async(url)=>{
    if(url.includes('/sessions?'))return json({sessions:[{id:'a',title:'A',updatedAt:1},{id:'b',title:'B',updatedAt:2}]});
    if(url.includes('/sessions/a')) {
      loads++;
      if(loads===1)return new Promise<Response>(resolve=>{firstResolve=resolve;});
      if(loads===2)throw new Error('暂时断网');
      return result('a','数据库最新版');
    }
    return result('b','会话B');
  }});
  await client.initialize();client.setSession('a');client.setSession('b');client.setSession('a');
  await waitUntil(()=>client.getSnapshot().historyError!==null);
  firstResolve(result('a','迟到旧内容'));await new Promise(resolve=>setTimeout(resolve,0));
  expect(client.getSnapshot().messages.some(m=>m.content==='迟到旧内容')).toBe(false);
  client.setSession('a');await waitUntil(()=>client.getSnapshot().historyError===null);
  expect(client.getSnapshot().messages[0].content).toBe('数据库最新版');
});

it("长历史恢复进行中的运行时只重连事件，不重复模型或丢掉早期原文",async()=>{
  const full=Array.from({length:121},(_,index)=>({id:`m-${index}`,role:index%2 ? 'assistant':'user',content:`消息${index}`}));
  let posts=0;
  const run={runId:'r',threadId:'s',status:'running',messages:full.slice(-100),state:{},input:{messages:full.slice(-100)}};
  const client=new CommerceClient({url:'/commerce/ag-ui/run',fetch:async(url,init)=>{
    if(init.method==='POST')posts++;
    if(url.includes('/sessions?'))return json({sessions:[{id:'s',title:'运行中',updatedAt:1}]});
    if(url.includes('/sessions/'))return json({messages:full,run});
    if(url.includes('/events'))return sse('r',[[1,{type:'RUN_STARTED',threadId:'s',runId:'r'}],
      [2,{type:'MESSAGES_SNAPSHOT',messages:[...run.messages,{id:'final',role:'assistant',content:'完整结果'}]}],
      [3,{type:'RUN_FINISHED',threadId:'s',runId:'r'}]]);
    return json(run);
  }});
  await client.initialize();expect(posts).toBe(0);
  expect(client.getSnapshot().messages).toHaveLength(122);
  expect(client.getSnapshot().messages[0].content).toBe('消息0');
  expect(client.getSnapshot().messages.at(-1)?.content).toBe('完整结果');
});

describe("持久运行恢复（使用官方 AG-UI SDK）", () => {
  it("网络断流按cursor重连，丢弃重复和乱序帧，不重新POST模型", async () => {
    let body: any, posts = 0, gets = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async (url, init) => {
      if (init.method === "POST") {
        posts++; body = JSON.parse(String(init.body));
        return sse(body.runId, [[1, { type: "RUN_STARTED", threadId: body.threadId, runId: body.runId }],
          [2, { type: "TEXT_MESSAGE_START", messageId: "a", role: "assistant" }],
          [3, { type: "TEXT_MESSAGE_CONTENT", messageId: "a", delta: "甲" }]]);
      }
      gets++;
      expect(String(url)).toContain("after=3");
      if (gets === 1) return sse(body.runId, [[3, { type: "TEXT_MESSAGE_CONTENT", messageId: "a", delta: "甲" }],
        [5, { type: "TEXT_MESSAGE_END", messageId: "a" }]]);
      return sse(body.runId, [[4, { type: "TEXT_MESSAGE_CONTENT", messageId: "a", delta: "乙" }],
        [5, { type: "TEXT_MESSAGE_END", messageId: "a" }],
        [6, { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }]]);
    }});
    await client.submit("背包");
    expect(posts).toBe(1); expect(gets).toBe(2);
    expect(client.getSnapshot().messages.at(-1)?.content).toBe("甲乙");
    expect(client.getSnapshot()).toMatchObject({ status: "idle", recoverableRunId: null });
  });

  it("切页detach只断订阅，刷新后从服务端恢复运行和历史", async () => {
    const storage = store();
    let body: any, posts = 0, cancels = 0, complete = false;
    const fetch = async (url: string, init: RequestInit) => {
      if (url.includes("/cancel")) { cancels++; return json({}); }
      if (init.method === "POST") {
        posts++; body = JSON.parse(String(init.body));
        return new Response(new ReadableStream({ start(controller) {
          controller.enqueue(new TextEncoder().encode(`id: ${body.runId}:1\ndata: ${JSON.stringify({ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId })}\n\n`));
          init.signal?.addEventListener("abort", () => controller.error(new DOMException("断开订阅", "AbortError")), { once: true });
        }}), { headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.includes("/events")) {
        complete = true;
        return sse(body.runId, [[1, { type: "RUN_STARTED", threadId: body.threadId, runId: body.runId }],
          [2, { type: "MESSAGES_SNAPSHOT", messages: [...body.messages, { id: "final", role: "assistant", content: "恢复后的完整结果" }] }],
          [3, { type: "STATE_SNAPSHOT", snapshot: { products: [], searchCompleted: true, progress: [] } }],
          [4, { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }]]);
      }
      const run = { runId: body.runId, threadId: body.threadId, status: complete ? "completed" : "running", input: body,
        messages: body.messages, state: { products: [], searchCompleted: false } };
      if (url.includes("/sessions?")) return json({ sessions: [{ id: body.threadId, title: "服务端历史", updatedAt: Date.now() }] });
      if (url.includes("/sessions/")) return json({ id: body.threadId, run });
      return json(run);
    };
    const first = new CommerceClient({ url: "/commerce/ag-ui/run", storage, fetch });
    const pending = first.submit("恢复测试");
    await waitUntil(() => !!body);
    first.detach();
    await pending;
    expect(cancels).toBe(0);
    const restored = new CommerceClient({ url: "/commerce/ag-ui/run", storage, fetch });
    await restored.initialize();
    expect(posts).toBe(1);
    expect(restored.getSnapshot()).toMatchObject({ status: "idle", searchCompleted: true, recoverableRunId: null });
    expect(restored.getSnapshot().messages.at(-1)?.content).toBe("恢复后的完整结果");
    expect(restored.getSnapshot().history[0].source).toBe("server");
  });

  it("明确停止调用cancel API，带身份头；断网失败不能声称已停止", async () => {
    let body: any, cancels = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", buyerId: "verified-buyer", accessToken: "test-token",
      fetch: async (url, init) => {
        expect(new Headers(init.headers).get("Authorization")).toBe("Bearer test-token");
        if (String(url).includes("/cancel")) {
          cancels++; throw new Error("网络断开");
        }
        body = JSON.parse(String(init.body));
        return new Response(new ReadableStream({ start(controller) {
          init.signal?.addEventListener("abort", () => controller.error(new DOMException("abort", "AbortError")), { once: true });
        }}), { headers: { "Content-Type": "text/event-stream" } });
      }});
    const pending = client.submit("背包");
    await waitUntil(() => !!body);
    client.stop();
    await pending;
    await waitUntil(() => client.getSnapshot().status === "error");
    expect(cancels).toBe(1);
    expect(client.getSnapshot().error).toContain("停止请求尚未确认");
    expect(client.getSnapshot().recoverableRunId).toBe(body.runId);
    expect(body.forwardedProps.buyerId).toBe("verified-buyer");
  });

  it("服务重启中断态从历史读取，不伪造继续执行", async () => {
    const values = store();
    values.setItem("shoppilot.buyer", "b1");
    values.setItem("shoppilot.agui.active-session", "s1");
    values.setItem("shoppilot.agui.sessions.v1", JSON.stringify([{ id: "s1", title: "旧记录", updatedAt: 1, messages: [{ id: "u", role: "user", content: "查询" }], products: [], searchCompleted: false, runId: "r1" }]));
    let posts = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", storage: values, buyerId:"b1", fetch: async (url, init) => {
      if (init.method === "POST") posts++;
      if (String(url).includes("/sessions?")) return json({ sessions: [{ id: "s1", title: "服务端记录", updatedAt: 2 }] });
      return json({ run: { runId: "r1", threadId: "s1", status: "interrupted", messages: [{ id: "a", role: "assistant", content: "已保存的部分结果" }], state: {} } });
    }});
    await client.initialize();
    expect(posts).toBe(0);
    expect(client.getSnapshot()).toMatchObject({ status: "error", recoverableRunId: null });
    expect(client.getSnapshot().messages[0].content).toBe("已保存的部分结果");
  });
});

describe("刷新时以服务端记录恢复，缓存仅用于加速", () => {
  const historyFetch = async (url: string) => url.includes("/sessions?")
    ? json({sessions:[{id:"older",title:"旧对话",updatedAt:1},{id:"saved",title:"已保存",updatedAt:2}]})
    : json({run:{runId:"r",threadId:"saved",status:"completed",messages:[{id:"a",role:"assistant",content:"完整的已保存对话"}],state:{}}});

  it.each([null, "{坏缓存", "[]"])("正文缓存为 %s 时仍按当前会话恢复", async (cache) => {
    const storage=store();
    storage.setItem("shoppilot.buyer","b1");
    storage.setItem("shoppilot.agui.active-session","saved");
    if(cache!==null) storage.setItem("shoppilot.agui.sessions.v1",cache);
    const client=new CommerceClient({url:"/commerce/ag-ui/run",storage,buyerId:"b1",fetch:historyFetch});
    expect(client.getSnapshot().sessionId).toBe("saved");
    await client.initialize();
    expect(client.getSnapshot().messages[0].content).toBe("完整的已保存对话");
  });

  it("缺少当前会话指针时按服务端时间恢复最新记录",async()=>{
    const storage=store();
    const client=new CommerceClient({url:"/commerce/ag-ui/run",storage,buyerId:"b1",fetch:historyFetch});
    expect(storage.getItem("shoppilot.buyer")).toBe("b1");
    await client.initialize();
    expect(client.getSnapshot().sessionId).toBe("saved");
    expect(storage.getItem("shoppilot.agui.active-session")).toBe("saved");
  });

  it("空白草稿刷新后恢复买家最近持久会话",async()=>{
    const storage=store();
    const first=new CommerceClient({url:"/commerce/ag-ui/run",storage,buyerId:"b1",fetch:historyFetch});
    await first.initialize(); first.reset();
    const draft=first.getSnapshot().sessionId;
    const restored=new CommerceClient({url:"/commerce/ag-ui/run",storage,buyerId:"b1",fetch:historyFetch});
    await restored.initialize();
    expect(restored.getSnapshot().sessionId).not.toBe(draft);
    expect(restored.getSnapshot().sessionId).toBe("saved");
    expect(restored.getSnapshot().messages[0].content).toBe("完整的已保存对话");
  });

  it("买家切换时不读取上一个买家的本机记录和会话指针",async()=>{
    const storage=store();
    storage.setItem("shoppilot.buyer","old-buyer");
    storage.setItem("shoppilot.agui.active-session","private-old");
    storage.setItem("shoppilot.agui.sessions.v1",JSON.stringify([{id:"private-old",title:"私有",updatedAt:1,messages:[{id:"a",role:"user",content:"私有内容"}]}]));
    const client=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"new-buyer",storage,fetch:async url=>{
      expect(url).toContain("buyer_id=new-buyer");return json({sessions:[]});
    }});
    await client.initialize();
    expect(client.getSnapshot().messages).toEqual([]);
    expect(client.getSnapshot().history).toEqual([]);
    expect(client.getSnapshot().sessionId).not.toBe("private-old");
  });
});

it("原生记忆审批通过 SDK resume 回传，不重发普通文本执行", async () => {
  const posts: any[] = [];
  const client = new CommerceClient({url:'/commerce/ag-ui/run',fetch:async (_url,init)=>{
    const request=JSON.parse(String(init.body));posts.push(request);
    const approval={id:'reply:call',tool:'remember_preference_tool',label:'保存购物偏好',arguments:'{"statement":"喜欢裙子"}'};
    return sse(request.runId,[[1,{type:'RUN_STARTED',threadId:request.threadId,runId:request.runId}],
      [2,{type:'STATE_SNAPSHOT',snapshot:{toolApprovals:posts.length===1?[approval]:[],products:[],progress:[]}}],
      [3,{type:'RUN_FINISHED',threadId:request.threadId,runId:request.runId,outcome:posts.length===1?{type:'interrupt',interrupts:[{id:approval.id,reason:'tool_confirmation'}]}:{type:'success'}}]]);
  }});
  await client.submit('记住我喜欢裙子');
  expect(client.getSnapshot().error).toBeNull();
  expect(client.getSnapshot().toolApprovals).toHaveLength(1);
  await client.resolveToolApproval('reply:call',true);
  expect(posts[1].resume).toEqual([{interruptId:'reply:call',status:'resolved',payload:{approved:true}}]);
  expect(client.getSnapshot().toolApprovals).toEqual([]);
});

it("默认固定 buyer-demo，忽略随机缓存身份并从数据库恢复",async()=>{
 const storage=store();storage.setItem("shoppilot.buyer","random-old-id");storage.setItem("shoppilot.agui.active-session","empty-draft");
 const requested:string[]=[];
 const fetch=async(url:string)=>{requested.push(url);return url.includes("/sessions?") ? json({sessions:[{id:"pao-history",title:"数据库历史",updatedAt:1}]}) : json({run:{runId:"r",threadId:"pao-history",status:"completed",messages:[{id:"u",role:"user",content:"数据库中的对话"}],state:{}}});};
 const first=new CommerceClient({url:"/commerce/ag-ui/run",storage,fetch});await first.initialize();
 expect(storage.getItem("shoppilot.buyer")).toBe("buyer-demo");expect(requested.every(url=>url.includes("buyer_id=buyer-demo"))).toBe(true);
 expect(first.getSnapshot().messages[0].content).toBe("数据库中的对话");
 const withoutCache=new CommerceClient({url:"/commerce/ag-ui/run",fetch});await withoutCache.initialize();
 expect(withoutCache.getSnapshot().sessionId).toBe("pao-history");
});

it.each([false,true])("版本过期保留旧历史并仅在新会话重试一次（再次过期=%s）",async(repeat)=>{
 const posts:any[]=[];
 const client=new CommerceClient({url:'/commerce/ag-ui/run',fetch:async(_url,init)=>{
  const body=JSON.parse(String(init.body));posts.push(body);
  return sse(body.runId,[[1,{type:'RUN_STARTED',threadId:body.threadId,runId:body.runId}],
   [2,{type:'STATE_SNAPSHOT',snapshot:{products:[],resumeDestination:'CN'}}],
   [3, posts.length===1 || repeat ? {type:'RUN_ERROR',code:'SESSION_VERSION_CHANGED',message:'选购环境已更新，旧记录仍保留'} : {type:'RUN_FINISHED',threadId:body.threadId,runId:body.runId}]]);
 }});
 const query='请核对 product_id=P1003，sku_id=P1003-S1 的库存与到手价';
 await client.submit(query);
 expect(posts).toHaveLength(2);
 expect(posts[0].threadId).not.toBe(posts[1].threadId);
 expect(posts[1].messages).toHaveLength(1);
 expect(posts[1].messages[0].content).toBe(query+'\n收货国家：CN。');
 expect(posts[1].forwardedProps.buyerId).toBe('buyer-demo');
 expect(client.getSnapshot().history.some(item=>item.id===posts[0].threadId)).toBe(true);
 expect(client.getSnapshot().status).toBe(repeat?'error':'idle');
});

 it("首轮模型运行早于会话落库时，确认列表404不会成为交易报错",async()=>{
  let finish:()=>void=()=>{};
  const client=new CommerceClient({url:'/commerce/ag-ui/run',fetch:async(_url,init)=>{
   if(init.method!=="POST") return new Response(JSON.stringify({detail:"会话不存在"}),{status:404,headers:{"Content-Type":"application/json"}});
   const body=JSON.parse(String(init.body));
   return new Response(new ReadableStream({start(controller){
    controller.enqueue(new TextEncoder().encode(`id: ${body.runId}:1\ndata: ${JSON.stringify({type:"RUN_STARTED",threadId:body.threadId,runId:body.runId})}\n\n`));
    finish=()=>{controller.enqueue(new TextEncoder().encode(`id: ${body.runId}:2\ndata: ${JSON.stringify({type:"RUN_FINISHED",threadId:body.threadId,runId:body.runId})}\n\n`));controller.close();};
   }}),{headers:{"Content-Type":"text/event-stream"}});
  }});
  const pending=client.submit("核对背包库存");
  await client.refreshConfirmations();
  expect(client.getSnapshot().confirmationError).toBeNull();
  finish();await pending;
 });


it("自动化来源由服务端恢复，刷新及读取失败保留已知标记，不猜标题或ID", async () => {
  let unavailable = false;
  const fetch = async (url: string) => {
    if (url.includes('/session-origins')) {
      if (unavailable) throw new Error('暂时离线');
      return json({ session_ids: ['actual-automation'] });
    }
    if (url.includes('/sessions?')) return json({ sessions: [
      { id: 'actual-automation', title: '每周清单', updatedAt: 2 },
      { id: 'automation-manual', title: '自动化只是我起的名字', updatedAt: 1 },
    ] });
    return json({});
  };
  const client = new CommerceClient({ url: '/commerce/ag-ui/run', fetch });
  await client.initialize();
  expect(client.getSnapshot().history[0].origin).toBe('automation');
  expect(client.getSnapshot().history[1].origin).toBeUndefined();
  const restored = new CommerceClient({ url: '/commerce/ag-ui/run', fetch });
  await restored.initialize();
  expect(restored.getSnapshot().history[0].origin).toBe('automation');
  unavailable = true;
  await restored.initialize();
  expect(restored.getSnapshot().history[0].origin).toBe('automation');
});


it("自动化新增会话刷新目录后可以打开，不覆盖当前对话或重启模型", async () => {
  let added = false;
  const reads: string[] = [];
  const client = new CommerceClient({ url: '/commerce/ag-ui/run', fetch: async (url, init) => {
    expect(init.method).not.toBe('POST'); reads.push(String(url));
    if (url.includes('/session-origins')) return json({ session_ids: added ? ['new-auto'] : [] });
    if (url.includes('/sessions?')) return json({ sessions: [
      { id:'manual', title:'当前手动会话', updatedAt:1 },
      ...(added ? [{ id:'new-auto', title:'新自动化结果', updatedAt:2 }] : []),
    ] });
    const id = url.includes('/sessions/new-auto') ? 'new-auto' : 'manual';
    return json({ run:{runId:id,threadId:id,status:'completed',state:{}}, messages:[{id,role:'assistant',content:id}] });
  }});
  await client.initialize();
  expect(client.getSnapshot().sessionId).toBe('manual');
  added = true;
  const oldReads=reads.filter(url=>url.includes('/sessions/manual')).length;
  await client.refreshHistory();
  expect(client.getSnapshot().sessionId).toBe('manual');
  expect(reads.filter(url=>url.includes('/sessions/manual')).length).toBe(oldReads);
  client.setSession('new-auto');
  await waitUntil(()=>client.getSnapshot().messages[0]?.content==='new-auto');
  expect(client.getSnapshot().history.find(item=>item.id==='new-auto')?.origin).toBe('automation');
});
