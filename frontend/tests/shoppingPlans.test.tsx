import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import ShoppingPlans, { SkillRunStatus } from "../src/components/ShoppingPlans";
import EventTimeline from "../src/components/EventTimeline";
import { readPublishedSkills, readSkillUsages, skillQueryDraft, submitSkillQuery } from "../src/lib/skills";
import { CommerceClient } from "../src/lib/commerceClient";
import type { PublishedSkill, SelectedSkill } from "../src/types";

const skill: PublishedSkill = { id: "weekend-travel", version: "v1", title: "周末轻装出游", description: "从行程和预算出发，比较轻便出行装备。",
  scope: "shopping", content_hash: "a".repeat(64), expires_at: null };
const catalog = (skills: unknown[]) => ({ capability_digest: "c".repeat(64), skills });
const sse = (events: unknown[]) => new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), { headers: { "Content-Type": "text/event-stream" } });

it("空库与读取失败分开显示，不生成演示方案或使用成功", () => {
  const props = { skills: [], status: "ready" as const, error: null, selected: null, onSelect: () => {}, onRefresh: () => {} };
  const empty = renderToStaticMarkup(<ShoppingPlans {...props} />);
  expect(empty).toContain("选购方案筹备中，可直接描述需求");
  expect(empty).not.toContain("已读取");
  expect(empty).not.toContain("plan-card ");
  const error = renderToStaticMarkup(<ShoppingPlans {...props} status="error" error="方案服务暂时不可用" />);
  expect(error).toContain("方案服务暂时不可用");
  expect(error).not.toContain("筹备中");
});

it("已选择是独立草稿状态，保留用户输入，换方案和取消可还原", () => {
  const first = skillQueryDraft("预算300元，寄到中国");
  const second = skillQueryDraft(first);
  expect(second).toBe("预算300元，寄到中国");
  expect(second).not.toContain(skill.id);
  const markup = renderToStaticMarkup(<ShoppingPlans skills={[skill]} status="ready" error={null} selected={skill} onSelect={() => {}} onRefresh={() => {}} />);
  expect(markup).toContain('aria-pressed="true"');
  expect(markup).toContain("已选择");
  expect(markup).not.toContain("已读取");
});

it("只接受有效发布清单，过滤过期条目和无效字段", () => {
  expect(readPublishedSkills(catalog([skill, skill, { ...skill, id: "old", expires_at: "2000-01-01T00:00:00Z" }, { ...skill, id: "bad", content_hash: "invalid" }]))).toEqual([skill]);
  expect(() => readPublishedSkills({ skills: [] })).toThrow();
  expect(() => readPublishedSkills(catalog([{ id: "broken" }]))).toThrow();
  expect(readSkillUsages([{ toolCallId: "1", status: "used", title: "看似成功" }])).toEqual([]);
});

it("只有有效成功投影可显示已读取；终止时不继续显示读取中", () => {
  const used = { toolCallId: "1", status: "used" as const, id: skill.id, title: skill.title, version: skill.version, contentHash: skill.content_hash };
  const usages = readSkillUsages([used]);
  const markup = renderToStaticMarkup(<EventTimeline events={[]} skillUsages={usages} />);
  expect(markup).toContain("周末轻装出游");
  expect(markup).toContain("v1");
  expect(markup).toContain("工具成功返回");
  const stopped = renderToStaticMarkup(<SkillRunStatus usages={[{ ...used, status: "reading" }]} running={false} />);
  expect(stopped).toContain("方案读取未完成");
  expect(stopped).not.toContain("正在读取");
  expect(stopped).not.toContain("已读取");
});

describe("真实 AG-UI SDK 与买家方案 API 契约", () => {
  it.each([
    { id: "../unpublished", version: "v1", contentHash: "a".repeat(64) },
    { id: "weekend-travel", version: "v1", contentHash: "invalid" },
    { id: "weekend-travel", version: "v1", contentHash: "a".repeat(64), body: "额外指令不能进入选择合同" },
  ])("无效或额外选择字段不能静默降级为普通请求：%j", async (selection) => {
    let calls = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async () => { calls++; return Response.json({}); } });
    await client.submit("通勤耳机", selection);
    expect(calls).toBe(0);
    expect(client.getSnapshot().error).toContain("所选方案信息无效");
  });

  it("服务端确定读取失败保持失败状态，不自动重发普通模型请求或显示已读取", async () => {
    let posts = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async (_, init) => {
      posts++;
      const body = JSON.parse(String(init.body));
      return sse([{ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId },
        { type: "STATE_SNAPSHOT", snapshot: { skillUsages: [{ toolCallId: "selected-1", source: "server_preload", status: "error", title: skill.title, version: skill.version }] } },
        { type: "RUN_ERROR", message: "选购方案版本已更新，请刷新后重新选择。", code: "SELECTED_SKILL_UNAVAILABLE" }]);
    } });
    await client.submit("周末出游", { id: skill.id, version: skill.version, contentHash: skill.content_hash });
    expect(posts).toBe(1);
    expect(client.getSnapshot()).toMatchObject({ status: "error", recoverableRunId: null });
    expect(client.getSnapshot().error).toContain("版本已更新");
    expect(client.getSnapshot().skillUsages).toMatchObject([{ status: "error", source: "server_preload" }]);
    expect(renderToStaticMarkup(<SkillRunStatus usages={client.getSnapshot().skillUsages} running={false} />)).not.toContain("已读取");
  });

  it("编辑期间方案到期：首发不产生模型请求，刷新并保留正文，再次发送才提交正文", async () => {
    const expires = Date.now() + 60_000;
    const expiring = { ...skill, expires_at: new Date(expires).toISOString() };
    let refreshes = 0;
    const sentQueries: string[] = [];
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async (url, init) => {
      if (String(url).includes("/skills?")) {
        refreshes++;
        return Response.json(catalog(refreshes === 1 ? [expiring] : []));
      }
      const body = JSON.parse(String(init.body));
      sentQueries.push(body.messages.at(-1).content);
      return sse([{ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId },
        { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }]);
    } });
    await client.refreshSkills();
    let selected: PublishedSkill | null = client.getSnapshot().skills[0];
    const request = "预算300元，寄到中国。\n请保留侧袋和重量的比较。";
    let draft = skillQueryDraft(request);
    let refresh: Promise<void> | undefined;
    let submission: Promise<void> | undefined;
    const send = (query: string, selection?: SelectedSkill) => { submission = client.submit(query, selection); };
    const recover = (query: string) => {
      draft = query;
      selected = null;
      refresh = client.refreshSkills();
    };
    // 恰好到 expires_at 也失效；经过真实 SDK 请求边界验证没有自动改写后发送。
    expect(submitSkillQuery(draft, selected, send, recover, expires)).toBe(false);
    await refresh;
    expect(sentQueries).toEqual([]);
    expect(submission).toBeUndefined();
    expect(draft).toBe(request);
    expect(selected).toBeNull();
    expect(refreshes).toBe(2);
    expect(client.getSnapshot().skills).toEqual([]);
    expect(submitSkillQuery(draft, selected, send, recover, expires + 1)).toBe(true);
    await submission;
    expect(sentQueries).toEqual([request]);
    expect(sentQueries[0]).not.toContain(expiring.id);
  });

  it("仍有效方案正常发送，过期草稿不拦截独立自然语言提问", async () => {
    const expires = Date.now() + 60_000;
    const expiring = { ...skill, expires_at: new Date(expires).toISOString() };
    const queries: string[] = [];
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async (_, init) => {
      const body = JSON.parse(String(init.body));queries.push(body.messages.at(-1).content);
      return sse([{ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId },
        { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }]);
    } });
    let submission: Promise<void> | undefined;
    const send = (query: string, selection?: SelectedSkill) => { submission = client.submit(query, selection); };
    const unexpectedExpiry = () => { throw new Error("不应阻断这次明确提问"); };
    const draft = skillQueryDraft("短途旅行");
    submitSkillQuery(draft, expiring, send, unexpectedExpiry, expires - 1);
    await submission;
    submitSkillQuery("直接比较这两款耳机", null, send, unexpectedExpiry, expires + 1);
    await submission;
    expect(queries).toEqual([draft, "直接比较这两款耳机"]);
  });

  it("读取方案复用同一买家Bearer，选择意图不冒充工具调用成功", async () => {
    let query = "";
    let selection: unknown;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", buyerId: "test-buyer", accessToken: "local-test-token",
      fetch: async (url, init) => {
        if (String(url).includes("/skills?")) {
          expect(String(url)).toBe("/commerce/skills?buyer_id=test-buyer");
          expect(new Headers(init.headers).get("Authorization")).toBe("Bearer local-test-token");
          return Response.json(catalog([skill]));
        }
        const body = JSON.parse(String(init.body)); query = body.messages.at(-1).content; selection = body.forwardedProps.selectedSkill;
        return sse([{ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId },
          { type: "TOOL_CALL_START", toolCallId: "skill-call", toolCallName: "load_agent_skill_tool" },
          { type: "TOOL_CALL_END", toolCallId: "skill-call" },
          { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }]);
      } });
    await client.refreshSkills();
    expect(client.getSnapshot().skills).toEqual([skill]);
    await client.submit("周末去旅行", { id: skill.id, version: skill.version, contentHash: skill.content_hash });
    expect(query).toBe("周末去旅行");
    expect(selection).toEqual({ id: skill.id, version: skill.version, contentHash: skill.content_hash });
    expect(client.getSnapshot().skillUsages).toEqual([]);
  });

  it("SDK消费实际状态投影，后续快照替换而非沿用旧成功", async () => {
    let calls = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async (_, init) => {
      const body = JSON.parse(String(init.body));calls++;
      return sse([{ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId },
        { type: "STATE_SNAPSHOT", snapshot: { products: [], skillUsages: calls === 1 ? [{ toolCallId: "skill-call", id: skill.id, title: skill.title, version: skill.version, contentHash: skill.content_hash, status: "used" }] : [] } },
        { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }]);
    } });
    await client.submit("按方案整理");
    expect(client.getSnapshot().skillUsages[0]).toMatchObject({ title: skill.title, version: "v1", status: "used" });
    await client.submit("现在直接找耳机");
    expect(client.getSnapshot().skillUsages).toEqual([]);
  });

  it("503和409不会伪装为真实空库，刷新响应有顺序保护", async () => {
    let finishFirst: ((response: Response) => void) | undefined;
    let calls = 0;
    const client = new CommerceClient({ url: "/commerce/ag-ui/run", fetch: async () => {
      calls++;
      if (calls === 1) return new Promise<Response>((resolve) => { finishFirst = resolve; });
      return Response.json({}, { status: 409 });
    } });
    const first = client.refreshSkills();
    await client.refreshSkills();
    finishFirst!(Response.json(catalog([skill])));await first;
    expect(client.getSnapshot().skillsStatus).toBe("error");
    expect(client.getSnapshot().skillsError).toContain("刚刚更新");
    expect(client.getSnapshot().skills).toEqual([]);
  });
});

it("同一版本预读和模型读取的成功提示合并，但失败状态仍可见", () => {
  const used = { toolCallId: "preload", source: "server_preload" as const, status: "used" as const,
    id: skill.id, version: skill.version, title: skill.title, contentHash: skill.content_hash };
  const markup = renderToStaticMarkup(<SkillRunStatus running={false} usages={[used, {...used, toolCallId: "tool"},
    {...used, toolCallId: "failed", status: "error"}]} />);
  expect(markup.match(/已读取选购方案/g)).toHaveLength(1);
  expect(markup).toContain("选购方案未能读取");
});
