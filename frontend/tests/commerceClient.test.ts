import { describe, expect, it } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";

const product = {
  product_id: "P1003",
  title: "旅行背包",
  price_major: 129,
  currency: "CNY",
  brand: "Wanderlite",
  category: "bag",
  origin_country: "CN",
  highlights: ["轻便"],
  skus: [],
  score: 0.8,
};
const sse = (events: unknown[]) =>
  new Response(
    events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
    { headers: { "Content-Type": "text/event-stream" } },
  );
type RequestBody = {
  threadId: string;
  runId: string;
  messages: Array<{ id: string; role: string; content: string }>;
};
const started = (body: RequestBody) => ({
  type: "RUN_STARTED",
  threadId: body.threadId,
  runId: body.runId,
});
const finished = (body: RequestBody) => ({
  type: "RUN_FINISHED",
  threadId: body.threadId,
  runId: body.runId,
});

describe("通过官方 SDK 消费真实 AG-UI 协议", () => {
  it("逐字内容被最终快照纠正，JSON Patch 更新商品，第二轮空结果清除旧卡", async () => {
    let calls = 0;
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run",
      fetch: async (_, init) => {
        const body: RequestBody = JSON.parse(String(init.body));
        calls++;
        if (calls === 2)
          return sse([
            started(body),
            {
              type: "STATE_SNAPSHOT",
              snapshot: { products: [], searchCompleted: true, progress: [] },
            },
            finished(body),
          ]);
        expect(body.messages.at(-1)?.content).toBe("找旅行背包");
        return sse([
          started(body),
          {
            type: "TEXT_MESSAGE_START",
            messageId: "assistant-1",
            role: "assistant",
          },
          {
            type: "TEXT_MESSAGE_CONTENT",
            messageId: "assistant-1",
            delta: "流式草稿",
          },
          { type: "TEXT_MESSAGE_END", messageId: "assistant-1" },
          {
            type: "STATE_SNAPSHOT",
            snapshot: { products: [], searchCompleted: false, progress: [] },
          },
          {
            type: "STATE_DELTA",
            delta: [
              { op: "replace", path: "/products", value: [product] },
              { op: "replace", path: "/searchCompleted", value: true },
            ],
          },
          {
            type: "MESSAGES_SNAPSHOT",
            messages: [
              ...body.messages,
              { id: "assistant-1", role: "assistant", content: "审核后的建议" },
            ],
          },
          finished(body),
        ]);
      },
    });
    await client.submit("找旅行背包");
    expect(client.getSnapshot()).toMatchObject({
      status: "idle",
      products: [product],
      searchCompleted: true,
    });
    expect(client.getSnapshot().messages.at(-1)?.content).toBe("审核后的建议");
    await client.submit("不存在的商品");
    expect(client.getSnapshot()).toMatchObject({
      products: [],
      searchCompleted: true,
      status: "idle",
    });
  });

  it("工具参数结束与工具结果分别显示，不把参数结束伪装成成功", async () => {
    const client = new CommerceClient({
      url: "/run",
      fetch: async (_, init) => {
        const body: RequestBody = JSON.parse(String(init.body));
        return sse([
          started(body),
          {
            type: "TOOL_CALL_START",
            toolCallId: "call-1",
            toolCallName: "product_search_tool",
          },
          {
            type: "TOOL_CALL_ARGS",
            toolCallId: "call-1",
            delta: '{"query":"背包"}',
          },
          { type: "TOOL_CALL_END", toolCallId: "call-1" },
          {
            type: "TOOL_CALL_RESULT",
            messageId: "tool-result-1",
            toolCallId: "call-1",
            content: '{"hits":[]}',
            role: "tool",
          },
          finished(body),
        ]);
      },
    });
    await client.submit("背包");
    expect(
      client
        .getSnapshot()
        .events.filter((event) => event.type.startsWith("TOOL_"))
        .map((event) => event.label),
    ).toEqual(["调用工具", "工具参数已就绪", "收到工具结果"]);
  });

  it("RUN_ERROR 和没有终止事件的断流都保留失败状态", async () => {
    for (const isRunError of [true, false]) {
      const client = new CommerceClient({
        url: "/run",
        fetch: async (_, init) => {
          const body: RequestBody = JSON.parse(String(init.body));
          return sse([
            started(body),
            ...(isRunError
              ? [
                  {
                    type: "RUN_ERROR",
                    message: "模型暂不可用",
                    code: "UPSTREAM_ERROR",
                  },
                ]
              : []),
          ]);
        },
      });
      await client.submit("背包");
      expect(client.getSnapshot().status).toBe("error");
      expect(client.getSnapshot().error).toBeTruthy();
    }
  });

  it("HTTP 错误显示可恢复提示，不把服务器错误页直接渲染给用户", async () => {
    const client = new CommerceClient({
      url: "/run",
      fetch: async () =>
        new Response("<html>internal traceback</html>", { status: 503 }),
    });
    await client.submit("背包");
    expect(client.getSnapshot()).toMatchObject({
      status: "error",
      error: "选购服务暂时不可用，请稍后重试。",
    });
  });

  it("取消会中断 HTTP，迟到结果不会污染新会话", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    let firstBody: RequestBody | undefined;
    let signal: AbortSignal | null | undefined;
    const client = new CommerceClient({
      url: "/run",
      fetch: async (_, init) => {
        signal = init.signal;
        firstBody = JSON.parse(String(init.body));
        return new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        });
      },
    });
    const pending = client.submit("背包");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(client.getSnapshot().status).toBe("running");
    client.stop();
    expect(signal?.aborted).toBe(true);
    expect(client.getSnapshot().status).toBe("stopped");
    const oldSession = client.getSnapshot().sessionId;
    client.reset();
    expect(client.getSnapshot().sessionId).not.toBe(oldSession);
    if (!firstBody || !resolveRequest) throw new Error("未发出请求");
    resolveRequest(sse([started(firstBody), finished(firstBody)]));
    await pending;
    expect(client.getSnapshot()).toMatchObject({
      status: "idle",
      messages: [],
      products: [],
    });
  });

  it("本机历史可恢复，并且存储受限不会阻断运行", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
    };
    const fetch = async (_: string, init: RequestInit) => {
      const body: RequestBody = JSON.parse(String(init.body));
      return sse([started(body), finished(body)]);
    };
    const first = new CommerceClient({ url: "/run", storage, fetch });
    await first.submit("第一次选购");
    const id = first.getSnapshot().sessionId;
    const second = new CommerceClient({ url: "/run", storage, fetch });
    second.setSession(id);
    expect(second.getSnapshot().messages[0].content).toBe("第一次选购");
    const restricted = new CommerceClient({
      url: "/run",
      fetch,
      storage: {
        getItem: () => {
          throw new Error("storage unavailable");
        },
        setItem: () => {
          throw new Error("quota");
        },
      },
    });
    await restricted.submit("正常运行");
    expect(restricted.getSnapshot().status).toBe("idle");
  });
});

it("没有浏览器缓存或对话记录时，按买家从服务端恢复选购表单会话",async()=>{
  const form={session_id:"draft-from-db",form_id:"form-a",created_at:Date.now(),revision:1};
  const client=new CommerceClient({url:"/commerce/ag-ui/run",fetch:async(url)=>{
    const body=String(url).includes("shopping-forms")?{form}:{sessions:[]};
    return new Response(JSON.stringify(body),{headers:{"Content-Type":"application/json"}});
  }});
  await client.initialize();
  expect(client.getSnapshot().sessionId).toBe("draft-from-db");
  expect(client.getSnapshot().shoppingForm).toEqual(form);
});

it("已保存的表单重复继续使用相同运行和消息 ID，不重复插入买家消息",async()=>{
  const sent:RequestBody[]=[];
  const client=new CommerceClient({url:"/run",fetch:async(_url,init)=>{
    const body=JSON.parse(String(init.body));sent.push(body);
    return sse([started(body),finished(body)]);
  }});
  const runId="form-run-"+"a".repeat(32);
  await client.submitForm("背包；单价300元",runId);
  await client.submitForm("背包；单价300元",runId);
  expect(sent).toHaveLength(2);
  expect(sent[0].runId).toBe(sent[1].runId);
  expect(sent[1].messages.filter(m=>m.id===runId+":user")).toHaveLength(1);
  expect(client.getSnapshot().messages.filter(m=>m.role==="user")).toHaveLength(1);
});

describe("自动化工作区的买家作用域", () => {
  it.each([
    ["/automations", "POST"],
    ["/automations/preview", "POST"],
    ["/automations/task-a", "PATCH"],
    ["/automations/task-a/runs", "POST"],
    ["/automation-runs/run-a/actions", "POST"],
  ])("%s 在请求正文附带当前买家，不能被表单覆盖", async (path, method) => {
    let requestBody: Record<string, unknown> = {};
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run", buyerId: "isolated-buyer",
      fetch: async (url, init) => {
        expect(String(url)).toContain("buyer_id=isolated-buyer");
        requestBody = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ok:true}));
      },
    });
    await client.workspaceRequest(path, method, {buyer_id:"another-buyer",request_id:"one"});
    expect(requestBody).toEqual({buyer_id:"isolated-buyer",request_id:"one"});
  });
});
