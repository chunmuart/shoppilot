// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import Automations from "../src/components/Automations";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: { root: Root; host: HTMLElement }[] = [];
const model = {
  profile_id: "gateway",
  profile_revision: 3,
  label: "测试模型",
  model_id: "fixture",
  provider: "测试供应商",
  builtin: true,
  status: "active",
  visible: true,
  capabilities: { context_max: 200000 },
  window_options: [{ id: "200k", tokens: 200000 }],
  default_window: "200k",
  reasoning: { kind: "boolean", options: ["off", "on"], default: "off" },
};
const definition = {
  name: "每周通勤清单",
  prompt: "比较背包，预算400元",
  model: {
    profile_id: "gateway",
    profile_revision: 3,
    window_id: "200k",
    thinking: "off",
  },
  skill: null,
  auto_start: false,
  output_mode: "merged",
  schedule: {
    kind: "weekly",
    time: "10:00",
    weekdays: [1],
    timezone: "Asia/Shanghai",
    cron: "",
    at: null,
    end_date: null,
  },
  currency: "CNY",
  locale: "zh-CN",
};
const task = {
  id: "task",
  revision: 2,
  status: "active",
  definition,
  next_run_at: 1791856800,
};
function requests(
  extra?: (path: string, method?: string, body?: any) => unknown,
) {
  return vi.fn(async (path: string, method?: string, body?: any) => {
    const result = extra?.(path, method, body);
    if (result !== undefined) return await result;
    if (path.startsWith("/models?")) return { models: [model], revision: 0, selection: null, default_revision: 0, management_allowed: false };
    if (path === "/automations/options") return { models: [model], skills: [] };
    if (path === "/automations/templates")
      return {
        items: [
          {
            id: "pick",
            name: "每周通勤清单",
            description: "按预算比较商品",
            prompt: "比较背包，预算400元",
            time: "10:00",
            weekdays: [1],
          },
        ],
      };
    if (path === "/automations/preview")
      return { times: ["2026-10-12T10:00:00+08:00"] };
    if (path.startsWith("/automations?"))
      return {
        items: [task],
        total: 1,
        worker: { online: true, last_seen: 1 },
      };
    if (path.startsWith("/automation-runs?")) return { items: [], total: 0 };
    return {};
  });
}
async function mount(
  request: ReturnType<typeof requests>,
  onOpenSession = vi.fn(),
) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () =>
    root.render(
      <Automations sessionId="test-session" request={request} onOpenSession={onOpenSession} />,
    ),
  );
  return host;
}
async function click(element: Element) {
  await act(async () => {
    (element as HTMLElement).click();
  });
}
function button(host: ParentNode, text: string) {
  const b = [...host.querySelectorAll("button")].find(
    (x) => x.textContent?.trim() === text,
  );
  expect(b).toBeDefined();
  return b!;
}
async function preview() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 300));
  });
}
afterEach(async () => {
  for (const x of mounted.splice(0)) {
    await act(async () => x.root.unmount());
    x.host.remove();
  }
});

it("加载服务器计划和调度心跳，读取页面不触发运行", async () => {
  const request = requests(),
    host = await mount(request);
  expect(host.textContent).toContain("每周通勤清单");
  expect(host.textContent).toContain("调度服务在线");
  expect(request.mock.calls.some((x) => x[1] === "POST")).toBe(false);
});

it("模板先填表，独立输出与明确授权按模型版本提交，创建不附带执行", async () => {
  const request = requests(),
    host = await mount(request);
  await click(button(host, "模板"));
  await click(button(host, "使用模板"));
  await preview();
  expect(
    request.mock.calls.some((x) => x[0] === "/automations" && x[1] === "POST"),
  ).toBe(false);
  await click(host.querySelector('input[value="independent"]')!);
  const checks = host.querySelectorAll("input[type=checkbox]");
  await click(checks[checks.length - 1]);
  await act(async () =>
    host
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  const saved = request.mock.calls.find(
    (x) => x[0] === "/automations" && x[1] === "POST",
  );
  expect(saved?.[2].definition.output_mode).toBe("independent");
  expect(saved?.[2].definition.auto_start).toBe(true);
  expect(saved?.[2].definition.model).toEqual(definition.model);
  expect(saved?.[2].request_id).toBeTruthy();
  expect(
    request.mock.calls.some((x) => x[0].endsWith("/runs") && x[1] === "POST"),
  ).toBe(false);
});

it("编辑携带当前 revision，保持合并输出及关闭自动开始的选择", async () => {
  const request = requests(),
    host = await mount(request);
  await click(button(host, "编辑"));
  await preview();
  await act(async () =>
    host
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  const saved = request.mock.calls.find(
    (x) => x[0] === "/automations/task" && x[1] === "PATCH",
  );
  expect(saved?.[2].expected_revision).toBe(2);
  expect(saved?.[2].definition.output_mode).toBe("merged");
  expect(saved?.[2].definition.auto_start).toBe(false);
});

it("保存失败重试沿用 request_id，不能因网络错误重复创建", async () => {
  let attempts = 0;
  const request = requests((path, method) => {
    if (path === "/automations/task" && method === "PATCH") {
      if (++attempts === 1) return Promise.reject(new Error("暂时离线"));
      return {};
    }
  });
  const host = await mount(request);
  await click(button(host, "编辑"));
  await preview();
  for (let i = 0; i < 2; i++)
    await act(async () =>
      host
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
  const calls = request.mock.calls.filter(
    (x) => x[0] === "/automations/task" && x[1] === "PATCH",
  );
  expect(calls).toHaveLength(2);
  expect(calls[0][2].request_id).toBe(calls[1][2].request_id);
});

it("待确认记录打开原会话，不在页面自动批准或重跑", async () => {
  const run = {
    id: "run",
    revision: 1,
    status: "awaiting_confirmation",
    definition,
    scheduled_for: 1,
    session_id: "merged-session",
  };
  const request = requests((path) =>
      path.startsWith("/automation-runs?")
        ? { items: [run], total: 1 }
        : undefined,
    ),
    open = vi.fn();
  const host = await mount(request, open);
  await click(button(host, "执行记录"));
  await click(button(host, "打开结果"));
  expect(open).toHaveBeenCalledWith("merged-session");
  expect(request.mock.calls.some((x) => x[1] === "POST")).toBe(false);
});

it("非法时间预览禁止保存，错误显示在时间设置附近", async () => {
  const request = requests((path) =>
    path === "/automations/preview"
      ? Promise.reject(new Error("无有效执行时间"))
      : undefined,
  );
  const host = await mount(request);
  await click(button(host, "编辑"));
  await preview();
  expect(host.textContent).toContain("无有效执行时间");
  expect(button(host, "保存设置").disabled).toBe(true);
});

it("自动化复用聊天选择器：分组、搜索、思考与版本进入草稿，取消不修改聊天或默认模型", async () => {
  const alternative = { ...model, profile_id: "custom", label: "自定义轻量模型", model_id: "light", builtin: false, profile_revision: 5 };
  const request = requests(path => path.startsWith("/models?") ? {
    models: [model, alternative], revision: 9, selection: { ...definition.model }, default_revision: 1, management_allowed: false,
  } : undefined);
  const host = await mount(request);
  await click(button(host, "编辑"));
  await click(host.querySelector('.model-picker-trigger')!);
  expect(host.querySelector('.automation-model-menu')).toBeNull();
  expect(host.querySelector('[aria-label="搜索模型"]')).not.toBeNull();
  expect(host.querySelector('.model-default-action')).toBeNull();
  await click(button(host.querySelector('.model-picker-groups')!, "自定义"));
  await click(host.querySelector('.model-option')!);
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain("自定义轻量模型");
  await click(host.querySelector('.model-picker-trigger')!);
  await click(host.querySelector('.model-choice input[value="on"]')!);
  expect(request.mock.calls.filter(call => call[1] && call[1] !== "GET").every(call => call[0] === "/automations/preview")).toBe(true);
  await preview();
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(request.mock.calls.find(call => call[0] === "/automations/task" && call[1] === "PATCH")?.[2].definition.model)
    .toEqual({ profile_id: "custom", profile_revision: 5, window_id: "200k", thinking: "on" });
});

it("模型浮层和嵌套模型管理分别关闭，不会退出任务草稿或提交任务", async () => {
  const request = requests();
  const host = await mount(request);
  await click(button(host, "编辑"));
  await click(host.querySelector('.model-picker-trigger')!);
  await act(async () => host.querySelector('[aria-label="搜索模型"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(host.querySelector('.automation-editor')).not.toBeNull();
  expect(host.querySelector('.model-picker-panel')).toBeNull();
  await click(host.querySelector('.model-picker-trigger')!);
  await click(host.querySelector('.model-manage-link')!);
  expect(document.querySelector('.model-manager')).not.toBeNull();
  // React portal 的 submit 冒泡不能误保存其下层自动化表单。
  await act(async () => document.querySelector('.model-manager')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(request.mock.calls.some(call => call[0] === "/automations/task" && call[1] === "PATCH")).toBe(false);
  await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(document.querySelector('.model-manager')).toBeNull();
  expect(host.querySelector('.automation-editor')).not.toBeNull();
  expect(document.activeElement).toBe(host.querySelector('.model-picker-trigger'));
  await click(button(host, "取消"));
  expect(host.querySelector('.automation-editor')).toBeNull();
  expect(request.mock.calls.some(call => call[1] === "PATCH")).toBe(false);
});
