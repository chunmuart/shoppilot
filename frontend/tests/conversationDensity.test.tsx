// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import ConversationTurns from "../src/components/ConversationTurns";
import ExecutionTrace from "../src/components/ExecutionTrace";
import ToolProductResult from "../src/components/ToolProductResult";
import type { ChatMessage, ExecutionActivity } from "../src/types";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: { host: HTMLDivElement; root: Root }[] = [];
async function mount(content: ReactNode) {
  const host = document.createElement("div"), root = createRoot(host);
  document.body.append(host); mounted.push({ host, root });
  const render = async (next: ReactNode) => { await act(async () => root.render(next)); };
  await render(content);
  return { host, render };
}
afterEach(async () => {
  for (const { host, root } of mounted.splice(0)) {
    await act(async () => root.unmount()); host.remove();
  }
});
async function click(element: HTMLElement) {
  await act(async () => {
    element.click();
    // 原生 details 的 toggle 在下一任务派发，等它完成再检查 React 状态。
    await new Promise(resolve => setTimeout(resolve, 0));
  });
}
async function toggle(details: HTMLDetailsElement) { await click(details.querySelector<HTMLElement>(":scope > summary")!); }
function button(host: ParentNode, label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === label);
  expect(found, `应存在“${label}”按钮`).toBeDefined(); return found!;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
const activity = (patch: Partial<ExecutionActivity> = {}): ExecutionActivity => ({
  id: "r:tool:a", runId: "r", toolCallId: "r:tool:a", toolName: "product_search_tool",
  kind: "tool", label: "检索商品", status: "completed", sequence: 1,
  startedAt: 1000, finishedAt: 1500, resultSummary: "已找到商品", ...patch,
});
const product = (index: number, title = `测试商品 ${index}`) => ({
  product_id: `P${1000 + index}`, title, price_major: 100 + index, currency: "CNY",
  default_sku_id: `P${1000 + index}-S1`, spec: "标准规格",
});
const result = (runId = "r", toolCallId = "r:tool:a", products = [product(1)]) => ({
  runId, toolCallId, status: "ready", products, totalCount: products.length, truncated: false,
});

it("已完成的紧凑过程默认折叠，展开轮次不会提前请求商品", async () => {
  const load = vi.fn(async () => result());
  const { host } = await mount(<ExecutionTrace activities={[activity()]} compact loadToolProducts={load} />);
  const run = host.querySelector<HTMLDetailsElement>(".execution-run")!;
  const row = host.querySelector<HTMLDetailsElement>(".execution-row details")!;
  expect(run.open).toBe(false); expect(row.open).toBe(false); expect(load).not.toHaveBeenCalled();
  await toggle(run);
  expect(run.open).toBe(true); expect(row.open).toBe(false); expect(load).not.toHaveBeenCalled();
});

it("展开工具才按运行与调用读取；同名并行结果乱序返回也不会串卡", async () => {
  const first = deferred<unknown>(), second = deferred<unknown>(), otherRun = deferred<unknown>();
  const load = vi.fn((runId: string, callId: string) => runId === "other" ? otherRun.promise : callId === "shared" ? first.promise : second.promise);
  const { host } = await mount(<ExecutionTrace activities={[
    activity({ id: "first", toolCallId: "shared" }),
    activity({ id: "second", toolCallId: "second", sequence: 2 }),
    activity({ id: "first", runId: "other", toolCallId: "shared" }),
  ]} loadToolProducts={load} />);
  const rows = [...host.querySelectorAll<HTMLDetailsElement>(".execution-row details")];
  expect(load).not.toHaveBeenCalled();
  await toggle(rows[0]); expect(load.mock.calls).toEqual([["r", "shared"]]);
  await toggle(rows[1]); await toggle(rows[2]);
  expect(load.mock.calls).toEqual([["r", "shared"], ["r", "second"], ["other", "shared"]]);
  await act(async () => second.resolve(result("r", "second", [product(2, "第二次调用商品")])));
  expect(rows[1].textContent).toContain("第二次调用商品"); expect(rows[0].textContent).not.toContain("第二次调用商品");
  await act(async () => {
    otherRun.resolve(result("other", "shared", [product(3, "另一轮商品")]));
    first.resolve(result("r", "shared", [product(1, "第一次调用商品")]));
  });
  expect(rows[0].textContent).toContain("第一次调用商品"); expect(rows[0].textContent).not.toContain("另一轮商品");
  expect(rows[2].textContent).toContain("另一轮商品"); expect(rows[2].textContent).not.toContain("第一次调用商品");
  await toggle(rows[0]); await toggle(rows[0]); expect(load).toHaveBeenCalledTimes(3);
});

it("流式消息替换为最终消息且运行结束后，手动展开的步骤和全部小卡仍保留", async () => {
  const load = vi.fn(async () => result("r", "r:tool:a", Array.from({ length: 8 }, (_, index) => product(index + 1))));
  const messages: ChatMessage[] = [{ id: "user", role: "user", content: "找旅行装备", runId: "r" }];
  const renderTurns = (next: ChatMessage[], running: boolean) => <ConversationTurns
    messages={next} activities={[activity()]} activeRunId="r" running={running} loadToolProducts={load} />;
  const { host, render } = await mount(renderTurns(messages, true));
  const run = host.querySelector<HTMLDetailsElement>(".execution-run")!;
  const row = host.querySelector<HTMLDetailsElement>(".execution-row details")!;
  expect(run.open).toBe(true); await toggle(row); expect(row.open).toBe(true);
  expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(6);
  await click(button(host, "查看全部 8 件")); expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(8);
  await render(renderTurns([...messages, { id: "stream:1", role: "assistant", content: "正在整理候选", runId: "r" }], true));
  await render(renderTurns([...messages, { id: "final:1", role: "assistant", content: "已经整理好八件商品。", runId: "r" }], false));
  expect(host.querySelector(".execution-run")).toBe(run); expect(host.querySelector(".execution-row details")).toBe(row);
  expect(run.open).toBe(true); expect(row.open).toBe(true); expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(8);
  expect(host.textContent).toContain("已经整理好八件商品。"); expect(host.textContent).not.toContain("正在整理候选");
  expect(load).toHaveBeenCalledTimes(1);
  await click(button(host, "收起")); expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(6);
});

it("用户手动收起运行中的过程后，后续事件与完成状态不会重新展开", async () => {
  const renderTrace = (running: boolean, status: ExecutionActivity["status"]) => <ExecutionTrace
    activities={[activity({ status })]} runId="r" running={running} compact />;
  const { host, render } = await mount(renderTrace(true, "running"));
  const run = host.querySelector<HTMLDetailsElement>(".execution-run")!;
  await toggle(run); expect(run.open).toBe(false);
  await render(renderTrace(true, "completed")); await render(renderTrace(false, "completed"));
  expect(host.querySelector(".execution-run")).toBe(run); expect(run.open).toBe(false);
});

it("有真实 Think 时可展开查看正文，思考行不会加载商品", async () => {
  const load = vi.fn(async () => result());
  const { host } = await mount(<ExecutionTrace compact loadToolProducts={load} activities={[
    activity({ id: "r:think:1", kind: "thinking", toolName: undefined, toolCallId: undefined,
      content: "先核对预算，再检查配送地区。", resultSummary: undefined }),
  ]} />);
  await toggle(host.querySelector<HTMLDetailsElement>(".execution-run")!);
  const row = host.querySelector<HTMLDetailsElement>(".execution-row details")!;
  expect(row.open).toBe(false); await toggle(row); expect(row.open).toBe(true);
  expect(row.querySelector(".execution-thinking-text")?.textContent).toBe("先核对预算，再检查配送地区。");
  expect(host.querySelector(".execution-thinking-note")).toBeNull(); expect(load).not.toHaveBeenCalled();
});

it("本轮没有 Think 时如实提示，不从工具摘要生成思考正文", async () => {
  const { host } = await mount(<ExecutionTrace compact activities={[activity()]} />);
  expect(host.querySelectorAll(".execution-thinking")).toHaveLength(0); expect(host.textContent).toContain("无 Think 记录");
  await toggle(host.querySelector<HTMLDetailsElement>(".execution-run")!);
  expect(host.querySelector(".execution-thinking-note")?.textContent).toContain("本轮没有可展示的思考记录");
  expect(host.querySelector(".execution-thinking-text")).toBeNull();
});

it("旧历史没有活动记录时提示记录缺失，不能宣称进行了零次调用", async () => {
  const { host } = await mount(<ConversationTurns messages={[
    { id: "old:user", role: "user", content: "旧的需求", runId: "old" },
    { id: "old:answer", role: "assistant", content: "旧的答复", runId: "old" },
  ]} activities={[]} running={false} />);
  expect(host.textContent).toContain("本轮未保存过程记录"); expect(host.textContent).not.toContain("0 次工具调用");
  expect(host.textContent).not.toContain("处理完成"); expect(host.querySelectorAll(".execution-row")).toHaveLength(0);
  expect(host.textContent).toContain("旧的答复");
});

it.each([
  { name: "分页片段为空", status: "ready", totalCount: 1, truncated: true, text: "分页或不完整" },
  { name: "字段不完整被过滤", status: "ready", totalCount: 3, truncated: true, text: "分页或不完整" },
  { name: "真实零命中", status: "ready", totalCount: 0, truncated: false, text: "本次检索没有返回商品" },
  { name: "旧调用没有可读结果", status: "unavailable", totalCount: null, truncated: false, text: "没有可展示的商品卡" },
])("小卡空态区分：$name", async ({ status, totalCount, truncated, text }) => {
  const load = vi.fn(async () => ({ ...result(), products: [], status, totalCount, truncated }));
  const { host } = await mount(<ToolProductResult runId="r" toolCallId="r:tool:a" load={load} />);
  expect(host.textContent).toContain(text); expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(0);
  if (truncated) expect(host.textContent).not.toContain("没有返回商品");
});

it("等待保存的商品结果可以重新读取，而不要求刷新整个会话", async () => {
  const load = vi.fn().mockResolvedValueOnce({ ...result(), status: "pending", products: [], totalCount: null }).mockResolvedValueOnce(result());
  const { host } = await mount(<ToolProductResult runId="r" toolCallId="r:tool:a" load={load} />);
  expect(host.textContent).toContain("商品结果尚未保存"); await click(button(host, "重新读取"));
  expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(1); expect(host.textContent).not.toContain("商品结果尚未保存");
  expect(load.mock.calls).toEqual([["r", "r:tool:a"], ["r", "r:tool:a"]]);
});

it("请求失败只显示友好提示，重试仍绑定原来的运行和调用", async () => {
  const load = vi.fn().mockRejectedValueOnce(new Error("private-network-body")).mockResolvedValueOnce(result());
  const { host } = await mount(<ToolProductResult runId="r" toolCallId="r:tool:a" load={load} />);
  expect(host.querySelector('[role="status"]')?.textContent).toContain("暂时无法读取"); expect(host.textContent).not.toContain("private-network-body");
  await click(button(host, "重试")); expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(1);
  expect(load.mock.calls).toEqual([["r", "r:tool:a"], ["r", "r:tool:a"]]);
});

it.each([{ runId: "other", callId: "r:tool:a" }, { runId: "r", callId: "other:tool" }])(
  "拒绝返回了错误关联的商品：$runId / $callId", async ({ runId, callId }) => {
  const load = vi.fn(async () => result(runId, callId, [product(9, "不属于当前调用的商品")]));
  const { host } = await mount(<ToolProductResult runId="r" toolCallId="r:tool:a" load={load} />);
  expect(host.textContent).toContain("暂时无法读取"); expect(host.textContent).not.toContain("不属于当前调用的商品");
  expect(host.querySelectorAll(".tool-product-mini")).toHaveLength(0);
});

it("切换调用后迟到的旧请求不能覆盖新调用结果", async () => {
  const previous = deferred<unknown>(), current = deferred<unknown>();
  const load = vi.fn((runId: string) => runId === "previous" ? previous.promise : current.promise);
  const { host, render } = await mount(<ToolProductResult runId="previous" toolCallId="shared" load={load} />);
  await render(<ToolProductResult runId="current" toolCallId="shared" load={load} />);
  await act(async () => current.resolve(result("current", "shared", [product(2, "当前调用商品")])));
  await act(async () => previous.resolve(result("previous", "shared", [product(1, "先前调用商品")])));
  expect(host.textContent).toContain("当前调用商品"); expect(host.textContent).not.toContain("先前调用商品");
  expect(load.mock.calls).toEqual([["previous", "shared"], ["current", "shared"]]);
});
