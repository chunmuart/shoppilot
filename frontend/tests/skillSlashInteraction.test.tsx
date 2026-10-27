// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import type { PublishedSkill } from "../src/types";

const plans: PublishedSkill[] = [
  { id: "weekend-travel", version: "v1", title: "周末轻装出游", description: "按行程、重量和预算比较旅行装备。", scope: "shopping", content_hash: "a".repeat(64), expires_at: null },
  { id: "daily-audio", version: "v2", title: "通勤声音方案", description: "比较通勤耳机的佩戴和续航。", scope: "shopping", content_hash: "b".repeat(64), expires_at: null },
];
let host: HTMLDivElement, root: Root;
let catalog: PublishedSkill[], requests: Array<Record<string, any>>, skillsStatus: number;
let clock: number | undefined;
const originalNow = Date.now;
const query = () => host.querySelector<HTMLTextAreaElement>("#query")!;
const options = () => [...host.querySelectorAll<HTMLButtonElement>('[role="option"]')];

beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  requests = []; catalog = plans; skillsStatus = 200; clock = undefined;
  vi.spyOn(Date, "now").mockImplementation(() => clock ?? originalNow());
  vi.stubGlobal("scrollTo", vi.fn());
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).includes("/skills?")) return Response.json({ capability_digest: "c".repeat(64), skills: catalog }, { status: skillsStatus });
    if (String(url).includes("/sessions?")) return Response.json({ sessions: [] });
    if (String(url).includes("/confirmations?")) return Response.json({ confirmations: [] });
    if (String(url).endsWith("/ag-ui/run") && init?.method === "POST") {
      const body = JSON.parse(String(init.body)); requests.push(body);
      const selected = body.forwardedProps.selectedSkill;
      const chosen = plans.find((plan) => plan.id === selected?.id);
      const events = [{ type: "RUN_STARTED", threadId: body.threadId, runId: body.runId },
        { type: "STATE_SNAPSHOT", snapshot: { products: [], skillUsages: chosen ? [{ toolCallId: "preload-1", source: "server_preload", status: "used", id: chosen.id, version: chosen.version, title: chosen.title, contentHash: chosen.content_hash }] : [] } },
        { type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId }];
      return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), { headers: { "Content-Type": "text/event-stream" } });
    }
    throw new Error(`未预期的测试请求：${url}`);
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function mount() { await act(async () => { root.render(<App />); }); }
async function type(value: string, caret = value.length) {
  await act(async () => {
    const input = query(); input.focus();
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input, value);
    input.setSelectionRange(caret, caret);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function key(key: string, properties: KeyboardEventInit = {}) {
  await act(async () => { query().dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...properties })); });
}
async function click(element: Element) {
  await act(async () => { element.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
}

describe("真实页面的斜线选购方案交互与官方SDK边界", () => {
  it("只输入斜线选方案会先填可编辑的中性需求，不自动开始商品比较或发请求", async () => {
    await mount(); await type("/"); await key("Enter");
    expect(requests).toHaveLength(0);
    expect(query().value).toBe("请按所选方案帮助我，先确认还缺少哪些必要信息。");
    expect(query().value).not.toContain("比较");
    expect(host.textContent).toContain("已选择 · 周末轻装出游");
    expect(host.textContent).not.toContain("已读取选购方案");
  });

  it("光标词首唤起、上下键循环、Enter仅选择，发送才携带结构化方案并显示真实读取", async () => {
    await mount();
    await type("预算300 /");
    expect(query().getAttribute("aria-expanded")).toBe("true");
    expect(options()).toHaveLength(2);
    await key("ArrowUp");
    expect(options()[1].getAttribute("aria-selected")).toBe("true");
    await key("ArrowDown"); await key("ArrowDown");
    await key("Enter");
    expect(requests).toHaveLength(0);
    expect(query().value).toBe("预算300");
    expect(host.textContent).toContain("已选择 · 通勤声音方案");
    expect(host.textContent).not.toContain("已读取选购方案");
    await key("Enter");
    expect(requests).toHaveLength(1);
    expect(requests[0].forwardedProps.selectedSkill).toEqual({ id: "daily-audio", version: "v2", contentHash: "b".repeat(64) });
    expect(requests[0].messages.at(-1).content).toBe("预算300");
    expect(host.textContent).toContain("已读取选购方案：通勤声音方案 · v2");
    expect(host.textContent).toContain("服务端已读取指定版本");
    expect(host.textContent).not.toContain("工具成功返回");
    await type("再比较一款背包"); await key("Enter");
    expect(requests[1].forwardedProps).not.toHaveProperty("selectedSkill");
  });

  it("按中文名称或ID过滤、鼠标选择并保留光标后正文，取消后不带方案发送", async () => {
    await mount();
    const draft = "预算300 /声音 耳罩要舒适";
    await type(draft, draft.indexOf(" 耳罩"));
    expect(options()).toHaveLength(1);
    expect(options()[0].textContent).toContain("通勤声音方案");
    await click(options()[0]);
    expect(query().value).toBe("预算300  耳罩要舒适");
    await click(host.querySelector('[aria-label="取消已选方案"]')!);
    expect(query().value).toBe("预算300  耳罩要舒适");
    await type("/WEEKEND");
    expect(options()).toHaveLength(1);
    expect(options()[0].textContent).toContain("周末轻装出游");
    await key("Escape");
    expect(query().value).toBe("/WEEKEND");
    expect(query().getAttribute("aria-expanded")).toBe("false");
    await type("预算300，继续直接比较"); await key("Enter");
    expect(requests[0].forwardedProps).not.toHaveProperty("selectedSkill");
  });

  it("网址和词中斜线不唤起，中文输入法Enter不选择也不发送", async () => {
    await mount();
    for (const value of ["https://example.com/a", "看这个 https://example.com/", "型号A/B", "2026/09/09"]) {
      await type(value); expect(query().getAttribute("aria-expanded")).toBe("false");
    }
    await type("/");
    await act(async () => { query().dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })); });
    await key("Enter", { isComposing: true, keyCode: 229 });
    expect(options()).toHaveLength(2); expect(requests).toHaveLength(0);
    expect(host.textContent).not.toContain("已选择 ·");
    await act(async () => { query().dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })); });
    await key("Escape");
    await type("通勤耳机");
    await key("Enter", { isComposing: true, keyCode: 229 });
    expect(requests).toHaveLength(0);
    await key("Enter"); expect(requests).toHaveLength(1);
  });

  it("真实空库、无匹配和加载失败各自呈现，菜单Enter不会误发斜线", async () => {
    catalog = []; await mount(); await type("/");
    expect(host.querySelector('.slash-skill-menu')?.textContent).toContain("选购方案筹备中，可直接描述需求");
    expect(options()).toHaveLength(0);
    await key("Enter"); expect(requests).toHaveLength(0);
    expect(host.querySelector<HTMLButtonElement>('[aria-label="发送选购需求"]')!.disabled).toBe(true);
    catalog = plans;
    await click(host.querySelector('.slash-skill-heading button')!);
    await type("/不存在");
    expect(host.querySelector('.slash-skill-menu')?.textContent).toContain("没有匹配的方案");
    skillsStatus = 503;
    await click(host.querySelector('.slash-skill-heading button')!);
    expect(host.querySelector('.slash-skill-menu')?.textContent).toContain("暂时无法加载");
    expect(host.querySelector('.slash-skill-menu')?.textContent).not.toContain("筹备中");
  });

  it("场景卡与斜线使用相同结构化合同，编辑中到期保留正文而不发送", async () => {
    clock = originalNow();
    catalog = [{ ...plans[0], expires_at: new Date(clock + 1000).toISOString() }];
    await mount(); await type("预算300，寄到中国");
    await click(host.querySelector('[aria-label="选择选购方案：周末轻装出游"]')!);
    expect(query().value).toBe("预算300，寄到中国");
    clock += 1000; catalog = [];
    await key("Enter");
    expect(requests).toHaveLength(0);
    expect(query().value).toBe("预算300，寄到中国");
    expect(host.textContent).toContain("这份方案已过期，已保留你的需求");
    expect(host.textContent).not.toContain("已选择 ·");
    await key("Enter");
    expect(requests[0].forwardedProps).not.toHaveProperty("selectedSkill");
  });

  it("场景卡选择发送同样确定的版本，普通正文中提到方案不生成结构化选择", async () => {
    await mount(); await type("周末出游，预算300");
    await click(host.querySelector('[aria-label="选择选购方案：周末轻装出游"]')!);
    await key("Enter");
    expect(requests[0].forwardedProps.selectedSkill).toEqual({ id: plans[0].id, version: plans[0].version, contentHash: plans[0].content_hash });
    await type("请使用 daily-audio v2，帮我看看耳机"); await key("Enter");
    expect(requests[1].forwardedProps).not.toHaveProperty("selectedSkill");
  });
});

it("澄清入口先向 Agent 发送需求，不直接创建固定表单", async()=>{
  await mount();
  await type("想买一个登机背包");
  const button=[...host.querySelectorAll('button')].find(b=>b.textContent==="澄清选购需求")!;
  await click(button);
  expect(requests).toHaveLength(1);
  expect(requests[0].messages.at(-1).content).toContain("想买一个登机背包");
  expect(requests[0].messages.at(-1).content).toContain("调用选购需求澄清工具");
  expect(vi.mocked(fetch).mock.calls.some(([url,init])=>String(url).includes('/shopping-forms')&&init?.method==='POST')).toBe(false);
  expect(host.querySelector('.shopping-form form')).toBeNull();
});
