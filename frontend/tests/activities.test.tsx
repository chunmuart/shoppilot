// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";
import { readActivities, upsertActivities, replaceRunActivities } from "../src/lib/activities";
import ExecutionTrace from "../src/components/ExecutionTrace";
import type { ExecutionActivity } from "../src/types";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const activity = (patch: Partial<ExecutionActivity> = {}): ExecutionActivity => ({
  id: "r:tool:1", runId: "r", kind: "tool", label: "检索商品", toolCallId: "r:tool:1",
  toolName: "product_search_tool", status: "preparing", sequence: 1, startedAt: 1000, finishedAt: null, ...patch,
});
const json = (value: unknown) => new Response(JSON.stringify(value), {headers: {"Content-Type": "application/json"}});
const stream = (events: unknown[]) => new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""), {headers: {"Content-Type": "text/event-stream"}});

it("按运行隔离展示字段，未知参数不会混入界面或保留原始结果", () => {
  const rows = readActivities([{...activity(), arguments: {phone: "secret"}, rawResult: "secret"}, activity({runId: "other"}), activity({status: "fake" as any})], "r");
  expect(rows).toHaveLength(1);
  expect(JSON.stringify(rows)).not.toContain("secret");
  expect(readActivities([activity({startedAt: NaN})])[0].startedAt).toBeNull();
});

it("同名并发工具保持独立；重放替换同一调用；本轮快照不丢前一轮", () => {
  const a = activity(), b = activity({id: "r:tool:2", toolCallId: "r:tool:2", sequence: 2});
  const old = activity({id: "old:1", runId: "old", status: "completed"});
  const next = upsertActivities([old, a, b], [activity({status: "running"})]);
  expect(next).toHaveLength(3); expect(next[1].status).toBe("running");
  expect(next[2]).toBe(b);
  const restored = replaceRunActivities(next, [activity({status: "completed"})], "r");
  expect(restored.map(row => row.runId)).toEqual(["old", "r"]);
});

it("实际 SDK 消费思考增量投影，工具 END 不伪报成功，最终快照不重复，消息保留轮次", async () => {
  const snapshots: string[] = [], storage = new Map<string, string>();
  let run = "";
  const client = new CommerceClient({url: "/commerce/ag-ui/run", storage: {getItem: k => storage.get(k) ?? null, setItem: (k, v) => {storage.set(k, v);}}, fetch: async (_url, init) => {
    const body = JSON.parse(String(init.body)); run = body.runId;
    const tool = activity({runId: run, id: run + ":tool:1", toolCallId: run + ":tool:1"});
    const thought = activity({runId: run, id: run + ":think:1", kind: "thinking", label: "思考过程", status: "running", content: "先核对预算", sequence: 0});
    return stream([
      {type: "RUN_STARTED", threadId: body.threadId, runId: run},
      {type: "STATE_SNAPSHOT", snapshot: {activities: [tool]}},
      {type: "CUSTOM", name: "activity.upsert", value: thought},
      {type: "CUSTOM", name: "activity.upsert", value: {...thought, content: "先核对预算，再比较重量"}},
      {type: "TOOL_CALL_START", toolCallId: tool.id, toolCallName: "product_search_tool"},
      {type: "TOOL_CALL_ARGS", toolCallId: tool.id, delta: "{}"},
      {type: "TOOL_CALL_END", toolCallId: tool.id},
      {type: "TEXT_MESSAGE_START", messageId: "answer", role: "assistant"},
      {type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "已整理"},
      {type: "TEXT_MESSAGE_END", messageId: "answer"},
      {type: "STATE_SNAPSHOT", snapshot: {activities: [{...thought, status: "completed", content: "先核对预算，再比较重量"}, {...tool, status: "completed"}]}},
      {type: "RUN_FINISHED", threadId: body.threadId, runId: run},
    ]);
  }});
  client.subscribe(() => snapshots.push(JSON.stringify(client.getSnapshot().activities)));
  await client.submit("预算300元背包");
  const result = client.getSnapshot();
  expect(result.activities).toHaveLength(2);
  expect(result.activities!.find(row => row.kind === "thinking")?.content).toBe("先核对预算，再比较重量");
  expect(snapshots.some(s => s.includes('"status":"preparing"'))).toBe(true);
  expect(result.messages.every(message => message.runId === run)).toBe(true);
  expect([...storage.values()].join("")).not.toContain("先核对预算");
});

it("服务端跨轮记录刷新恢复；恢复下一轮不会清空以前的工具与Think", async () => {
  const prior = activity({runId: "prior", id: "prior:1", status: "completed"});
  const client = new CommerceClient({url: "/commerce/ag-ui/run", fetch: async (url, init) => {
    if (init.method === "POST") {
      const b = JSON.parse(String(init.body));
      return stream([{type: "RUN_STARTED", threadId: b.threadId, runId: b.runId},
        {type: "STATE_SNAPSHOT", snapshot: {activities: [activity({runId: b.runId, id: b.runId + ":1", status: "completed"})]}},
        {type: "RUN_FINISHED", threadId: b.threadId, runId: b.runId}]);
    }
    if (url.includes("/sessions?")) return json({sessions: [{id: "s", title: "选购", updatedAt: 1}]});
    return json({messages: [{id: "u", role: "user", content: "第一轮", runId: "prior"}], activities: [prior],
      run: {runId: "prior", threadId: "s", status: "completed", messages: [], state: {activities: [prior]}}});
  }});
  await client.initialize();
  expect(client.getSnapshot().activities?.[0].id).toBe("prior:1");
  await client.submit("继续");
  expect(client.getSnapshot().activities).toHaveLength(2);
  expect(client.getSnapshot().activities?.[0].runId).toBe("prior");
  expect(client.getSnapshot().messages[0].runId).toBe("prior");
});

it("Think 和工具详情可展开，未知耗时不显示，模型文本不作为HTML执行", async () => {
  const host = document.createElement("div"), root = createRoot(host); document.body.append(host);
  try {
    await act(async () => root.render(<ExecutionTrace activities={[
      activity({kind: "thinking", content: "<img src=x onerror=alert(1)>", status: "completed", startedAt: null}),
      activity({id: "r:2", sequence: 2, status: "waiting", inputSummary: "保存购物偏好，等待批准"}),
    ]}/>));
    expect(host.textContent).toContain("Think · 思考过程");
    expect(host.textContent).toContain("等待确认");
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector(".execution-duration")).toBeNull();
    expect(host.querySelectorAll(".execution-row details")).toHaveLength(2);
    const thoughtDetail = host.querySelector<HTMLDetailsElement>(".execution-row details")!;
    expect(thoughtDetail.open).toBe(false);
    await act(async () => {
      thoughtDetail.querySelector("summary")!.click();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    expect(thoughtDetail.open).toBe(true);
  } finally {await act(async () => root.unmount());host.remove();}
});

it.each(["running", "completed"])("审批后断线恢复（服务端 %s）重新核对旧轮等待状态，不重复提交", async status => {
  const waiting = activity({runId: "prior", id: "prior:approval", status: "waiting"});
  const approved = {...waiting, status: "approved"};
  const resumed = activity({runId: "resumed", id: "resumed:tool", status: "completed"});
  const run = {runId: "resumed", threadId: "s", status, messages: [], state: {activities: [resumed]}, input: {messages: []}};
  let loads = 0, posts = 0;
  const client = new CommerceClient({url: "/commerce/ag-ui/run", fetch: async (url, init) => {
    if (init.method === "POST") {posts++; throw new Error("不应重复执行模型");}
    if (url.includes("/sessions?")) return json({sessions: [{id: "s", title: "审批恢复", updatedAt: 1}]});
    if (url.includes("/sessions/s")) {
      loads++;
      return json({messages: [], activities: loads === 1 ? [waiting, resumed] : [approved, resumed], run: {...run, status: loads === 1 ? "running" : "completed"}});
    }
    if (url.includes("/events")) return new Response([
      {type: "RUN_STARTED", threadId: "s", runId: "resumed"},
      {type: "STATE_SNAPSHOT", snapshot: {activities: [resumed]}},
      {type: "RUN_FINISHED", threadId: "s", runId: "resumed"},
    ].map((event, index) => `id: resumed:${index + 1}\ndata: ${JSON.stringify(event)}\n\n`).join(""), {headers: {"Content-Type": "text/event-stream"}});
    return json(run);
  }});
  await client.initialize();
  expect(posts).toBe(0);
  expect(loads).toBe(2);
  expect(client.getSnapshot().activities?.find(row => row.id === waiting.id)?.status).toBe("approved");
  expect(client.getSnapshot().activities?.find(row => row.id === resumed.id)?.status).toBe("completed");
});
