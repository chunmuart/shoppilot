import { useCallback, useEffect, useLayoutEffect, useId, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import type { WorkspaceRequest } from './BuyerWorkspace';
import Modal from './Modal';
import Icon from './Icon';
import {
  formatModelWindow, modelAvailable, modelPreference, readModelCatalog, thinkingLabel,
  type ModelCatalog, type ModelPreference, type ModelPreset, type ModelProfile, type ModelSelection,
} from '../lib/modelCatalog';
import './modelPicker.css';

export type ModelWorkspaceOptions = {
  sessionId: string; request: WorkspaceRequest; busy: boolean; pending: boolean;
  onChanged?: () => void | Promise<void>;
  draft?: { value: ModelSelection; onChange: (selection: ModelSelection) => void };
  disabled?: boolean;
};

export function useModelWorkspace({ sessionId, request, busy, onChanged, draft }: ModelWorkspaceOptions) {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const generation = useRef(0), readVersion = useRef(0), lock = useRef(false);
  const changed = useRef(onChanged); changed.current = onChanged;
  const refresh = useCallback(async (current = generation.current) => {
    const read = ++readVersion.current;
    try {
      const data = readModelCatalog(await request('/models?session_id=' + encodeURIComponent(sessionId)));
      if (generation.current === current && readVersion.current === read) { setCatalog(data); setError(''); }
    } catch (e) {
      if (generation.current === current && readVersion.current === read) setError(e instanceof Error ? e.message : '模型列表暂时无法读取');
    } finally {
      if (generation.current === current && readVersion.current === read) setLoading(false);
    }
  }, [request, sessionId]);
  useEffect(() => {
    const current = ++generation.current;
    setCatalog(null); setLoading(true); setSaving(false); setError(''); setNotice(''); lock.current = false;
    void refresh(current);
    return () => { ++generation.current; ++readVersion.current; };
  }, [refresh]);
  const previousBusy = useRef(busy);
  useEffect(() => {
    const previous = previousBusy.current; previousBusy.current = busy;
    if (previous && !busy) void refresh();
  }, [busy, refresh]);
  const mutate = async (operation: () => Promise<unknown>, message: string | ((result: unknown) => string), secret = '') => {
    if (lock.current || loading) return false;
    const current = generation.current;
    lock.current = true; ++readVersion.current; setSaving(true); setError(''); setNotice('');
    try {
      const result = await operation();
      if (generation.current !== current) return false;
      await refresh(current);
      if (generation.current !== current) return false;
      setNotice(typeof message === 'function' ? message(result) : message);
      await changed.current?.();
      return true;
    } catch (e) {
      if (generation.current === current) {
        const message = e instanceof Error ? e.message : '保存失败，请重试。';
        await refresh(current);
        if (generation.current !== current) return false;
        setError(secret ? message.split(secret).join('••••••••') : message);
      }
      return false;
    } finally {
      if (generation.current === current) { lock.current = false; setSaving(false); }
    }
  };
  const select = (selection: ModelSelection) => draft
    ? Promise.resolve((draft.onChange(selection), true))
    : mutate(
    () => request('/sessions/' + encodeURIComponent(sessionId) + '/model', 'PATCH', {
      profile_id: selection.profile_id, profile_revision: selection.profile_revision,
      window_id: selection.window_id, thinking: selection.thinking, expected_revision: catalog?.revision,
    }), busy ? '已保存，当前回复结束后使用新设置。' : '模型设置已保存。',
  );
  const preferences = async (changes: { profile: ModelProfile; value: ModelPreference }[]) => mutate(async () => {
    for (const { profile, value } of changes) {
      await request('/models/' + encodeURIComponent(profile.profile_id) + '/preferences', 'PATCH', {
        visible: value.visible, default_window: value.window_id, thinking: value.thinking, expected_revision: profile.profile_revision,
      });
    }
  }, '模型列表与默认选项已保存。');
  const add = (draft: Record<string, unknown>, secret: string) => mutate(async () => {
    const validation = await request('/model-connections/validate', 'POST', draft);
    if (typeof validation.validation_ticket !== 'string' || typeof validation.config_hash !== 'string') throw new Error('模型校验未返回有效凭证，请重试。');
    return request('/model-connections', 'POST', { ...draft, validation_ticket: validation.validation_ticket, config_hash: validation.config_hash });
  }, result => result && typeof result === 'object' && 'reused' in result && result.reused === true
    ? '已校验，使用已有模型。' : draft.profile_id ? '模型已校验并更新，正在执行的回复保持原配置。' : '模型已校验并添加，可在选择列表中使用。', secret);
  const validate = async (draft: Record<string, unknown>, secret: string) => {
    if (lock.current || loading) return null;
    const current = generation.current;
    lock.current = true; setSaving(true); setError(''); setNotice('');
    try {
      const result = await request('/model-connections/validate', 'POST', draft);
      if (current !== generation.current) return null;
      if (typeof result.validation_ticket !== 'string' || typeof result.config_hash !== 'string') throw new Error('模型校验未返回有效凭证，请重试。');
      return result;
    } catch (e) {
      if (current === generation.current) {
        const message = e instanceof Error ? e.message : '连接测试失败，请检查配置后重试。';
        setError(secret ? message.split(secret).join('••••••••') : message);
      }
      return null;
    } finally {
      if (current === generation.current) { lock.current = false; setSaving(false); }
    }
  };
  const setDefault = (selection: ModelSelection) => mutate(
    () => request('/models/default', 'POST', {
      profile_id: selection.profile_id, window_id: selection.window_id, thinking: selection.thinking,
      expected_revision: catalog?.default_revision,
    }), '已设为新会话默认，已有会话保持原选择。',
  );
  // 草稿复用模型目录和管理能力，但绝不写入聊天会话的模型绑定。
  const displayedCatalog = catalog && draft ? { ...catalog, selection: draft.value, pending_selection: null, active_selection: null } : catalog;
  return { catalog: displayedCatalog, loading, saving, error, notice, refresh, select, preferences, add, validate, setDefault, clearError: () => setError('') };
}

type Workspace = ReturnType<typeof useModelWorkspace>;

function selectionDisplayName(selection: ModelSelection | null | undefined, profile: ModelProfile | undefined) {
  // 同一 API 型号可以更新展示名；配置已换型号时，旧快照仍展示旧型号名称。
  return selection?.model_id && selection.model_id === profile?.model_id ? profile.label : selection?.label ?? profile?.label;
}

function ReasoningSelect({ profile, value, disabled, onChange }: {
  profile: ModelProfile; value: string | null; disabled: boolean; onChange: (value: string) => void;
}) {
  return profile.reasoning.kind !== 'unsupported' && profile.reasoning.options.length ? <select aria-label={profile.label + '的思考设置'}
    value={value ?? ''} disabled={disabled} onChange={e => onChange(e.target.value)}>
    {profile.reasoning.options.map(option => <option key={option} value={option}>{thinkingLabel(option)}</option>)}
  </select> : <span className="model-unsupported">不支持</span>;
}

function ModelChoiceGroup({ label, value, options, disabled, onChange }: {
  label: string; value: string | null; options: { value: string; label: string; title?: string }[];
  disabled: boolean; onChange: (value: string) => void;
}) {
  const name = useId();
  // 原生单选保留方向键和读屏语义，视觉上使用紧凑的分段按钮。
  const locked = disabled || options.length < 2;
  return <div className="model-choice-group" role="radiogroup" aria-label={label} aria-disabled={locked}>
    {options.map(option => <label key={option.value} className="model-choice" title={option.title}>
      <input type="radio" name={name} value={option.value} checked={value === option.value} disabled={locked}
        onChange={() => onChange(option.value)} />
      <span>{option.label}</span>
    </label>)}
  </div>;
}

function WindowSlider({ profile, value, disabled, onChange }: {
  profile: ModelProfile; value: string; disabled: boolean; onChange: (value: string) => void;
}) {
  const windows = profile.window_options;
  const index = Math.max(0, windows.findIndex(window => window.id === value));
  const current = windows[index];
  const locked = disabled || windows.length < 2;
  const selectIndex = (next: number) => {
    if (!locked && windows[next]) onChange(windows[next].id);
  };
  return <div className="model-window-slider">
    <input type="range" min={0} max={Math.max(0, windows.length - 1)} step={1} value={index} disabled={locked}
      aria-label={profile.label + '的上下文窗口'} aria-valuetext={current ? formatModelWindow(current.tokens) + '，本次上限' : '无可选窗口'}
      style={{ background: `linear-gradient(to right, var(--model-window-fill, #e9e6e2) ${windows.length > 1 ? index / (windows.length - 1) * 100 : 100}%, var(--model-window-track, #f6f5f4) 0)` }}
      onChange={event => selectIndex(Number(event.target.value))} onKeyDown={event => {
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? windows.length - 1
          : ['ArrowRight', 'ArrowUp', 'PageUp'].includes(event.key) ? Math.min(windows.length - 1, index + 1)
            : ['ArrowLeft', 'ArrowDown', 'PageDown'].includes(event.key) ? Math.max(0, index - 1) : null;
        if (next !== null) { event.preventDefault(); selectIndex(next); }
      }} />
    <div className="model-window-ticks" aria-hidden="true">{windows.map((window, position) => <span key={window.id}
      className={position === index ? 'is-selected' : ''}>{formatModelWindow(window.tokens)}</span>)}</div>
  </div>;
}

function ModelManager({ workspace, onAdd, onClose }: { workspace: Workspace; onAdd: (profile?: ModelProfile) => void; onClose: () => void }) {
  const { catalog, saving, preferences, error, notice } = workspace;
  const [tab, setTab] = useState<'builtin' | 'custom'>('builtin');
  const [drafts, setDrafts] = useState<Record<string, ModelPreference>>({});
  const [attempted, setAttempted] = useState<Set<string>>(() => new Set());
  const [connecting, setConnecting] = useState<string | null>(null);
  const models = (catalog?.models ?? []).filter(model => !model.catalog_hidden);
  const filtered = models.filter(model => (model.catalog_group ?? (model.builtin ? 'default' : 'custom')) === (tab === 'builtin' ? 'default' : 'custom'));
  const changes = models.flatMap(profile => {
    const value = drafts[profile.profile_id];
    return value && JSON.stringify(value) !== JSON.stringify(modelPreference(profile)) ? [{ profile, value }] : [];
  });
  const update = (profile: ModelProfile, change: Partial<ModelPreference>) => setDrafts(current => ({
    ...current, [profile.profile_id]: { ...modelPreference(profile), ...current[profile.profile_id], ...change },
  }));
  const disabled = saving || !catalog;
  const connectionDisabled = disabled || !catalog?.management_allowed;
  const connect = async (profile: ModelProfile) => {
    if (connectionDisabled || !profile.is_virtual || !profile.connection_profile_id || profile.connection_profile_revision == null) return;
    setAttempted(current => new Set(current).add(profile.profile_id)); setConnecting(profile.profile_id);
    await workspace.add({
      preset_id: profile.preset_id, provider: profile.provider, protocol: profile.protocol,
      model_id: profile.model_id, label: profile.label, base_url: profile.base_url,
      capabilities: profile.capabilities, window_options: profile.window_options, default_window: profile.default_window,
      reasoning: profile.reasoning, counter: profile.counter ?? { kind: 'calibrated_estimate' },
      credential_profile_id: profile.connection_profile_id, credential_profile_revision: profile.connection_profile_revision, api_key: '',
    }, '');
    setConnecting(null);
  };
  return <div className="model-manager">
    <div className="model-manager-toolbar"><div className="model-tabs" role="tablist" aria-label="模型来源" onKeyDown={event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 'builtin' : event.key === 'End' ? 'custom' : tab === 'builtin' ? 'custom' : 'builtin';
      setTab(next); event.currentTarget.querySelectorAll<HTMLButtonElement>('button')[next === 'builtin' ? 0 : 1]?.focus();
    }}>
      <button type="button" role="tab" aria-selected={tab === 'builtin'} onClick={() => setTab('builtin')}>默认</button>
      <button type="button" role="tab" aria-selected={tab === 'custom'} onClick={() => setTab('custom')}>自定义</button>
    </div><button type="button" className="model-add-link" disabled={connectionDisabled} onClick={() => onAdd()}><Icon name="plus" />添加模型</button></div>
    <div className="model-table" role="tabpanel" aria-label={tab === 'builtin' ? '默认模型' : '自定义模型'}>
      <div className="model-table-header" aria-hidden="true"><span>模型名称</span><span>思考设置</span><span>显示状态</span><span>上下文窗口</span></div>
      {filtered.map(profile => {
        const value = drafts[profile.profile_id] ?? modelPreference(profile);
        const preferenceDisabled = disabled || !!profile.is_virtual;
        return <div className="model-table-row" key={profile.profile_id}>
          <div className="model-row-name"><span className="model-logo" aria-hidden="true"><Icon name="spark" /></span><div>
            <strong>{profile.label}</strong><small>API · {profile.model_id}</small>
            {!profile.builtin && !profile.is_virtual && <button type="button" className="model-edit-link" disabled={connectionDisabled} onClick={() => onAdd(profile)}>编辑连接</button>}
            {profile.is_virtual && <button type="button" className="model-edit-link model-connect-link"
              disabled={connectionDisabled || !profile.connection_profile_id || profile.connection_profile_revision == null}
              onClick={() => void connect(profile)}>{connecting === profile.profile_id ? '正在连接…' : attempted.has(profile.profile_id) ? '重试连接' : '连接当前网关'}</button>}
            {!modelAvailable(profile) && <small className="model-unavailable">{profile.unavailable_reason ?? '暂不可用'}</small>}
          </div></div>
          <div className="model-row-control"><span className="model-mobile-label">思考设置</span><ReasoningSelect profile={profile} value={value.thinking} disabled={preferenceDisabled} onChange={thinking => update(profile, { thinking })} /></div>
          <div className="model-row-control"><span className="model-mobile-label">列表显示</span><button type="button" role="switch" aria-label={'在选择列表显示' + profile.label} aria-checked={value.visible}
            disabled={preferenceDisabled} className="model-visibility-switch" onClick={() => update(profile, { visible: !value.visible })}><span /></button></div>
          <div className="model-row-control model-row-window"><span className="model-mobile-label">上下文窗口</span><WindowSlider profile={profile} value={value.window_id} disabled={preferenceDisabled} onChange={window_id => update(profile, { window_id })} />
            <small>{profile.source?.kind === 'operator_declared' ? '声明上限' : '配置上限'} {formatModelWindow(profile.capabilities.context_max)}</small></div>
        </div>;
      })}
      {!filtered.length && <div className="model-empty"><Icon name="spark" /><p>{tab === 'custom' ? '接入你自己的模型，让它帮你选购。' : '这里还没有默认模型。'}</p>{tab === 'custom' && <button type="button" disabled={connectionDisabled} onClick={() => onAdd()}>添加第一个模型</button>}</div>}
    </div>
    {!catalog?.management_allowed && <p className="model-help">可调整自己的列表和默认选项；添加或编辑连接需要本机或管理员权限。</p>}
    {error && <p className="model-error" role="alert">{error}</p>}
    {notice && <p className="model-notice" role="status">{notice}</p>}
    <footer className="model-dialog-footer"><p><Icon name="info" />偏好保存后生效，已开始的回复保持原设置。</p><div>
      <button type="button" className="model-button-secondary" disabled={saving} onClick={onClose}>取消</button>
      <button type="button" className="model-button-primary" disabled={disabled || !changes.length}
        onClick={() => void preferences(changes).then(ok => { if (ok) setDrafts({}); })}>{saving ? '正在保存…' : '保存设置'}</button>
    </div></footer>
  </div>;
}

function AddModel({ workspace, editing, onBack }: { workspace: Workspace; editing: ModelProfile | null; onBack: () => void }) {
  const presets = workspace.catalog?.presets ?? [];
  const allModels = workspace.catalog?.models ?? [];
  const credentialSources = allModels.filter(profile => profile.key_configured && profile.base_url && modelAvailable(profile));
  // 历史重复项不列出；若规范项没有可用凭据，保留当前网关作为明确的凭据来源。
  const connections = credentialSources.filter(profile => !profile.catalog_hidden || (profile.profile_id === 'current-gateway'
    && !credentialSources.some(source => source.profile_id === profile.canonical_profile_id && !source.catalog_hidden)));
  const selectedSource = allModels.find(profile => profile.profile_id === workspace.catalog?.selection?.profile_id);
  const currentGateway = allModels.find(profile => profile.profile_id === 'current-gateway');
  const initialConnection = connections.find(profile => profile.profile_id === (selectedSource?.canonical_profile_id ?? selectedSource?.profile_id))
    ?? connections.find(profile => profile.profile_id === (currentGateway?.canonical_profile_id ?? 'current-gateway')) ?? connections[0];
  const [provider, setProvider] = useState(editing?.provider ?? 'custom'), [label, setLabel] = useState(editing?.label ?? '');
  const [modelId, setModelId] = useState(editing?.model_id ?? ''), [baseUrl, setBaseUrl] = useState(editing?.base_url ?? ''), [apiKey, setApiKey] = useState('');
  const [presetId, setPresetId] = useState(editing?.preset_id ?? '');
  const [borrowCredential, setBorrowCredential] = useState(false), [credentialId, setCredentialId] = useState(initialConnection?.profile_id ?? '');
  const [showKey, setShowKey] = useState(false), [contextMax, setContextMax] = useState(String(editing?.capabilities.context_max ?? 128000));
  const [maxInput, setMaxInput] = useState(editing?.capabilities.max_input == null ? '' : String(editing.capabilities.max_input));
  const [outputMax, setOutputMax] = useState(String(editing?.capabilities.max_output ?? 8192));
  const [windows, setWindows] = useState(editing?.window_options.map(window => window.tokens).join(', ') ?? '64000, 128000');
  const [defaultWindow, setDefaultWindow] = useState(editing?.default_window ?? 'budget-128000');
  const [reasoning, setReasoning] = useState(editing?.reasoning.kind ?? 'unsupported');
  const [defaultThinking, setDefaultThinking] = useState(editing?.reasoning.default ?? 'default');
  const [reasoningOptions, setReasoningOptions] = useState(editing?.reasoning.kind === 'effort' ? editing.reasoning.options.join(', ') : 'low, medium, high');
  const [localError, setLocalError] = useState(''), [tested, setTested] = useState<Record<string, unknown> | null>(null);
  const [action, setAction] = useState<'test' | 'save' | null>(null);
  const id = useId(), disabled = workspace.saving || !workspace.catalog?.management_allowed;
  const preset = presets.find(item => item.preset_id === presetId);
  const connection = connections.find(profile => profile.profile_id === credentialId);
  const options = reasoning === 'unsupported' ? ['default'] : reasoning === 'boolean' ? ['default', 'off', 'on']
    : [...new Set(reasoningOptions.split(/[,，\s]+/).filter(Boolean))];
  const tokens = [...new Set(windows.split(/[,，\s]+/).filter(Boolean).map(Number))].sort((a, b) => a - b);
  const windowOptions = tokens.filter(value => Number.isSafeInteger(value) && value > 0).map(value =>
    (preset?.window_options ?? editing?.window_options)?.find(window => window.tokens === value) ?? { id: 'budget-' + value, tokens: value, kind: 'local_budget' });
  const chosenWindow = windowOptions.some(window => window.id === defaultWindow) ? defaultWindow : windowOptions[windowOptions.length - 1]?.id ?? '';
  const chosenThinking = options.includes(defaultThinking) ? defaultThinking : options[0] ?? 'default';
  const invalidateTest = () => { setTested(null); setLocalError(''); workspace.clearError(); };
  const useConnection = (profile: ModelProfile | undefined) => {
    if (profile) { setProvider(profile.provider); setBaseUrl(profile.base_url!); setApiKey(''); setShowKey(false); }
  };
  const applyPreset = (value: ModelPreset | undefined) => {
    invalidateTest(); setPresetId(value?.preset_id ?? '');
    if (!value) return;
    setModelId(value.model_id); setLabel(value.label);
    setContextMax(String(value.capabilities.context_max)); setOutputMax(String(value.capabilities.max_output));
    setMaxInput(value.capabilities.max_input == null ? '' : String(value.capabilities.max_input));
    setWindows(value.window_options.map(window => window.tokens).join(', ')); setDefaultWindow(value.default_window);
    setReasoning(value.reasoning.kind); setDefaultThinking(value.reasoning.default ?? 'default');
    if (value.reasoning.kind === 'effort') setReasoningOptions(value.reasoning.options.join(', '));
    if (!borrowCredential) { setProvider(value.provider); setBaseUrl(value.base_url); setApiKey(''); }
  };
  const buildDraft = (): Record<string, unknown> | null => {
    setLocalError('');
    if (!modelId.trim() || !baseUrl.trim()) { setLocalError('请填写模型 ID 和服务地址。'); return null; }
    if (borrowCredential && !connection) { setLocalError('请选择可用的网关连接，或取消沿用后填写新连接。'); return null; }
    if (!borrowCredential && editing?.key_configured && (baseUrl.trim().replace(/\/$/, '') !== (editing.base_url ?? '').replace(/\/$/, '') || provider !== editing.provider) && !apiKey.trim()) {
      setLocalError('服务地址或供应商已改变，请填写对应的新 API Key，原密钥不会发送到新的服务地址。'); return null;
    }
    const context = Number(contextMax), output = Number(outputMax), input = maxInput.trim() ? Number(maxInput) : null;
    if (!Number.isSafeInteger(context) || !Number.isSafeInteger(output) || context <= output + 4096 || output < 8192) {
      setLocalError('请填写有效窗口和输出上限：输出至少 8192，窗口还需留出输入和安全余量。'); return null;
    }
    if (input !== null && (!Number.isSafeInteger(input) || input <= 0 || input > context)) {
      setLocalError('输入上限需为正整数，且不能超过模型最大窗口。'); return null;
    }
    if (!tokens.length || tokens.some(value => !Number.isSafeInteger(value) || value <= output + Math.max(4096, Math.ceil(value * .05)) || value > context)) {
      setLocalError('各窗口档位需大于输出预留与安全余量，且不超过模型最大窗口。'); return null;
    }
    if (reasoning !== 'unsupported' && !options.length) { setLocalError('请填写模型实际支持的思考档位。'); return null; }
    return {
      ...(editing ? { profile_id: editing.profile_id, expected_revision: editing.profile_revision } : {}),
      ...(preset ? { preset_id: preset.preset_id } : {}),
      ...(borrowCredential && connection ? { credential_profile_id: connection.profile_id, credential_profile_revision: connection.profile_revision } : {}),
      provider, protocol: 'openai_chat', model_id: modelId.trim(), label: label.trim() || modelId.trim(),
      base_url: baseUrl.trim(), api_key: borrowCredential ? '' : apiKey,
      capabilities: { context_max: context, max_input: input, max_output: output, streaming: true, tool_calling: true },
      window_options: windowOptions, default_window: chosenWindow,
      reasoning: { kind: reasoning, options, default: chosenThinking },
      counter: { kind: 'calibrated_estimate' },
    };
  };
  const test = async () => {
    setTested(null);
    const draft = buildDraft(); if (!draft) return;
    setAction('test');
    const result = await workspace.validate(draft, apiKey);
    if (result) setTested(result.validation && typeof result.validation === 'object' ? result.validation as Record<string, unknown> : {});
    setAction(null);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); event.stopPropagation(); setTested(null);
    const draft = buildDraft(); if (!draft) return;
    // 保存时重新探测，避免旧测试凭证过期或草稿发生变化。
    setAction('save');
    const ok = await workspace.add(draft, apiKey);
    setAction(null);
    if (ok) { setApiKey(''); onBack(); }
  };
  const providers = [...new Set(['custom', provider, ...(editing ? [editing.provider] : []), ...presets.map(value => value.provider)])];
  const returnedModels = Array.isArray(tested?.returned_models) ? tested.returned_models.filter((value): value is string => typeof value === 'string') : [];
  return <div className="model-add-dialog">
    <header className="model-dialog-heading"><span className="model-eyebrow">CONNECT YOUR MODEL</span><h2>{editing ? '编辑模型' : '添加模型'}</h2><p>选择官方参考配置，或接入你自己的模型服务。</p></header>
    <form onSubmit={event => void submit(event)} onChangeCapture={invalidateTest}>
      <fieldset disabled={disabled}>
        {!!presets.length && <div className="model-preset-section"><label htmlFor={id + '-preset'}>型号预设<select id={id + '-preset'} value={presetId} onChange={e => applyPreset(presets.find(value => value.preset_id === e.target.value))}>
          <option value="">手动填写模型</option>{presets.map(value => <option key={value.preset_id} value={value.preset_id}>{value.label}</option>)}
        </select></label>
          {preset && <div className="model-preset-reference"><strong>官方参考窗口 {formatModelWindow(preset.official_limits?.context_max ?? preset.capabilities.context_max)}</strong>
            <p>当前网关尚未验证大窗口容量。下面的“本次上限”只控制本地预算，默认 {formatModelWindow(preset.window_options.find(window => window.id === preset.default_window)?.tokens ?? 0)}。</p>
            {preset.official_limits?.max_input_off && <p>官方输入上限：关闭思考 {preset.official_limits.max_input_off.toLocaleString()}；开启思考 {preset.official_limits.max_input_on?.toLocaleString() ?? '未知'}。本次采用兼容两者的保守输入上限。</p>}
            {preset.source.url && /^https?:\/\//.test(preset.source.url) && <a href={preset.source.url} target="_blank" rel="noreferrer">官方规格与版本说明 ↗</a>}
            {preset.source.checked_at && <small>核对于 {preset.source.checked_at}</small>}
          </div>}
        </div>}
        {!!connections.length && <div className="model-borrow-section">
          <label className="model-borrow-toggle"><input type="checkbox" checked={borrowCredential} onChange={e => { setBorrowCredential(e.target.checked); if (e.target.checked) useConnection(connection); }} />沿用当前网关连接</label>
          {borrowCredential ? <><label htmlFor={id + '-connection'}>凭据来源<select id={id + '-connection'} value={credentialId} onChange={e => { setCredentialId(e.target.value); useConnection(connections.find(profile => profile.profile_id === e.target.value)); }}>
            {connections.map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.label} · {profile.provider}</option>)}
          </select></label><p className="model-help">只在服务端复用这条连接的凭据，服务地址与供应商已锁定。取消勾选可填写新连接。</p></> : <p className="model-help">勾选后使用已有连接测试新型号，无需再次填写密钥。</p>}
        </div>}
        <label htmlFor={id + '-provider'}>供应商<select id={id + '-provider'} value={provider} disabled={borrowCredential} onChange={e => setProvider(e.target.value)}>
          {providers.map(value => <option key={value} value={value}>{value === 'custom' ? '自定义网关 · OpenAI 兼容' : value + ' · OpenAI 兼容'}</option>)}
        </select></label>
        <label htmlFor={id + '-url'}>服务地址 <span>Base URL</span><input id={id + '-url'} required type="url" disabled={borrowCredential} value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder="https://your-provider.example/v1" autoComplete="off" spellCheck={false} /></label>
        <div className="model-form-grid model-identity-fields">
          <label htmlFor={id + '-label'}>展示名称<input id={id + '-label'} required value={label} maxLength={120} onChange={e => setLabel(e.target.value)} placeholder="例如：千问旗舰模型" /><small>用于页面展示，可以自行命名。</small></label>
          <label htmlFor={id + '-model'}>API 模型 ID<input id={id + '-model'} required maxLength={200} value={modelId} onChange={e => {
            if (!label || label === modelId.slice(0, 120)) setLabel(e.target.value.slice(0, 120));
            setModelId(e.target.value); if (e.target.value !== preset?.model_id) setPresetId('');
          }} placeholder="供应商提供的 model_id" autoComplete="off" spellCheck={false} /><small>调用时发送这个型号，不使用展示名称。</small></label>
        </div>
        {!borrowCredential && <label htmlFor={id + '-key'}>API Key<div className="model-secret-input"><input id={id + '-key'} type={showKey ? 'text' : 'password'} value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={editing?.key_configured ? '留空保留已配置的密钥' : '请输入 API Key'} autoComplete="new-password" spellCheck={false} />
          <button type="button" aria-label={showKey ? '隐藏 API Key' : '显示 API Key'} aria-pressed={showKey} onClick={() => setShowKey(value => !value)}>{showKey ? '隐藏' : '显示'}</button></div>
          {editing?.key_configured && <small>密钥已配置且不会回显；修改服务地址时请同时填写对应的新密钥。</small>}</label>}
        <details className="model-advanced"><summary>容量与思考设置</summary>
          <p className="model-help">以下是本次配置。连通性校验不证明最大窗口容量；提高本地预算也不会扩大供应商实际支持的窗口。</p>
          <div className="model-form-grid"><label htmlFor={id + '-context'}>模型最大窗口<input id={id + '-context'} required inputMode="numeric" type="number" min={16384} value={contextMax} onChange={e => setContextMax(e.target.value)} /></label>
            <label htmlFor={id + '-input'}>独立输入上限<input id={id + '-input'} inputMode="numeric" type="number" min={1} value={maxInput} onChange={e => setMaxInput(e.target.value)} placeholder="可选，按服务商限制填写" /></label></div>
          <label htmlFor={id + '-output'}>每轮最大输出 tokens<input id={id + '-output'} required inputMode="numeric" type="number" min={8192} value={outputMax} onChange={e => setOutputMax(e.target.value)} /><small>预设保留 8192 个输出 token，不直接使用官方理论最大输出。</small></label>
          <label htmlFor={id + '-windows'}>可选的本次上限<input id={id + '-windows'} required value={windows} onChange={e => setWindows(e.target.value)} /><small>以逗号分隔，每个值都不得超过模型最大窗口。</small></label>
          <label htmlFor={id + '-default-window'}>默认本次上限<select id={id + '-default-window'} value={chosenWindow} onChange={e => setDefaultWindow(e.target.value)}>
            {windowOptions.map(window => <option key={window.id} value={window.id}>{formatModelWindow(window.tokens)}</option>)}
          </select><small>只是此模型的初始选项，不会替换已有会话或新会话默认模型。</small></label>
          <label htmlFor={id + '-reasoning'}>思考支持<select id={id + '-reasoning'} value={reasoning} onChange={e => setReasoning(e.target.value)}>
            <option value="unsupported">不启用思考参数</option><option value="boolean">开关 · enable_thinking</option><option value="effort">强度 · reasoning_effort</option>
          </select></label>
          {reasoning === 'effort' && <label htmlFor={id + '-options'}>供应商支持的强度档位<input id={id + '-options'} required value={reasoningOptions} onChange={e => setReasoningOptions(e.target.value)} /><small>例如 low, medium, high。仅填写该模型确实支持的值。</small></label>}
          {reasoning !== 'unsupported' && <label htmlFor={id + '-default-thinking'}>默认思考设置<select id={id + '-default-thinking'} value={chosenThinking} onChange={e => setDefaultThinking(e.target.value)}>
            {options.map(option => <option key={option} value={option}>{thinkingLabel(option)}</option>)}
          </select></label>}
        </details>
      </fieldset>
      {(localError || workspace.error) && <p className="model-error" role="alert">{localError || workspace.error}</p>}
      {tested && <div className="model-test-result" role="status"><strong>连接测试通过 · 尚未保存</strong>
        <p>流式回复与工具往返可用。小请求不验证大窗口容量。</p>
        {returnedModels.length > 0 && <p>网关返回型号：{returnedModels.join('、')}</p>}
        <p>{tested.model_identity_matches === true ? '返回型号标识一致。' : tested.model_identity_matches === false ? '返回型号与请求不一致，请核对网关映射后再保存。' : '网关未返回型号，身份未知。'}</p>
        {typeof tested.tested_at === 'string' && <small>测试时间：{tested.tested_at}</small>}
      </div>}
      <footer className="model-dialog-footer"><p><Icon name="info" />测试与保存都会发送少量固定样例，不携带你的对话。</p><div>
        <button type="button" disabled={workspace.saving} className="model-button-secondary" onClick={onBack}>返回</button>
        <button type="button" className="model-button-secondary model-test-button" disabled={disabled || !modelId.trim() || !baseUrl.trim()} onClick={() => void test()}>{workspace.saving && action === 'test' ? '正在测试…' : '测试连接'}</button>
        <button type="submit" className="model-button-primary" disabled={disabled || !modelId.trim() || !baseUrl.trim()}>{workspace.saving && action === 'save' ? '正在校验并保存…' : editing ? '校验并保存模型' : '校验并添加模型'}</button>
      </div></footer>
    </form>
  </div>;
}

export default function ModelPicker(props: ModelWorkspaceOptions) {
  const workspace = useModelWorkspace(props);
  const { catalog, loading, error, select } = workspace;
  const saving = workspace.saving || !!props.disabled;
  const [open, setOpen] = useState(false), [modal, setModal] = useState<'manage' | 'add' | null>(null), [query, setQuery] = useState('');
  const [editing, setEditing] = useState<ModelProfile | null>(null);
  const [group, setGroup] = useState<'default' | 'custom'>('default');
  const groupInitialized = useRef(false);
  const wrapper = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null), search = useRef<HTMLInputElement>(null), list = useRef<HTMLDivElement>(null);
  const id = useId();
  const [anchor, setAnchor] = useState<{ left: number; top?: number; bottom?: number; width: number; maxHeight: number }>();
  useLayoutEffect(() => {
    if (!open || !props.draft) return;
    // 弹窗里的滚动容器不能裁掉选项，按触发器位置把浮层放在可用空间较多的一侧。
    const place = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(350, window.innerWidth - 24);
      const above = rect.top - 16, below = window.innerHeight - rect.bottom - 16;
      const height = Math.min(440, Math.max(above, below));
      setAnchor({ left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        ...(above >= below ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }), width, maxHeight: height });
    };
    place(); window.addEventListener('resize', place); document.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); document.removeEventListener('scroll', place, true); };
  }, [open, props.draft !== undefined]);
  useEffect(() => { setOpen(false); setModal(null); setQuery(''); }, [props.sessionId]);
  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const outside = (event: PointerEvent) => { if (!wrapper.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const selected = catalog?.selection;
  const effective = catalog?.pending_selection ?? selected;
  const nextProfile = catalog?.models.find(profile => profile.profile_id === effective?.profile_id);
  const canonicalProfile = nextProfile?.canonical_profile_id ? catalog?.models.find(profile => profile.profile_id === nextProfile.canonical_profile_id && !profile.catalog_hidden) : undefined;
  const exactProfile = nextProfile && !nextProfile.catalog_hidden && (effective?.profile_revision == null || effective.profile_revision === nextProfile.profile_revision) ? nextProfile : undefined;
  const selectionLabel = nextProfile?.catalog_hidden ? canonicalProfile?.label ?? effective?.label ?? nextProfile.label : selectionDisplayName(effective, nextProfile);
  const runningProfile = catalog?.models.find(profile => profile.profile_id === catalog.active_selection?.profile_id);
  const runningCanonical = runningProfile?.catalog_hidden && runningProfile.canonical_profile_id
    ? catalog?.models.find(profile => profile.profile_id === runningProfile.canonical_profile_id && !profile.catalog_hidden) : undefined;
  const runningLabel = runningCanonical?.label ?? selectionDisplayName(catalog?.active_selection, runningProfile);
  const isDefault = effective && catalog?.default_selection && ['profile_id', 'window_id', 'thinking'].every(key => effective[key as keyof ModelSelection] === catalog.default_selection?.[key as keyof ModelSelection]);
  const profileGroup = (profile?: ModelProfile) => profile?.catalog_group ?? (profile?.builtin !== false ? 'default' : 'custom');
  const selectedGroup = profileGroup(canonicalProfile ?? nextProfile);
  useEffect(() => {
    // 列表晚到时只初始化一次，随后尊重买家的分组与搜索操作。
    if (open && catalog && !groupInitialized.current) {
      setGroup(selectedGroup); groupInitialized.current = true;
    }
  }, [open, catalog, selectedGroup]);
  const searching = query.trim().length > 0;
  const candidates = (catalog?.models ?? []).filter(profile => !profile.catalog_hidden && (profile.visible || profile.profile_id === effective?.profile_id)
    && (searching || profileGroup(profile) === group)
    && [profile.label, profile.model_id, profile.provider].some(value => value.toLowerCase().includes(query.trim().toLowerCase())));
  const close = () => { setOpen(false); trigger.current?.focus(); };
  const choose = async (profile: ModelProfile, selection?: ModelSelection) => {
    if (await select(selection ?? { profile_id: profile.profile_id, profile_revision: profile.profile_revision,
      window_id: modelPreference(profile).window_id, thinking: modelPreference(profile).thinking })) close();
  };
  const manage = () => { setOpen(false); setModal('manage'); };
  const closeModal = () => { setModal(null); trigger.current?.focus(); };
  return <div className="model-picker" ref={wrapper} onKeyDown={event => {
    if (!open || modal) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    // 搜索框位于聊天表单内，回车只留在模型选择器，不能意外发送草稿。
    if (event.key === 'Enter' && event.target === search.current) event.preventDefault();
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp')
      && (event.target === search.current || list.current?.contains(event.target as Node))) {
      const items = [...(list.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
      if (!items.length) return;
      event.preventDefault();
      const index = items.indexOf(document.activeElement as HTMLButtonElement), delta = event.key === 'ArrowDown' ? 1 : -1;
      items[index < 0 ? (delta > 0 ? 0 : items.length - 1) : (index + delta + items.length) % items.length]?.focus();
    }
  }}>
    <button type="button" ref={trigger} className="model-picker-trigger" disabled={props.disabled} aria-label={'选择模型，已选' + (selectionLabel ?? '正在读取')}
      aria-expanded={open} aria-haspopup="dialog" aria-controls={open ? id : undefined} onClick={() => {
        if (!open) { setGroup(selectedGroup); setQuery(''); groupInitialized.current = !!catalog; }
        setOpen(value => !value);
      }}>
      <Icon name="spark" /><span>{selectionLabel ?? (loading ? '读取模型…' : '选择模型')}</span>
      {exactProfile && exactProfile.reasoning.kind !== 'unsupported' && exactProfile.reasoning.options.length ? <small>{thinkingLabel(effective?.thinking)}</small> : null}
      {catalog?.pending_selection && <i className="model-pending-dot" aria-label="已保存下一轮设置" />}
      <svg className="model-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
    </button>
    {open && <div className="model-picker-panel" style={props.draft && anchor ? { ...anchor, position: 'fixed', top: anchor.top ?? 'auto', bottom: anchor.bottom ?? 'auto', right: 'auto', zIndex: 85 } : undefined} role="dialog" aria-label="选择模型" id={id}>
      <div className="model-picker-toolbar">
        <div className="model-picker-groups" role="tablist" aria-label="选择模型来源" onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 'default' : event.key === 'End' ? 'custom' : group === 'default' ? 'custom' : 'default';
          setGroup(next); setQuery(''); groupInitialized.current = true;
          event.currentTarget.querySelectorAll<HTMLButtonElement>('button')[next === 'default' ? 0 : 1]?.focus();
        }}>
          {(['default', 'custom'] as const).map(value => <button key={value} type="button" role="tab" id={id + '-' + value}
            aria-selected={group === value} aria-controls={id + '-options'} tabIndex={group === value ? 0 : -1}
            onClick={() => { setGroup(value); setQuery(''); groupInitialized.current = true; }}>{value === 'default' ? '默认' : '自定义'}</button>)}
        </div>
        <div className="model-search"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8" cy="8" r="5" /><path d="m12 12 5 5" /></svg>
          <input ref={search} aria-label="搜索模型" placeholder="搜索全部模型" value={query} onChange={e => { setQuery(e.target.value); groupInitialized.current = true; }} /></div>
      </div>
      {searching && <p className="model-search-scope" role="status">全部模型的搜索结果</p>}
      {(props.busy || props.pending || catalog?.pending_selection) && <p className="model-selection-note">{props.pending ? '待确认操作继续使用原模型，新设置在本轮结束后生效。' : props.busy ? '正在进行的回复保持原模型，新设置在下一轮生效。' : '已保存，将用于下一次回复。'}{(props.busy || props.pending) && runningLabel && <span>{props.pending ? '待确认操作' : '当前回复'}：{runningLabel}</span>}{catalog?.pending_selection && selectionLabel && <span>下一轮：{selectionLabel}</span>}</p>}
      {error && <p className="model-error" role="alert">{error}<button type="button" disabled={saving} onClick={() => void workspace.refresh()}>重新加载</button></p>}
      <div className="model-options" ref={list} id={id + '-options'} role="tabpanel" aria-label={searching ? '全部模型的搜索结果' : group === 'default' ? '默认模型列表' : '自定义模型列表'}>
        {loading ? <p className="model-empty" role="status">正在读取模型…</p> : candidates.map(profile => {
          const isSelected = profile.profile_id === effective?.profile_id && (effective.profile_revision == null || profile.profile_revision === effective.profile_revision);
          const window = profile.window_options.find(value => value.id === (isSelected ? effective?.window_id : modelPreference(profile).window_id));
          return <button type="button" key={profile.profile_id} className={'model-option' + (isSelected ? ' is-selected' : '')}
            disabled={saving || !modelAvailable(profile)} aria-pressed={isSelected} onClick={() => void choose(profile)}>
            <span className="model-logo" aria-hidden="true"><Icon name="spark" />{isSelected && <span className="model-option-check"><Icon name="check" /></span>}</span>
            <span className="model-option-copy"><strong title={profile.label}>{profile.label}</strong><small title={'API · ' + profile.model_id + ' · ' + profile.provider}>API · {profile.model_id}</small>
              {!modelAvailable(profile) && <small className="model-option-reason">{profile.unavailable_reason ?? '暂不可用，请检查连接'}</small>}</span>
            <span className="model-option-window" title="本次上下文上限">{window ? formatModelWindow(window.tokens) : '未知'}</span>
          </button>;
        })}
        {!loading && !candidates.length && <p className="model-empty">没有找到模型，可到模型管理中添加或显示。</p>}
      </div>
      {exactProfile && effective && exactProfile.reasoning.kind !== 'unsupported' && exactProfile.reasoning.options.length > 0 && <div className="model-current-controls">
        <div><span>思考</span><ModelChoiceGroup label={exactProfile.label + '的思考设置'} value={effective.thinking} disabled={saving}
          options={exactProfile.reasoning.options.map(option => ({ value: option, label: thinkingLabel(option) }))}
          onChange={thinking => void choose(exactProfile, { ...effective, thinking })} /></div>
      </div>}
      {effective && !exactProfile && <p className="model-selection-note">{nextProfile?.catalog_hidden && '此会话沿用历史连接。'}保留第 {effective.profile_revision ?? '—'} 版会话设置{effective.context_size ? ` · ${formatModelWindow(effective.context_size)}` : ''}。要使用新的配置，请从列表重新选择模型。</p>}
      {workspace.notice && <p className="model-notice" role="status">{workspace.notice}</p>}
      <footer className="model-picker-footer">
        <button type="button" className="model-manage-link" onClick={manage}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 3-1 3-3 1-2 4 2 2v3l3 3 3-1 3 1 3-3v-3l2-2-2-4-3-1-1-3Z" /><circle cx="11" cy="11" r="3" /></svg>模型管理</button>
        {!props.draft && effective && exactProfile && <button type="button" className="model-default-action" disabled={saving || !!isDefault}
          onClick={() => void workspace.setDefault(effective)}>{isDefault ? '已是新会话默认' : '设为新会话默认'}</button>}
      </footer>
    </div>}
    {modal && createPortal(<Modal key={modal} title={modal === 'add' ? editing ? '编辑模型' : '添加模型' : '模型管理'} onClose={closeModal}>
      {modal === 'manage' ? <ModelManager workspace={workspace} onAdd={profile => { setEditing(profile ?? null); setModal('add'); }} onClose={closeModal} />
        : <AddModel workspace={workspace} editing={editing} onBack={() => setModal('manage')} />}
    </Modal>, document.body)}
  </div>;
}
