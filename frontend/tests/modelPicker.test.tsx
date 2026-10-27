// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ModelPicker, { type ModelWorkspaceOptions } from '../src/components/ModelPicker';
import type { ModelCatalog, ModelPreset, ModelProfile } from '../src/lib/modelCatalog';
import { formatModelWindow } from '../src/lib/modelCatalog';

let host: HTMLDivElement, root: Root, catalog: ModelCatalog;
let calls: { path: string; method?: string; body?: Record<string, unknown> }[];
let request: ReturnType<typeof vi.fn>, changed: ReturnType<typeof vi.fn>;
const profile = (id: string, overrides: Partial<ModelProfile> = {}): ModelProfile => ({
  profile_id: id, profile_revision: 2, label: id === 'main' ? '主选购模型' : '轻量模型', model_id: id,
  provider: '测试网关', protocol: 'openai_chat', builtin: true, source: { kind: 'operator_declared' },
  status: 'active', visible: true, capabilities: { context_max: 128000, max_output: 8192, streaming: true, tool_calling: true },
  window_options: [{ id: 'budget-64000', tokens: 64000, kind: 'local_budget' }, { id: 'budget-128000', tokens: 128000, kind: 'local_budget' }],
  default_window: 'budget-128000', reasoning: { kind: 'boolean', options: ['default', 'off', 'on'], default: 'default' }, ...overrides,
});

beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  catalog = { revision: 7, models: [profile('main'), profile('light', { builtin: false, reasoning: { kind: 'unsupported', options: ['default'], default: 'default' } })],
    selection: { profile_id: 'main', profile_revision: 2, window_id: 'budget-128000', thinking: 'on' },
    management_allowed: true, default_profile_id: 'main', default_revision: 0 };
  calls = []; changed = vi.fn();
  request = vi.fn(async (path: string, method?: string, body?: Record<string, unknown>) => {
    calls.push({ path, method, body });
    if (path.startsWith('/models?')) return JSON.parse(JSON.stringify(catalog));
    if (path.endsWith('/model') && method === 'PATCH') {
      catalog = { ...catalog, revision: catalog.revision + 1, selection: { profile_id: String(body!.profile_id),
        profile_revision: Number(body!.profile_revision), window_id: String(body!.window_id), thinking: body!.thinking as string } };
      return { ...catalog };
    }
    if (path.endsWith('/preferences')) {
      const id = path.split('/')[2];
      catalog.models = catalog.models.map(model => model.profile_id === id ? { ...model, visible: Boolean(body!.visible), preferred_window: String(body!.default_window), preferred_thinking: String(body!.thinking) } : model);
      return { saved: true };
    }
    if (path === '/model-connections/validate') return { validation_ticket: 'test-ticket', config_hash: 'safe-hash' };
    if (path === '/model-connections') return { saved: true };
    if (path === '/models/default') { catalog.default_revision++; catalog.default_selection = body as unknown as ModelCatalog['default_selection']; return { saved: true }; }
    throw new Error('未预期请求：' + path);
  });
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); localStorage.clear(); });
const mount = async (overrides: Partial<ModelWorkspaceOptions> = {}) => act(async () => root.render(
  <ModelPicker sessionId="s1" request={request} busy={false} pending={false} onChanged={changed} {...overrides} />,
));
const click = async (element: Element | null) => { expect(element).toBeTruthy(); await act(async () => (element as HTMLElement).click()); };
const open = () => click(host.querySelector('.model-picker-trigger'));
const manage = async () => { await open(); await click(host.querySelector('.model-manage-link')); };
const add = async () => { await manage(); await click(document.querySelector('.model-add-link')); };
async function fill(element: HTMLInputElement | HTMLSelectElement | null, value: string) {
  expect(element).toBeTruthy();
  await act(async () => {
    Object.getOwnPropertyDescriptor(element!.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(element, value);
    element!.dispatchEvent(new Event(element!.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  });
}
const submitAdd = () => act(async () => { document.querySelector('.model-add-dialog form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
const input = (suffix: string) => document.querySelector<HTMLInputElement>('.model-add-dialog input[id$="-' + suffix + '"]');
const selectField = (suffix: string) => document.querySelector<HTMLSelectElement>('.model-add-dialog select[id$="-' + suffix + '"]');
const preset = (id = 'qwen-official-test'): ModelPreset => ({
  preset_id: id, model_id: id, label: '千问参考 ' + id, provider: '阿里云百炼', protocol: 'openai_chat', base_url: 'https://official.example/v1',
  capabilities: { context_max: 1000000, max_input: 983616, max_output: 8192, streaming: true, tool_calling: true },
  window_options: [128000, 200000, 400000, 1000000].map(tokens => ({ id: 'budget-' + tokens, tokens, kind: 'local_budget' })),
  default_window: 'budget-200000', reasoning: { kind: 'boolean', options: ['default', 'off', 'on'], default: 'off' },
  official_limits: { context_max: 1000000, max_input_off: 991808, max_input_on: 983616, max_output: 131072 },
  source: { kind: 'official_reference', url: 'https://official.example/specification', checked_at: '2026-10-08', gateway_context_verified: false },
});

it('从服务端读取选中模型与能力，切换时提交版本并刷新上下文', async () => {
  await mount(); expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('主选购模型');
  await open(); await click(host.querySelector('.model-picker-groups button:last-child'));
  await click([...host.querySelectorAll('.model-option')].find(item => item.textContent?.includes('轻量模型'))!);
  expect(calls.find(call => call.method === 'PATCH')?.body).toEqual({ profile_id: 'light', profile_revision: 2, window_id: 'budget-128000', thinking: 'default', expected_revision: 7 });
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('轻量模型');
  expect(host.querySelector('[role="dialog"]')).toBeNull(); expect(changed).toHaveBeenCalledTimes(1);
  await open(); expect(host.querySelector('[aria-label="轻量模型的思考设置"]')).toBeNull();
});

it('选择器搜索、方向键和 Escape 可用，按钮不会提交聊天表单', async () => {
  const submit = vi.fn();
  await act(async () => root.render(<form onSubmit={event => { event.preventDefault(); submit(); }}><ModelPicker sessionId="s1" request={request} busy={false} pending={false} /></form>));
  await open(); const search = host.querySelector<HTMLInputElement>('[aria-label="搜索模型"]')!;
  expect(document.activeElement).toBe(search);
  await fill(search, '轻量'); expect(host.querySelectorAll('.model-option')).toHaveLength(1);
  await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })));
  expect(document.activeElement).toBe(host.querySelector('.model-option'));
  await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(host.querySelector('.model-picker-panel')).toBeNull(); expect(document.activeElement).toBe(host.querySelector('.model-picker-trigger')); expect(submit).not.toHaveBeenCalled();
});

it('浮层按来源分组，搜索跨组，重开定位已选模型而不修改绑定', async () => {
  catalog.selection = { profile_id: 'light', profile_revision: 2, window_id: 'budget-64000', thinking: 'default' };
  await mount(); await open();
  expect(host.querySelector('.model-picker-groups [aria-selected="true"]')?.textContent).toBe('自定义');
  expect(host.querySelectorAll('.model-option')).toHaveLength(1);
  expect(host.querySelector('.model-option')?.getAttribute('aria-pressed')).toBe('true');
  const currentTab = host.querySelector('.model-picker-groups [aria-selected="true"]')!;
  await act(async () => currentTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true, cancelable: true })));
  expect(host.querySelector('.model-picker-groups [aria-selected="true"]')?.textContent).toBe('默认');
  expect(document.activeElement).toBe(host.querySelector('.model-picker-groups button'));
  await fill(host.querySelector('[aria-label="搜索模型"]'), '模型');
  expect(host.querySelectorAll('.model-option')).toHaveLength(2);
  expect(host.querySelector('.model-search-scope')?.textContent).toContain('全部模型');
  await click(host.querySelector('.model-picker-trigger')); await open();
  expect(host.querySelector<HTMLInputElement>('[aria-label="搜索模型"]')?.value).toBe('');
  expect(host.querySelector('.model-picker-groups [aria-selected="true"]')?.textContent).toBe('自定义');
  expect(calls.some(call => call.method === 'PATCH')).toBe(false);
});

it('浮层列表导航不拦截思考单选档位的原生方向键', async () => {
  await mount(); await open();
  const radio = host.querySelector<HTMLInputElement>('[aria-label="主选购模型的思考设置"] input:checked')!;
  const event = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
  await act(async () => { radio.focus(); radio.dispatchEvent(event); });
  expect(event.defaultPrevented).toBe(false);
  expect(document.activeElement).toBe(radio);
});

it('加载中打开浮层，目录到达后定位当前自定义模型', async () => {
  let finish: (data: ModelCatalog) => void = () => {};
  request.mockImplementationOnce(() => new Promise<ModelCatalog>(resolve => { finish = resolve; }));
  await mount(); await open();
  expect(host.querySelector('.model-empty')?.textContent).toContain('正在读取');
  catalog.selection = { profile_id: 'light', profile_revision: 2, window_id: 'budget-64000', thinking: 'default' };
  await act(async () => finish(catalog));
  expect(host.querySelector('.model-picker-groups [aria-selected="true"]')?.textContent).toBe('自定义');
  expect(host.querySelector('.model-option[aria-pressed="true"]')?.textContent).toContain('轻量模型');
});

it('浮层移除上限快捷配置，管理页仍可配置窗口且不改写会话', async () => {
  await mount(); await open();
  expect(host.querySelector('[aria-label="主选购模型的上下文窗口"]')).toBeNull();
  expect(host.querySelector('.model-current-controls')?.textContent).not.toContain('本次上限');
  expect(host.querySelector('.model-option[aria-pressed="true"] .model-option-window')?.textContent).toBe('128K');
  await click(host.querySelector('.model-manage-link'));
  expect(document.querySelector('.model-manager [aria-label="主选购模型的上下文窗口"]')).toBeTruthy();
  expect(calls.some(call => call.method === 'PATCH')).toBe(false);
});

it('思考档位单独更新，保留当前模型版本和窗口选择', async () => {
  catalog.selection = { ...catalog.selection!, window_id: 'budget-64000' };
  await mount(); await open();
  const group = host.querySelector('[role="radiogroup"][aria-label="主选购模型的思考设置"]')!;
  const options = [...group.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
  expect(options.map(option => option.value)).toEqual(['default', 'off', 'on']);
  expect(options.find(option => option.checked)?.value).toBe('on');
  await click(options.find(option => option.value === 'off')!);
  expect(calls.find(call => call.method === 'PATCH')?.body).toEqual({ profile_id: 'main', profile_revision: 2,
    thinking: 'off', window_id: 'budget-64000', expected_revision: 7 });
  await open();
  expect(host.querySelector<HTMLInputElement>('[aria-label="主选购模型的思考设置"] input:checked')?.value).toBe('off');
});

it('单档思考显示已选值但不可重复提交', async () => {
  catalog.models[0].reasoning.options = ['on'];
  await mount(); await open();
  const options = [...host.querySelectorAll<HTMLInputElement>('[aria-label="主选购模型的思考设置"] input[type="radio"]')];
  expect(options).toHaveLength(1);
  expect(options[0].checked).toBe(true);
  expect(options[0].disabled).toBe(true);
  await click(options[0]);
  expect(calls.some(call => call.method === 'PATCH')).toBe(false);
});

it('保存档位时禁用其它选项，避免并发覆盖当前选择', async () => {
  let finish: () => void = () => {};
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    if (path.endsWith('/model') && method === 'PATCH') await new Promise<void>(resolve => { finish = resolve; });
    return original(path, method, body);
  });
  await mount(); await open();
  await click(host.querySelector('[aria-label="主选购模型的思考设置"] input[value="off"]'));
  const options = [...host.querySelectorAll<HTMLInputElement>('.model-current-controls input[type="radio"]')];
  expect(options).toHaveLength(3);
  expect(options.every(option => option.disabled)).toBe(true);
  await click(host.querySelector('[aria-label="主选购模型的思考设置"] input[value="off"]'));
  await act(async () => finish());
  expect(calls.filter(call => call.method === 'PATCH')).toHaveLength(1);
  expect(calls.find(call => call.method === 'PATCH')?.body).toMatchObject({ thinking: 'off', window_id: 'budget-128000' });
  expect(host.querySelector('.model-picker-panel')).toBeNull();
});

it('待审批时展示下一轮选择，不把新模型冒充正在运行的模型', async () => {
  catalog.active_selection = { ...catalog.selection!, label: '主选购模型' };
  catalog.pending_selection = { profile_id: 'light', profile_revision: 2, window_id: 'budget-64000', thinking: 'default' };
  await mount({ pending: true }); await open();
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('轻量模型');
  expect(host.querySelector('.model-selection-note')?.textContent).toContain('待确认操作继续使用原模型');
  expect(host.querySelector('.model-selection-note')?.textContent).toContain('待确认操作：主选购模型');
  expect(host.querySelector('.model-selection-note')?.textContent).toContain('下一轮：轻量模型');
  expect(host.querySelector('[aria-label="轻量模型的上下文窗口"]')).toBeNull();
  expect(host.querySelector('.model-current-controls')).toBeNull();
});

it('模型管理保留内置/自定义区分和未支持思考，偏好提交独立于会话绑定', async () => {
  await mount(); await manage();
  expect(document.querySelector('.model-manager')?.textContent).not.toContain('轻量模型');
  await click(document.querySelector('[role="switch"]'));
  await fill(document.querySelector<HTMLInputElement>('.model-manager [aria-label="主选购模型的上下文窗口"]'), '0');
  await click(document.querySelector('.model-manager .model-button-primary'));
  expect(calls.find(call => call.path.endsWith('/preferences'))?.body).toEqual({ visible: false, default_window: 'budget-64000', thinking: 'default', expected_revision: 2 });
  expect(calls.filter(call => call.path.endsWith('/model'))).toHaveLength(0);
  await click([...document.querySelectorAll('[role="tab"]')].find(item => item.textContent === '自定义')!);
  expect(document.querySelector('.model-manager')?.textContent).toContain('轻量模型');
  expect(document.querySelector('.model-unsupported')?.textContent).toBe('不支持');
});

it('自定义连接先校验再用凭证保存，密钥不写浏览器存储，弹窗不会嵌套聊天表单', async () => {
  const submit = vi.fn();
  await act(async () => root.render(<form onSubmit={event => { event.preventDefault(); submit(); }}><ModelPicker sessionId="s1" request={request} busy={false} pending={false} /></form>));
  await add(); await fill(input('url'), 'https://example.test/v1'); await fill(input('model'), 'my-model'); await fill(input('key'), 'private-test-key');
  expect(document.querySelectorAll('form form')).toHaveLength(0); expect(input('key')?.type).toBe('password');
  await submitAdd();
  const writes = calls.filter(call => call.path.startsWith('/model-connections'));
  expect(writes.map(call => call.path)).toEqual(['/model-connections/validate', '/model-connections']);
  expect(writes[0].body).toMatchObject({ api_key: 'private-test-key', protocol: 'openai_chat', reasoning: { kind: 'unsupported', options: ['default'] } });
  expect(writes[1].body).toMatchObject({ validation_ticket: 'test-ticket', config_hash: 'safe-hash', model_id: 'my-model' });
  expect(localStorage.length).toBe(0); expect(document.body.textContent).not.toContain('private-test-key'); expect(submit).not.toHaveBeenCalled();
  expect(document.querySelector('.model-manager')).toBeTruthy();
});

it('自定义校验失败保留输入、隐藏并脱敏密钥，不保存未经校验的配置', async () => {
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    if (path === '/model-connections/validate') throw new Error('无效凭据 private-test-key');
    return original(path, method, body);
  });
  await mount(); await add(); await fill(input('url'), 'https://example.test/v1'); await fill(input('model'), 'my-model'); await fill(input('key'), 'private-test-key'); await submitAdd();
  expect(input('model')?.value).toBe('my-model'); expect(input('key')?.value).toBe('private-test-key'); expect(input('key')?.type).toBe('password');
  expect(document.querySelector('.model-error')?.textContent).toContain('无效凭据 ••••••••'); expect(calls.some(call => call.path === '/model-connections')).toBe(false);
});

it('窗口声明不合理时本地拦截，不发探针', async () => {
  await mount(); await add(); await fill(input('url'), 'https://example.test/v1'); await fill(input('model'), 'my-model'); await fill(input('context'), '64000'); await submitAdd();
  expect(document.querySelector('.model-error')?.textContent).toContain('不超过模型最大窗口');
  expect(calls.some(call => call.path.startsWith('/model-connections'))).toBe(false);
});

it('校验没有返回完整凭证时不允许保存', async () => {
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => path === '/model-connections/validate' ? { validation_ticket: 'unbound' } : original(path, method, body));
  await mount(); await add(); await fill(input('url'), 'https://example.test/v1'); await fill(input('model'), 'my-model'); await submitAdd();
  expect(document.querySelector('.model-error')?.textContent).toContain('有效凭证');
  expect(calls.some(call => call.path === '/model-connections')).toBe(false);
});

it('切换会话时晚到的旧模型目录不会覆盖新会话', async () => {
  let finish: (data: Record<string, unknown>) => void = () => {};
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => path.endsWith('old') ? new Promise(resolve => { finish = resolve; }) : original(path, method, body));
  await mount({ sessionId: 'old' }); expect(host.textContent).toContain('读取模型');
  await mount({ sessionId: 'new' }); await act(async () => finish({ ...catalog, selection: { profile_id: 'light', window_id: 'budget-64000', thinking: 'default' } }));
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('主选购模型');
});

it('普通买家可以修改自己的偏好，但新增与编辑连接仍需管理员权限', async () => {
  catalog.management_allowed = false; await mount(); await manage();
  expect(document.querySelector<HTMLButtonElement>('.model-add-link')?.disabled).toBe(true);
  expect(document.querySelector<HTMLButtonElement>('[role="switch"]')?.disabled).toBe(false);
  await click(document.querySelector('[role="switch"]'));
  await fill(document.querySelector<HTMLInputElement>('.model-manager [aria-label="主选购模型的上下文窗口"]'), '0');
  await click(document.querySelector('.model-manager .model-button-primary'));
  expect(calls.find(call => call.path.endsWith('/preferences'))?.body).toMatchObject({ visible: false, default_window: 'budget-64000', expected_revision: 2 });
  await click([...document.querySelectorAll('[role="tab"]')].find(item => item.textContent === '自定义')!);
  expect(document.querySelector<HTMLButtonElement>('.model-edit-link')?.disabled).toBe(true);
  expect(document.querySelector('.model-manager')?.textContent).toContain('添加或编辑连接需要本机或管理员权限');
  expect(calls.some(call => call.path.startsWith('/model-connections'))).toBe(false);
});

it('普通买家能独立设置新会话默认，不获得连接管理权限', async () => {
  catalog.management_allowed = false; catalog.default_revision = 3;
  await mount(); await open();
  expect(host.querySelector<HTMLButtonElement>('.model-default-action')?.disabled).toBe(false);
  await click(host.querySelector('.model-default-action'));
  expect(calls.find(call => call.path === '/models/default')?.body).toEqual({ profile_id: 'main', window_id: 'budget-128000', thinking: 'on', expected_revision: 3 });
  expect(host.querySelector('.model-notice')?.textContent).toContain('已有会话保持原选择');
  await click(host.querySelector('.model-manage-link'));
  expect(document.querySelector<HTMLButtonElement>('.model-add-link')?.disabled).toBe(true);
});

it('加载失败不伪造默认型号，重新加载可恢复', async () => {
  const original = request.getMockImplementation()!; request.mockRejectedValueOnce(new Error('连接中断'));
  await mount(); await open(); expect(host.querySelector('.model-error')?.textContent).toContain('连接中断');
  expect(host.querySelectorAll('.model-option')).toHaveLength(0);
  request.mockImplementation(original); await click(host.querySelector('.model-error button'));
  expect(host.querySelector('.model-error')).toBeNull(); expect(host.querySelectorAll('.model-option')).toHaveLength(1);
});

it('窗口格式只反映实际数值，不把未知窗口写成零', () => {
  expect(formatModelWindow(128000)).toBe('128K'); expect(formatModelWindow(1000000)).toBe('1M'); expect(formatModelWindow(0)).toBe('未知');
});

it('新会话默认独立提交版本，不更改当前会话绑定', async () => {
  catalog.default_revision = 4; await mount(); await open(); await click(host.querySelector('.model-default-action'));
  expect(calls.find(call => call.path === '/models/default')?.body).toEqual({ profile_id: 'main', window_id: 'budget-128000', thinking: 'on', expected_revision: 4 });
  expect(calls.filter(call => call.path.endsWith('/model'))).toHaveLength(0);
  expect(host.querySelector('.model-notice')?.textContent).toContain('已有会话保持原选择');
  expect(host.querySelector<HTMLButtonElement>('.model-default-action')?.disabled).toBe(true);
});

async function editCustom() {
  await mount(); await manage(); await click([...document.querySelectorAll('[role="tab"]')].find(item => item.textContent === '自定义')!);
  await click(document.querySelector('.model-edit-link'));
}
it('编辑自定义连接携带版本，原密钥不回显，空值保留', async () => {
  catalog.models[1] = { ...catalog.models[1], base_url: 'https://old.test/v1', key_configured: true };
  await editCustom(); expect(input('model')?.value).toBe('light'); expect(input('key')?.value).toBe('');
  expect(input('key')?.placeholder).toContain('留空保留'); await submitAdd();
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).toMatchObject({ profile_id: 'light', expected_revision: 2, api_key: '' });
  expect(calls.find(call => call.path === '/model-connections')?.body).toMatchObject({ profile_id: 'light', validation_ticket: 'test-ticket' });
});

it('编辑服务地址时不允许空密钥复用，避免凭据发送到新服务', async () => {
  catalog.models[1] = { ...catalog.models[1], base_url: 'https://old.test/v1', key_configured: true };
  await editCustom(); await fill(input('url'), 'https://new.test/v1'); await submitAdd();
  expect(document.querySelector('.model-error')?.textContent).toContain('请填写对应的新 API Key');
  expect(calls.some(call => call.path.startsWith('/model-connections'))).toBe(false);
});

it('连续点击只提交一次选择，服务端失败后保留旧模型且允许重试', async () => {
  let fail: (reason: Error) => void = () => {};
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => path.endsWith('/model') ? new Promise((_, reject) => { fail = reject; }) : original(path, method, body));
  await mount(); await open(); await click(host.querySelector('.model-picker-groups button:last-child'));
  const target = [...host.querySelectorAll<HTMLButtonElement>('.model-option')].find(item => item.textContent?.includes('轻量模型'))!;
  await act(async () => { target.click(); target.click(); });
  expect(request.mock.calls.filter(([path]) => String(path).endsWith('/model'))).toHaveLength(1); expect(target.disabled).toBe(true);
  await act(async () => fail(new Error('新模型容量不足，请先整理')));
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('主选购模型'); expect(host.querySelector('.model-error')?.textContent).toContain('容量不足');
  expect(target.disabled).toBe(false); expect(changed).not.toHaveBeenCalled();
});

it('运行快照的旧型号名称和窗口不会被目录最新版本覆盖', async () => {
  catalog.selection = { ...catalog.selection!, profile_revision: 1, label: '旧型号配置', context_size: 64000 };
  catalog.active_selection = { ...catalog.selection };
  await mount({ busy: true }); await open();
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('旧型号配置');
  expect(host.textContent).toContain('当前回复：旧型号配置'); expect(host.textContent).toContain('保留第 1 版会话设置 · 64K');
  expect(host.querySelector('.model-current-controls')).toBeNull();
  expect(host.querySelector('.model-option[aria-pressed="true"]')).toBeNull();
});

it('模型搜索框回车被拦截，向上键从末项开始导航', async () => {
  await mount(); await open(); const search = host.querySelector<HTMLInputElement>('[aria-label="搜索模型"]')!;
  const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  await act(async () => search.dispatchEvent(event)); expect(event.defaultPrevented).toBe(true);
  await fill(search, '模型');
  await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true, cancelable: true })));
  expect(document.activeElement?.textContent).toContain('轻量模型');
});

it('空闲会话的待生效选择不误称正在生成，人工容量只称声明上限', async () => {
  catalog.pending_selection = { ...catalog.selection!, window_id: 'budget-64000' };
  catalog.active_selection = { ...catalog.selection!, label: '旧模型' };
  await mount(); await open();
  expect(host.querySelector('.model-selection-note')?.textContent).toContain('已保存，将用于下一次回复');
  expect(host.querySelector('.model-selection-note')?.textContent).not.toContain('正在进行');
  expect(host.querySelector('.model-selection-note')?.textContent).not.toContain('当前回复');
  await click(host.querySelector('.model-manage-link'));
  expect(document.querySelector('.model-row-window small')?.textContent).toContain('声明上限');
});

it('官方型号与容量全部从目录预设填入，默认 200K 和关闭思考并独立保留 8192 输出', async () => {
  catalog.presets = [preset(), preset('another-official-id')];
  await mount(); await add(); await fill(selectField('preset'), 'qwen-official-test');
  expect([...selectField('preset')!.options].map(option => option.value)).toEqual(['', 'qwen-official-test', 'another-official-id']);
  expect(input('model')?.value).toBe('qwen-official-test'); expect(input('url')?.value).toBe('https://official.example/v1');
  expect(input('context')?.value).toBe('1000000'); expect(input('input')?.value).toBe('983616'); expect(input('output')?.value).toBe('8192');
  expect(selectField('default-window')?.value).toBe('budget-200000'); expect(selectField('default-thinking')?.value).toBe('off');
  expect([...selectField('default-window')!.options].map(option => option.textContent)).toEqual(['128K', '200K', '400K', '1M']);
  expect(document.querySelector('.model-preset-reference')?.textContent).toContain('当前网关尚未验证大窗口容量');
  expect(document.querySelector('.model-preset-reference')?.textContent).toContain('核对于 2026-10-08');
  expect(document.querySelector<HTMLAnchorElement>('.model-preset-reference a')?.href).toBe('https://official.example/specification');
  await fill(input('key'), 'new-private-key'); await submitAdd();
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).toMatchObject({
    preset_id: 'qwen-official-test', capabilities: { context_max: 1000000, max_input: 983616, max_output: 8192 },
    default_window: 'budget-200000', reasoning: { default: 'off' },
  });
});

it('只有明确勾选才引用网关凭据，并锁定来源端点与供应商且不回显密钥', async () => {
  catalog.presets = [preset()];
  catalog.models[0] = { ...catalog.models[0], base_url: 'https://gateway.example/v1', key_configured: true, profile_revision: 9 };
  await mount(); await add(); await fill(selectField('preset'), 'qwen-official-test'); await fill(input('key'), 'temporary-key');
  expect(document.querySelector<HTMLInputElement>('.model-borrow-toggle input')?.checked).toBe(false);
  await click(document.querySelector('.model-borrow-toggle input'));
  expect(input('key')).toBeNull(); expect(input('url')?.disabled).toBe(true); expect(selectField('provider')?.disabled).toBe(true);
  expect(input('url')?.value).toBe('https://gateway.example/v1'); expect(selectField('provider')?.value).toBe('测试网关');
  await click(document.querySelector('.model-test-button'));
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).toMatchObject({
    credential_profile_id: 'main', credential_profile_revision: 9, api_key: '', provider: '测试网关', base_url: 'https://gateway.example/v1',
  });
  expect(calls.some(call => call.path === '/model-connections')).toBe(false);
  expect(changed).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0);
  expect(document.body.textContent).not.toContain('temporary-key');
  expect(document.querySelector('.model-test-result')?.textContent).toContain('网关未返回型号，身份未知');
});

it('切换预设不会把借用密钥转发到官方地址，取消借用移除凭据引用', async () => {
  catalog.presets = [preset(), preset('another-model')];
  catalog.models[0] = { ...catalog.models[0], base_url: 'https://gateway.example/v1', key_configured: true };
  await mount(); await add(); await click(document.querySelector('.model-borrow-toggle input'));
  await fill(selectField('preset'), 'another-model');
  expect(input('url')?.value).toBe('https://gateway.example/v1'); expect(input('key')).toBeNull();
  await click(document.querySelector('.model-borrow-toggle input')); expect(input('url')?.disabled).toBe(false);
  expect(input('key')?.value).toBe('');
  await fill(input('url'), 'https://new.example/v1'); await fill(input('key'), 'new-key');
  await click(document.querySelector('.model-test-button'));
  const body = calls.find(call => call.path === '/model-connections/validate')?.body;
  expect(body).toMatchObject({ api_key: 'new-key', base_url: 'https://new.example/v1', preset_id: 'another-model' });
  expect(body).not.toHaveProperty('credential_profile_id'); expect(body).not.toHaveProperty('credential_profile_revision');
});

it('独立测试显示身份和时间但不保存，改动输入使成功失效，保存重新校验新草稿', async () => {
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    const result = await original(path, method, body);
    return path === '/model-connections/validate' ? { ...result, validation: { model_identity_matches: true, returned_models: [body!.model_id], tested_at: '2026-10-08T02:00:00Z' } } : result;
  });
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1'); await fill(input('model'), 'custom-model');
  await click(document.querySelector('.model-test-button'));
  expect(document.querySelector('.model-test-result')?.textContent).toContain('连接测试通过 · 尚未保存');
  expect(document.querySelector('.model-test-result')?.textContent).toContain('返回型号标识一致');
  expect(document.querySelector('.model-test-result')?.textContent).toContain('custom-model');
  expect(document.querySelector('.model-test-result')?.textContent).toContain('2026-10-08T02:00:00Z');
  expect(calls.some(call => call.path === '/model-connections')).toBe(false);
  await fill(input('output'), '10000'); expect(document.querySelector('.model-test-result')).toBeNull();
  await submitAdd();
  expect(calls.filter(call => call.path === '/model-connections/validate')).toHaveLength(2);
  expect(calls.find(call => call.path === '/model-connections')?.body).toMatchObject({ capabilities: { max_output: 10000 } });
});

it('连接测试失败保留草稿和脱敏错误，不保存，修改配置后清理过期失败信息', async () => {
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    if (path === '/model-connections/validate') throw new Error('探测失败 secret-example');
    return original(path, method, body);
  });
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1'); await fill(input('model'), 'custom-model'); await fill(input('key'), 'secret-example');
  await click(document.querySelector('.model-test-button'));
  expect(input('model')?.value).toBe('custom-model'); expect(input('key')?.type).toBe('password');
  expect(document.querySelector('.model-error')?.textContent).toBe('探测失败 ••••••••');
  expect(document.querySelector('.model-test-result')).toBeNull(); expect(calls.some(call => call.path === '/model-connections')).toBe(false);
  await fill(input('model'), 'correct-model'); expect(document.querySelector('.model-error')).toBeNull();
});

it('修改预设型号后移除官方引用，不把其他型号冒充经过官方核对的配置', async () => {
  catalog.presets = [preset()]; await mount(); await add(); await fill(selectField('preset'), 'qwen-official-test');
  await fill(input('model'), 'my-custom-model');
  expect(selectField('preset')?.value).toBe(''); expect(document.querySelector('.model-preset-reference')).toBeNull();
  await click(document.querySelector('.model-test-button'));
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).not.toHaveProperty('preset_id');
});

it('编辑保留独立输入上限，输入不能超过上下文声明上限', async () => {
  catalog.models[1] = { ...catalog.models[1], base_url: 'https://existing.example/v1', key_configured: true,
    capabilities: { ...catalog.models[1].capabilities, max_input: 115000 } };
  await editCustom(); expect(input('input')?.value).toBe('115000');
  await click(document.querySelector('.model-test-button'));
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).toMatchObject({ capabilities: { max_input: 115000 } });
  await fill(input('input'), '150000'); await submitAdd();
  expect(document.querySelector('.model-error')?.textContent).toContain('输入上限需为正整数');
  expect(calls.filter(call => call.path === '/model-connections/validate')).toHaveLength(1);
  await fill(input('input'), '110000'); await submitAdd();
  expect(calls.find(call => call.path === '/model-connections')?.body).toMatchObject({ capabilities: { max_input: 110000 } });
});

it('普通自定义连接的网关型号不一致时明确提示，不能误称身份一致', async () => {
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    const result = await original(path, method, body);
    return path === '/model-connections/validate' ? { ...result, validation: { model_identity_matches: false, returned_models: ['other-model'] } } : result;
  });
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1'); await fill(input('model'), 'my-model');
  await click(document.querySelector('.model-test-button'));
  expect(document.querySelector('.model-test-result')?.textContent).toContain('返回型号与请求不一致');
  expect(document.querySelector('.model-test-result')?.textContent).not.toContain('返回型号标识一致');
  expect(calls.some(call => call.path === '/model-connections')).toBe(false);
});

it('测试进行中锁定编辑和保存，同步重复点击只发一个探针', async () => {
  let finish: (result: Record<string, unknown>) => void = () => {};
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => path === '/model-connections/validate'
    ? new Promise(resolve => { finish = resolve; }) : original(path, method, body));
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1'); await fill(input('model'), 'my-model');
  const button = document.querySelector<HTMLButtonElement>('.model-test-button')!;
  await act(async () => { button.click(); button.click(); });
  expect(request.mock.calls.filter(([path]) => path === '/model-connections/validate')).toHaveLength(1);
  expect(document.querySelector<HTMLFieldSetElement>('.model-add-dialog fieldset')?.disabled).toBe(true);
  expect(document.querySelector<HTMLButtonElement>('.model-add-dialog .model-button-primary')?.disabled).toBe(true);
  await act(async () => finish({ validation_ticket: 'ticket', config_hash: 'hash' }));
  expect(document.querySelector('.model-test-result')).not.toBeNull();
  expect(calls.some(call => call.path === '/model-connections')).toBe(false);
});

it('切换会话后的旧连接测试不会写入新会话或重新打开旧弹窗', async () => {
  let finish: (result: Record<string, unknown>) => void = () => {};
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => path === '/model-connections/validate'
    ? new Promise(resolve => { finish = resolve; }) : original(path, method, body));
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1'); await fill(input('model'), 'my-model');
  await click(document.querySelector('.model-test-button')); await mount({ sessionId: 'new-session' });
  await act(async () => finish({ validation_ticket: 'ticket', config_hash: 'hash' }));
  expect(document.querySelector('.model-add-dialog')).toBeNull(); expect(document.querySelector('.model-test-result')).toBeNull();
  expect(calls.some(call => call.path === '/model-connections')).toBe(false); expect(changed).not.toHaveBeenCalled();
});

it('管理页使用紧凑默认页签和 API 副标题，分组依赖目录而不是 builtin 字段', async () => {
  catalog.models[1] = { ...catalog.models[1], catalog_group: 'default', label: 'Qwen3.7-Plus', model_id: 'qwen3.7-plus' };
  catalog.models.push(profile('private', { label: '个人连接', builtin: true, catalog_group: 'custom' }));
  await mount(); await manage();
  expect(document.querySelector('.model-manager .model-dialog-heading')).toBeNull();
  expect(document.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('默认');
  expect(document.querySelector('.model-manager')?.textContent).toContain('Qwen3.7-Plus');
  expect(document.querySelector('.model-manager')?.textContent).toContain('API · qwen3.7-plus');
  expect(document.querySelector('.model-manager')?.textContent).not.toContain('个人连接');
  await click([...document.querySelectorAll('[role="tab"]')].find(item => item.textContent === '自定义')!);
  expect(document.querySelector('.model-manager')?.textContent).toContain('个人连接');
  expect(document.querySelector('.model-manager')?.textContent).not.toContain('Qwen3.7-Plus');
});

it('窗口滑杆按离散档位映射并支持方向键和首尾键，不伪造中间容量', async () => {
  catalog.models[0] = { ...catalog.models[0], window_options: preset().window_options, default_window: 'budget-200000' };
  await mount(); await manage();
  const slider = document.querySelector<HTMLInputElement>('.model-window-slider input')!;
  expect(slider.type).toBe('range'); expect(slider.step).toBe('1'); expect(slider.min).toBe('0'); expect(slider.max).toBe('3');
  expect(slider.getAttribute('aria-valuetext')).toBe('200K，本次上限');
  await act(async () => slider.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true })));
  expect(slider.getAttribute('aria-valuetext')).toBe('400K，本次上限');
  await act(async () => slider.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true, cancelable: true })));
  expect(slider.getAttribute('aria-valuetext')).toBe('1M，本次上限');
  await act(async () => slider.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true, cancelable: true })));
  expect(slider.getAttribute('aria-valuetext')).toBe('128K，本次上限');
  await click(document.querySelector('.model-manager .model-button-primary'));
  expect(calls.find(call => call.path.endsWith('/preferences'))?.body).toMatchObject({ default_window: 'budget-128000' });
});

it('单档窗口禁用滑杆，取消管理页丢弃未保存的偏好', async () => {
  catalog.models[0] = { ...catalog.models[0], window_options: [{ id: 'only-window', tokens: 128000, kind: 'local_budget' }], default_window: 'only-window' };
  await mount(); await manage();
  expect(document.querySelector<HTMLInputElement>('.model-window-slider input')?.disabled).toBe(true);
  await click(document.querySelector('[role="switch"]'));
  await click(document.querySelector('.model-manager .model-button-secondary'));
  await manage();
  expect(document.querySelector('[role="switch"]')?.getAttribute('aria-checked')).toBe('true');
  expect(calls.some(call => call.path.endsWith('/preferences'))).toBe(false);
});

it('隐藏重复和验收连接不会重新列出；旧绑定仅规范显示名，重选才提交新版本', async () => {
  catalog.models[0] = { ...catalog.models[0], catalog_hidden: true, canonical_profile_id: 'canonical', label: '重复 Flash' };
  catalog.models.push(profile('canonical', { profile_revision: 8, label: 'Qwen3.8-Flash', model_id: 'qwen3.8-flash', catalog_group: 'default' }));
  catalog.models.push(profile('test-entry', { catalog_hidden: true, label: '验收测试连接', builtin: false }));
  catalog.selection = { ...catalog.selection!, label: '历史验收名称', context_size: 64000 };
  catalog.active_selection = { ...catalog.selection };
  await mount({ busy: true }); await open();
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('Qwen3.8-Flash');
  expect(host.textContent).toContain('当前回复：Qwen3.8-Flash'); expect(host.textContent).toContain('此会话沿用历史连接');
  expect(host.querySelector('.model-current-controls')).toBeNull(); expect(host.querySelector('.model-default-action')).toBeNull();
  expect([...host.querySelectorAll('.model-option')].map(item => item.textContent).join('')).not.toMatch(/重复 Flash|验收测试连接/);
  expect(host.querySelector('.model-option[aria-pressed="true"]')).toBeNull();
  expect(calls.some(call => call.method === 'PATCH')).toBe(false);
  await click(host.querySelector('.model-manage-link'));
  expect(document.querySelector('.model-table')?.textContent).not.toMatch(/重复 Flash|验收测试连接/);
  await click(document.querySelector('.model-manager .model-button-secondary')); await open();
  await click([...host.querySelectorAll('.model-option')].find(item => item.textContent?.includes('Qwen3.8-Flash'))!);
  expect(calls.find(call => call.path.endsWith('/model'))?.body).toMatchObject({ profile_id: 'canonical', profile_revision: 8 });
});

const virtual = (): ModelProfile => ({
  ...profile('preset-qwen3.8-max'), ...preset('qwen3.8-max'), profile_id: 'preset-qwen3.8-max', profile_revision: 0,
  builtin: true, label: 'Qwen3.8-Max', status: 'unconfigured', is_virtual: true, catalog_group: 'default',
  base_url: 'https://gateway.example/v1', provider: '测试网关',
  connection_profile_id: 'main', connection_profile_revision: 2,
  unavailable_reason: '尚未校验当前网关连接，请先连接',
});

it('未连接型号显示不可用并禁用偏好写入，连接先校验再保存且不提交虚拟 ID', async () => {
  catalog.models.push(virtual());
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    const result = await original(path, method, body);
    if (path === '/model-connections') catalog.models = catalog.models.map(model => model.is_virtual
      ? { ...model, profile_id: 'real-max', profile_revision: 1, is_virtual: false, status: 'active', unavailable_reason: undefined } : model);
    return result;
  });
  await mount(); await open();
  const candidate = [...host.querySelectorAll<HTMLButtonElement>('.model-option')].find(item => item.textContent?.includes('Qwen3.8-Max'))!;
  expect(candidate.disabled).toBe(true); expect(candidate.textContent).toContain('尚未校验');
  await click(host.querySelector('.model-manage-link'));
  const row = [...document.querySelectorAll('.model-table-row')].find(item => item.textContent?.includes('Qwen3.8-Max'))!;
  expect(row.querySelector<HTMLButtonElement>('[role="switch"]')?.disabled).toBe(true);
  expect(row.querySelector<HTMLInputElement>('input[type="range"]')?.disabled).toBe(true);
  expect(row.querySelector<HTMLSelectElement>('select')?.disabled).toBe(true);
  await click(row.querySelector('.model-connect-link'));
  const writes = calls.filter(call => call.path.startsWith('/model-connections'));
  expect(writes.map(call => call.path)).toEqual(['/model-connections/validate', '/model-connections']);
  expect(writes[0].body).toMatchObject({ preset_id: 'qwen3.8-max', credential_profile_id: 'main', credential_profile_revision: 2,
    model_id: 'qwen3.8-max', label: 'Qwen3.8-Max', base_url: 'https://gateway.example/v1', api_key: '' });
  expect(writes[0].body).not.toHaveProperty('profile_id'); expect(writes[0].body).not.toHaveProperty('expected_revision');
  expect(writes[1].body).toMatchObject({ validation_ticket: 'test-ticket', config_hash: 'safe-hash' });
  expect(document.querySelector('.model-connect-link')).toBeNull();
  expect(calls.some(call => call.path.endsWith('/preferences'))).toBe(false);
});

it('虚拟型号连接失败保持不可选并提供重试，不能把失败当作已激活', async () => {
  catalog.models.push(virtual());
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    if (path === '/model-connections/validate') throw new Error('当前网关限流，请稍后重试');
    return original(path, method, body);
  });
  await mount(); await manage(); await click(document.querySelector('.model-connect-link'));
  expect(document.querySelector('.model-error')?.textContent).toContain('当前网关限流');
  expect(document.querySelector('.model-connect-link')?.textContent).toBe('重试连接');
  expect(calls.some(call => call.path === '/model-connections')).toBe(false);
  await click(document.querySelector('.model-manager .model-button-secondary')); await open();
  expect([...host.querySelectorAll<HTMLButtonElement>('.model-option')].find(item => item.textContent?.includes('Qwen3.8-Max'))?.disabled).toBe(true);
});

it('普通买家无法激活虚拟型号，已失败条目保留可见原因', async () => {
  catalog.management_allowed = false;
  catalog.models.push({ ...virtual(), status: 'validation_failed', unavailable_reason: '服务商暂时限流' });
  await mount(); await manage();
  expect(document.querySelector<HTMLButtonElement>('.model-connect-link')?.disabled).toBe(true);
  expect(document.querySelector('.model-table')?.textContent).toContain('服务商暂时限流');
  expect(calls.some(call => call.path.startsWith('/model-connections'))).toBe(false);
});

it('展示名称在主表单自动填写且允许独立命名，API 改名不覆盖手填名称', async () => {
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1');
  expect(input('label')?.closest('details')).toBeNull();
  await fill(input('model'), 'qwen-example-id'); expect(input('label')?.value).toBe('qwen-example-id');
  await fill(input('label'), '我的选购模型'); await fill(input('model'), 'other-api-id');
  expect(input('label')?.value).toBe('我的选购模型');
  await click(document.querySelector('.model-test-button'));
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).toMatchObject({ label: '我的选购模型', model_id: 'other-api-id' });
});

it('当前网关被去重后优先使用规范连接作凭据来源，不重复展示历史源', async () => {
  catalog.models[0] = { ...catalog.models[0], profile_id: 'current-gateway', catalog_hidden: true, canonical_profile_id: 'canonical',
    key_configured: true, base_url: 'https://gateway.example/v1' };
  catalog.selection = { ...catalog.selection!, profile_id: 'current-gateway' };
  catalog.models.push(profile('canonical', { label: 'Qwen3.8-Flash', key_configured: true, base_url: 'https://gateway.example/v1' }));
  await mount(); await add(); await click(document.querySelector('.model-borrow-toggle input'));
  expect([...selectField('connection')!.options].map(option => option.value)).toEqual(['canonical']);
  expect(selectField('connection')?.value).toBe('canonical');
  await fill(input('model'), 'qwen-example'); await click(document.querySelector('.model-test-button'));
  expect(calls.find(call => call.path === '/model-connections/validate')?.body).toMatchObject({ credential_profile_id: 'canonical' });
});

it('服务端复用已有模型时不误报新增成功', async () => {
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (path: string, method?: string, body?: Record<string, unknown>) => {
    const result = await original(path, method, body);
    return path === '/model-connections' ? { ...result, reused: true } : result;
  });
  await mount(); await add(); await fill(input('url'), 'https://custom.example/v1'); await fill(input('model'), 'existing-model'); await submitAdd();
  expect(document.querySelector('.model-notice')?.textContent).toBe('已校验，使用已有模型。');
});

it('旧快照与目录仍为同一 API 型号时统一展示名，不改运行版本', async () => {
  catalog.models[0] = { ...catalog.models[0], label: 'Qwen3.8-Flash', model_id: 'qwen3.8-flash' };
  catalog.selection = { ...catalog.selection!, profile_revision: 1, label: 'qwen3.8-flash', model_id: 'qwen3.8-flash' };
  catalog.active_selection = { ...catalog.selection };
  await mount({ busy: true }); await open();
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('Qwen3.8-Flash');
  expect(host.textContent).toContain('当前回复：Qwen3.8-Flash');
  expect(host.textContent).toContain('保留第 1 版会话设置');
  expect(host.querySelector('.model-current-controls')).toBeNull();
  expect(calls.some(call => call.method === 'PATCH')).toBe(false);
});

it('同一连接的新版本已更换 API 型号时保留旧快照名称，不能冒充新型号', async () => {
  catalog.models[0] = { ...catalog.models[0], label: 'Qwen3.8-Max', model_id: 'qwen3.8-max' };
  catalog.selection = { ...catalog.selection!, profile_revision: 1, label: 'Qwen3.8-Flash', model_id: 'qwen3.8-flash' };
  catalog.active_selection = { ...catalog.selection };
  await mount({ busy: true }); await open();
  expect(host.querySelector('.model-picker-trigger')?.textContent).toContain('Qwen3.8-Flash');
  expect(host.querySelector('.model-picker-trigger')?.textContent).not.toContain('Qwen3.8-Max');
  expect(host.textContent).toContain('当前回复：Qwen3.8-Flash');
  expect(host.querySelector('.model-current-controls')).toBeNull();
  expect(calls.some(call => call.method === 'PATCH')).toBe(false);
});
