// @vitest-environment jsdom
import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HistorySearch from "../src/components/HistorySearch";
import type { SessionSummary } from "../src/types";

type SearchProps = ComponentProps<typeof HistorySearch>;
const records: SessionSummary[] = [
  { id: "travel-alpha", title: "旅行背包", updatedAt: 3000, source: "server" },
  { id: "travel-beta", title: "旅行耳机", updatedAt: 2000, source: "server" },
  { id: "travel-gamma", title: "旅行水杯", updatedAt: 1000, source: "local" },
];
let host: HTMLDivElement, trigger: HTMLButtonElement, root: Root;
let props: SearchProps, opened: ReturnType<typeof vi.fn>, closed: ReturnType<typeof vi.fn>;
let previousOverflow: string;
let originalScrollIntoView: typeof Element.prototype.scrollIntoView;

beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  previousOverflow = document.body.style.overflow;
  originalScrollIntoView = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = vi.fn();
  opened = vi.fn(); closed = vi.fn();
  props = { history: records, currentSessionId: records[0].id, busy: false, onOpen: opened, onClose: closed };
  trigger = document.createElement("button"); trigger.textContent = "搜索";
  host = document.createElement("div");
  document.body.append(trigger, host); root = createRoot(host); trigger.focus();
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove(); trigger.remove();
  document.body.style.overflow = previousOverflow;
  Element.prototype.scrollIntoView = originalScrollIntoView;
  vi.restoreAllMocks();
});

async function render(overrides: Partial<SearchProps> = {}) {
  props = { ...props, ...overrides };
  await act(async () => root.render(<HistorySearch {...props} />));
}
const input = () => host.querySelector<HTMLInputElement>('[role="combobox"]')!;
const options = () => [...host.querySelectorAll<HTMLButtonElement>('[role="option"]')];
const closeButton = () => host.querySelector<HTMLButtonElement>('[aria-label="关闭"]')!;
const clearButton = () => host.querySelector<HTMLButtonElement>('[aria-label="清除搜索"]')!;
function activeOption() {
  const id = input().getAttribute("aria-activedescendant");
  return options().find(option => option.id === id);
}
async function fill(value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input(), value);
    input().dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function key(value: string, properties: KeyboardEventInit = {}, target: Element = input()) {
  const event = new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true, ...properties });
  await act(async () => { target.dispatchEvent(event); });
  return event;
}
async function click(element: HTMLElement) {
  await act(async () => element.click());
}

describe("选购历史搜索弹窗", () => {
  it("完整检索超过100条的摘要，标题或会话ID忽略大小写和首尾空白", async () => {
    const history: SessionSummary[] = Array.from({ length: 120 }, (_, index) => ({
      id: `session-${index}`, title: `选购记录 ${index}`, updatedAt: 1000 - index, source: "server",
    }));
    history.push({ id: "Archive-ZX-120", title: "深冬通勤 Boots", updatedAt: 1, source: "server" });
    await render({ history });
    expect(options()).toHaveLength(121);
    await fill("  bOoTs  ");
    expect(options()).toHaveLength(1);
    expect(options()[0].textContent).toContain("深冬通勤 Boots");
    await fill("  ARCHIVE-zx-120  ");
    expect(options()).toHaveLength(1);
    await key("Enter");
    expect(opened).toHaveBeenLastCalledWith("Archive-ZX-120");
    await fill("  \t ");
    expect(options()).toHaveLength(121);
    expect(host.textContent).toContain("最近选购");
  });

  it("上下键循环选择，Enter交给父级打开且不会自行关闭", async () => {
    await render();
    expect(activeOption()).toBe(options()[0]);
    expect(options()[0].getAttribute("aria-selected")).toBe("true");
    expect((await key("ArrowUp")).defaultPrevented).toBe(true);
    expect(activeOption()).toBe(options()[2]);
    await key("ArrowDown");
    expect(activeOption()).toBe(options()[0]);
    await key("ArrowDown");
    expect(activeOption()).toBe(options()[1]);
    expect((await key("Enter")).defaultPrevented).toBe(true);
    expect(opened).toHaveBeenCalledExactlyOnceWith("travel-beta");
    expect(closed).not.toHaveBeenCalled();
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.activeElement).toBe(input());
  });

  it("中文输入法的方向键与Enter、兼容键码229不选择或打开记录", async () => {
    await render();
    const first = activeOption();
    await key("ArrowDown", { isComposing: true });
    await key("ArrowUp", { keyCode: 229 });
    await key("Enter", { isComposing: true });
    await key("Enter", { keyCode: 229 });
    expect(activeOption()).toBe(first);
    expect(opened).not.toHaveBeenCalled();
    expect(closed).not.toHaveBeenCalled();
    await key("Enter");
    expect(opened).toHaveBeenCalledExactlyOnceWith("travel-alpha");
  });

  it("搜索变化重置高亮，清空后恢复全部记录和输入焦点", async () => {
    await render();
    await key("ArrowUp");
    expect(activeOption()?.textContent).toContain("旅行水杯");
    await fill("旅行");
    expect(activeOption()).toBe(options()[0]);
    await fill("beta");
    expect(options()).toHaveLength(1);
    expect(activeOption()?.textContent).toContain("旅行耳机");
    await fill("没有这样的记录");
    expect(options()).toHaveLength(0);
    expect(input().hasAttribute("aria-activedescendant")).toBe(false);
    await key("ArrowUp"); await key("ArrowDown"); await key("Enter");
    expect(opened).not.toHaveBeenCalled();
    clearButton().focus();
    await click(clearButton());
    expect(input().value).toBe("");
    expect(options()).toHaveLength(3);
    expect(activeOption()).toBe(options()[0]);
    expect(document.activeElement).toBe(input());
  });

  it("列表异步到达或重排时按ID保留高亮，已删除或空列表不留下失效引用", async () => {
    await render({ history: [] });
    await fill("旅行");
    expect(activeOption()).toBeUndefined();
    await render({ history: records });
    expect(input().value).toBe("旅行");
    expect(activeOption()).toBe(options()[0]);
    await key("ArrowDown");
    await render({ history: [records[1], records[2], records[0]] });
    expect(activeOption()?.textContent).toContain("旅行耳机");
    expect(activeOption()).toBe(options()[0]);
    await render({ history: [records[2], records[0]] });
    expect(activeOption()).toBe(options()[0]);
    await key("Enter");
    expect(opened).toHaveBeenLastCalledWith("travel-gamma");
    await render({ history: [] });
    expect(input().hasAttribute("aria-activedescendant")).toBe(false);
    opened.mockClear();
    await key("Enter");
    expect(opened).not.toHaveBeenCalled();
  });

  it("忙碌状态下鼠标和Enter都无法打开，状态解除后使用当前选择", async () => {
    await render();
    await key("ArrowDown");
    await render({ busy: true });
    expect(options().every(option => option.getAttribute("aria-disabled") === "true")).toBe(true);
    expect(host.textContent).toContain("当前选购正在处理中");
    await click(options()[0]); await key("Enter");
    expect(opened).not.toHaveBeenCalled();
    expect(closed).not.toHaveBeenCalled();
    await render({ busy: false });
    await key("Enter");
    expect(opened).toHaveBeenCalledExactlyOnceWith("travel-beta");
    await click(options()[2]);
    expect(opened).toHaveBeenLastCalledWith("travel-gamma");
  });

  it("区分没有历史与没有匹配，历史错误不妨碍使用本机缓存", async () => {
    await render({ history: [] });
    expect(host.textContent).toContain("还没有选购记录");
    expect(host.textContent).not.toContain("没有找到匹配记录");
    await fill("不存在");
    expect(host.textContent).toContain("没有找到匹配记录");
    expect(host.textContent).not.toContain("还没有选购记录");
    await fill("");
    await render({ history: [records[2]], error: "服务端历史暂不可用，当前显示本机缓存。" });
    expect([...host.querySelectorAll('[role="status"]')].some(item => item.textContent?.includes("服务端历史暂不可用"))).toBe(true);
    expect(options()).toHaveLength(1);
    expect(host.textContent).not.toContain("还没有选购记录");
    await key("Enter");
    expect(opened).toHaveBeenCalledExactlyOnceWith("travel-gamma");
  });

  it("打开时聚焦搜索框，Escape由父级移除后恢复入口焦点和原滚动状态", async () => {
    document.body.style.overflow = "scroll";
    await render();
    expect(document.activeElement).toBe(input());
    expect(host.querySelector('[role="dialog"]')?.getAttribute("aria-modal")).toBe("true");
    expect(document.body.style.overflow).toBe("hidden");
    await key("Escape");
    expect(closed).toHaveBeenCalledOnce();
    expect(opened).not.toHaveBeenCalled();
    // 弹窗生命周期由父级控制；关闭回调之后模拟父级卸载。
    await act(async () => root.render(null));
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe("scroll");
  });

  it("Tab在关闭、输入和清空之间闭环，不把焦点移入tabIndex=-1的结果", async () => {
    await render();
    expect(options().every(option => option.tabIndex === -1)).toBe(true);
    expect((await key("Tab")).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(closeButton());
    expect((await key("Tab", { shiftKey: true }, closeButton())).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(input());
    await fill("旅行");
    clearButton().focus();
    expect((await key("Tab", {}, clearButton())).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(closeButton());
    expect((await key("Tab", { shiftKey: true }, closeButton())).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(clearButton());
    expect(opened).not.toHaveBeenCalled();
  });
});
