// 页面只解释服务端公开能力，不维护第二份模型与窗口映射。
export type ModelWindow = { id: string; tokens: number; kind: string; label?: string };
export type ModelSource = { kind?: string; url?: string; checked_at?: string; gateway_context_verified?: boolean };
export type ModelProfile = {
  profile_id: string; profile_revision: number; label: string; model_id: string;
  provider: string; protocol: string; base_url?: string; builtin: boolean; source?: ModelSource; preset_id?: string;
  status: string; visible: boolean; unavailable_reason?: string;
  catalog_hidden?: boolean; catalog_group?: 'default' | 'custom'; canonical_profile_id?: string;
  is_virtual?: boolean; connection_profile_id?: string; connection_profile_revision?: number;
  capabilities: { context_max: number; max_input?: number | null; max_output: number; streaming?: boolean; tool_calling?: boolean };
  window_options: ModelWindow[]; default_window: string;
  preferred_window?: string; preferred_thinking?: string;
  reasoning: { kind: string; options: string[]; default: string | null };
  counter?: { kind?: string }; key_configured?: boolean; key_last_four?: string;
  validation?: Record<string, unknown>;
};
export type ModelPreset = {
  preset_id: string; label: string; model_id: string; provider: string; protocol: string; base_url: string;
  capabilities: ModelProfile['capabilities']; window_options: ModelWindow[]; default_window: string;
  reasoning: ModelProfile['reasoning']; counter?: ModelProfile['counter']; source: ModelSource;
  official_limits?: { context_max?: number; max_input_off?: number; max_input_on?: number; max_output?: number };
};
export type ModelSelection = {
  profile_id: string; profile_revision?: number; window_id: string; thinking: string | null;
  model_id?: string; label?: string; context_size?: number; context_max?: number; fingerprint?: string;
};
export type ModelCatalog = {
  revision: number; models: ModelProfile[]; selection: ModelSelection | null;
  pending_selection?: ModelSelection | null; default_profile_id?: string;
  active_selection?: ModelSelection | null;
  default_selection?: ModelSelection | null; default_revision: number;
  management_allowed: boolean;
  presets?: ModelPreset[];
};
export type ModelPreference = { visible: boolean; window_id: string; thinking: string | null };

export function readModelCatalog(data: Record<string, unknown>): ModelCatalog {
  if (!Array.isArray(data.models) || !Number.isInteger(data.revision)) throw new Error('模型列表格式无效，请重新加载。');
  return data as ModelCatalog;
}

export function modelPreference(profile: ModelProfile): ModelPreference {
  return { visible: profile.visible, window_id: profile.preferred_window ?? profile.default_window, thinking: profile.preferred_thinking ?? profile.reasoning.default };
}

export function formatModelWindow(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return '未知';
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(2))}M`;
  if (tokens >= 1_000) return `${Number((tokens / 1_000).toFixed(1))}K`;
  return String(tokens);
}

export function thinkingLabel(value: string | null | undefined): string {
  if (value == null || value === 'none' || value === 'unsupported') return '不支持';
  const labels: Record<string, string> = { default: '默认', off: '关闭', on: '开启', false: '关闭', true: '开启',
    minimal: '最小', low: '低', medium: '中', high: '高', xhigh: '极高', max: '最大', ultra: '最高', auto: '自动' };
  return labels[value] ?? value;
}

export function modelAvailable(profile: ModelProfile): boolean {
  return !profile.is_virtual && profile.status === 'active'
    && profile.window_options.length > 0;
}
