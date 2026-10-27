import type { ExecutionActivity } from "../types";

const statuses = new Set(["preparing", "running", "waiting", "completed", "error", "cancelled", "interrupted", "approved"]);
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const time = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const text = (value: unknown, max: number): string | undefined =>
  typeof value === "string" ? value.slice(0, max) : undefined;

/** 仅接收服务端展示合同，显式丢弃额外字段，不消费原始工具结果。 */
export function readActivities(value: unknown, runId?: string): ExecutionActivity[] {
  if (!Array.isArray(value)) return [];
  const result = new Map<string, ExecutionActivity>();
  for (const item of value) {
    if (!record(item) || typeof item.id !== "string" || !item.id ||
      typeof item.runId !== "string" || !item.runId || (runId && item.runId !== runId) ||
      !["tool", "thinking"].includes(String(item.kind)) || !statuses.has(String(item.status)) ||
      typeof item.sequence !== "number" || !Number.isSafeInteger(item.sequence) || item.sequence < 0) continue;
    const activity: ExecutionActivity = {
      id: item.id, runId: item.runId, kind: item.kind as ExecutionActivity["kind"],
      label: text(item.label, 120) || (item.kind === "thinking" ? "思考过程" : "执行工具"),
      status: item.status as ExecutionActivity["status"], sequence: item.sequence,
      startedAt: time(item.startedAt), finishedAt: time(item.finishedAt),
      toolCallId: text(item.toolCallId, 512), toolName: text(item.toolName, 120),
      inputSummary: text(item.inputSummary, 1600), resultSummary: text(item.resultSummary, 1600),
      content: item.kind === "thinking" ? text(item.content, 20000) : undefined,
      truncated: item.truncated === true, source: text(item.source, 80),
    };
    result.set(activity.runId + ":" + activity.id, activity);
  }
  return [...result.values()];
}

export function upsertActivities(current: ExecutionActivity[], incoming: ExecutionActivity[]): ExecutionActivity[] {
  const next = [...current];
  for (const item of incoming) {
    const index = next.findIndex(saved => saved.id === item.id && saved.runId === item.runId);
    if (index < 0) next.push(item);
    // 保持未变化的行引用，文本流不会导致每张执行卡重绘。
    else if (JSON.stringify(next[index]) !== JSON.stringify(item)) next[index] = item;
  }
  return next;
}

/** 本轮快照是权威状态；其他轮次不被新一轮运行清空。 */
export function replaceRunActivities(current: ExecutionActivity[], incoming: ExecutionActivity[], runId: string): ExecutionActivity[] {
  const ids = new Set(incoming.map(item => item.id));
  return upsertActivities(current.filter(item => item.runId !== runId || ids.has(item.id)), incoming);
}
