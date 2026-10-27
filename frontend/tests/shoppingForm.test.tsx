// @vitest-environment jsdom
import React, {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {afterEach, beforeEach, expect, it, vi} from "vitest";
import ShoppingForm, {readShoppingForm, displayShoppingMessage} from "../src/components/ShoppingForm";

let host: HTMLDivElement, root: Root;
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const q = (id: string, type: string, label: string, extra = {}) => ({
  id, type, label, required: false, help_text: "", placeholder: "", unit: "", minimum: null, maximum: null, options: [], ...extra,
});
function makeForm(questions: ReturnType<typeof q>[], defaults = {}, formId = "form-1") {
  return {form_id: formId, session_id: "s", revision: 1, submission: null, messages: [
    {version: "v0.9", createSurface: {surfaceId: formId, catalogId: "shoppilot.local/shopping-v2"}},
    {version: "v0.9", updateComponents: {surfaceId: formId, components: [{id: "root", component: "ShoppingForm", title: "Agent 本次提出的问题",
      context: "已明确想选耳机", description: "请补充使用方式，便于比较", questions, value: {path: "/requirements"},
      action: {event: {name: "applyShoppingRequirements", context: {requirements: {path: "/requirements"}}}}}]}},
    {version: "v0.9", updateDataModel: {surfaceId: formId, path: "/", value: {requirements: defaults, revision: 1}}},
  ]};
}
const form = makeForm([q("hours", "number", "每天佩戴多久？", {unit: "小时", minimum: 0, maximum: 24})]);
const clone = (v: unknown): any => JSON.parse(JSON.stringify(v));

it("聊天只展示问答摘要，保留原始结构化输入及未回答状态", () => {
  const payload = {title: "耳机需求", agent_context: "内部背景", answers: [
    {question: "使用场景", display_value: ["通勤", "会议"], unit: ""},
    {question: "每天佩戴多久", display_value: 3, unit: "小时"},
  ], unanswered: [{question: "品牌偏好"}]};
  const content = "买家已提交本轮澄清答案。说明\n" + JSON.stringify(payload);
  const message = {id: "form-run-" + "a".repeat(32) + ":user", content};
  expect(displayShoppingMessage(message)).toBe("已补充：耳机需求\n使用场景：通勤、会议\n每天佩戴多久：3 小时\n暂未填写：品牌偏好");
  expect(message.content).toBe(content);
  expect(displayShoppingMessage({...message, id: "normal-user-message"})).toBe(content);
  expect(displayShoppingMessage({...message, content: "买家已提交本轮澄清答案。\n坏 JSON"})).toContain("坏 JSON");
});
beforeEach(() => {host = document.createElement("div"); document.body.append(host); root = createRoot(host);});
afterEach(async () => {await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks();});
async function input(name: string, value: string) {
  const el = host.querySelector(`input[name="${name}"]`)!;
  await act(async () => {Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value); el.dispatchEvent(new Event("input", {bubbles: true}));});
}
async function submit() {
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
}

it("提交结构化答案并使用服务端生成的消息和运行 ID", async () => {
  const applied = vi.fn(), request = vi.fn(async (_path: string, _method?: string, _body?: Record<string, unknown>) => ({status: "submitted", query: "已收到：每天佩戴3小时", run_id: "run-1"}));
  await act(async () => root.render(<ShoppingForm form={form} busy={false} request={request} onApplied={applied}/>));
  await input("hours", "3"); await submit();
  const body = request.mock.calls[0][2] as any;
  expect(body.action.name).toBe("applyShoppingRequirements");
  expect(body.action.context.requirements).toEqual({hours: 3});
  expect(applied).toHaveBeenCalledWith("已收到：每天佩戴3小时", "run-1");
});

it("未知组件拒绝渲染，运行中不能提交", async () => {
  const request = vi.fn(), applied = vi.fn();
  await act(async () => root.render(<ShoppingForm form={form} busy={true} request={request} onApplied={applied}/>));
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  await submit(); expect(request).not.toHaveBeenCalled();
  const bad = clone(form); bad.messages[1].updateComponents.components[0].component = "Html";
  await act(async () => root.render(<ShoppingForm form={bad} busy={false} request={request} onApplied={applied}/>));
  expect(host.querySelector("form")).toBeNull(); expect(host.textContent).toContain("暂不支持");
});

it("刷新后恢复题目与答案，重复继续不再写入表单", async () => {
  const request = vi.fn(), applied = vi.fn();
  const saved = {...form, submission: {query: "每天佩戴3小时", run_id: "stable-run", status: "submitted", values: {hours: 3}}};
  await act(async () => root.render(<ShoppingForm form={saved} busy={false} request={request} onApplied={applied}/>));
  expect(host.textContent).toContain("每天佩戴多久？"); expect(host.textContent).toContain("3 小时");
  await act(async () => host.querySelector("button")!.click());
  expect(request).not.toHaveBeenCalled(); expect(applied).toHaveBeenCalledWith("每天佩戴3小时", "stable-run");
});

it("问题、顺序、文案和选项由 Agent 指定，不自动追加任何业务问题", async () => {
  const questions = [
    q("scene", "single_select", "主要在什么场景听音乐？", {required: true, options: [{value: "metro", label: "地铁通勤"}, {value: "run", label: "户外跑步"}]}),
    q("features", "multi_select", "更看重哪些特性？", {options: [{value: "anc", label: "主动降噪"}, {value: "comfort", label: "佩戴舒适"}]}),
    q("device", "text", "连接什么设备？", {placeholder: "填写设备型号", help_text: "用于检查兼容性"}),
  ];
  const request = vi.fn(async (_p: string, _m?: string, _b?: Record<string, unknown>) => ({query: "耳机澄清答案", run_id: "run"}));
  await act(async () => root.render(<ShoppingForm form={makeForm(questions)} busy={false} request={request} onApplied={vi.fn()}/>));
  expect(host.querySelectorAll(".shopping-form-question")).toHaveLength(3);
  expect(Array.from(host.querySelectorAll(".shopping-form-question")).map(el => el.textContent)).toEqual([
    expect.stringContaining("主要在什么场景听音乐？"), expect.stringContaining("更看重哪些特性？"), expect.stringContaining("连接什么设备？"),
  ]);
  expect(host.querySelector('[name="query"],[name="budget"],[name="airline"]')).toBeNull();
  const choice = host.querySelector<HTMLSelectElement>('select[name="scene"]')!;
  expect(choice.value).toBe("");
  await act(async () => {choice.value = "metro"; choice.dispatchEvent(new Event("change", {bubbles: true}));});
  await act(async () => host.querySelector<HTMLInputElement>('input[value="anc"]')!.click());
  await input("device", "笔记本"); await submit();
  expect((request.mock.calls[0][2] as any).action.context.requirements).toEqual({scene: "metro", features: ["anc"], device: "笔记本"});
});

it("只校验 Agent 声明的必填，留空可选项不生成答案", async () => {
  const request = vi.fn();
  const questions = [q("features", "multi_select", "请选择关心的功能", {required: true, options: [{value: "anc", label: "降噪"}]}), q("note", "text", "可选补充")];
  await act(async () => root.render(<ShoppingForm form={makeForm(questions)} busy={false} request={request} onApplied={vi.fn()}/>));
  await submit(); expect(request).not.toHaveBeenCalled(); expect(host.textContent).toContain("请回答：请选择关心的功能");
  await act(async () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  await submit(); expect((request.mock.calls[0][2] as any).action.context.requirements).toEqual({features: ["anc"]});
});

it("新一轮工具调用可完全更换题目，不能残留上一轮答案", async () => {
  const request = vi.fn();
  await act(async () => root.render(<ShoppingForm form={form} busy={false} request={request} onApplied={vi.fn()}/>));
  await input("hours", "5");
  const next = makeForm([q("connection", "text", "要有线还是无线？")], {}, "form-2");
  await act(async () => root.render(<ShoppingForm form={next} busy={false} request={request} onApplied={vi.fn()}/>));
  expect(host.querySelector('[name="hours"]')).toBeNull(); await input("connection", "无线"); await submit();
  expect((request.mock.calls[0][2] as any).action.context.requirements).toEqual({connection: "无线"});
});

it("拒绝未知输入类型和重复题号，题目中的 HTML 作为文本展示", async () => {
  const badType = clone(form); badType.messages[1].updateComponents.components[0].questions[0].type = "script";
  expect(readShoppingForm(badType)).toBeNull();
  const duplicate = clone(form); duplicate.messages[1].updateComponents.components[0].questions.push(duplicate.messages[1].updateComponents.components[0].questions[0]);
  expect(readShoppingForm(duplicate)).toBeNull();
  const malicious = makeForm([q("note", "text", '<img src=x onerror="alert(1)">')]);
  await act(async () => root.render(<ShoppingForm form={malicious} busy={false} request={vi.fn()} onApplied={vi.fn()}/>));
  expect(host.querySelector("img,script")).toBeNull(); expect(host.textContent).toContain("<img src=x");
});

it("保存请求未完成时重复提交只发送一次", async () => {
  let resolve!: (value: Record<string, unknown>) => void;
  const request = vi.fn(() => new Promise<Record<string, unknown>>(done => {resolve = done;}));
  const applied = vi.fn();
  await act(async () => root.render(<ShoppingForm form={form} busy={false} request={request} onApplied={applied}/>));
  await input("hours", "2"); await submit(); await submit(); expect(request).toHaveBeenCalledTimes(1);
  await act(async () => resolve({query: "2小时", run_id: "once", values: {hours: 2}}));
  expect(applied).toHaveBeenCalledTimes(1);
});
