import { memo, useEffect, useRef, useState } from "react";
import type { ExecutionActivity } from "../types";
import Icon from "./Icon";
import ToolProductResult, { type LoadToolProducts } from "./ToolProductResult";
import "./executionTrace.css";

const stateLabels: Record<ExecutionActivity["status"], string> = {
  preparing: "准备中", running: "进行中", waiting: "等待确认", completed: "已完成",
  approved: "已批准", error: "未完成", cancelled: "已取消", interrupted: "已中断",
};

function duration(item: ExecutionActivity): string | null {
  if (item.startedAt === null || item.finishedAt === null || item.finishedAt < item.startedAt) return null;
  const seconds = (item.finishedAt - item.startedAt) / 1000;
  return seconds < 1 ? "< 1 秒" : seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

const ActivityRow = memo(function ActivityRow({ item, loadToolProducts }: { item: ExecutionActivity; loadToolProducts?: LoadToolProducts }) {
  const [open, setOpen] = useState(false);
  const [visited, setVisited] = useState(false);
  const thinking = item.kind === "thinking";
  const live = item.status === "running" || item.status === "preparing";
  const elapsed = duration(item);
  return <li className={`execution-row execution-${item.kind}`} data-status={item.status}>
    <details open={open} onToggle={event => { setOpen(event.currentTarget.open); if (event.currentTarget.open) setVisited(true); }}>
      <summary>
        <span className={`execution-icon${live ? " is-live" : ""}`}>
          <Icon name={thinking ? "spark" : item.status === "completed" ? "check" : item.status === "error" ? "info" : "clock"} />
        </span>
        <span className="execution-title">{thinking ? "Think · 思考过程" : item.label}</span>
        <span className="execution-preview">{thinking ? (live ? "正在接收" : `${item.content?.length ?? 0} 字`) : item.resultSummary || item.inputSummary}</span>
        <span className="execution-status">{stateLabels[item.status]}</span>
        {elapsed && <span className="execution-duration">{elapsed}</span>}
        <span className="execution-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="execution-detail">
        {thinking ? <>
          <p className="execution-source">模型返回的思考内容</p>
          <div className="execution-thinking-text">{item.content || (live ? "正在等待模型返回思考内容…" : "本段没有可显示的思考内容。")}</div>
        </> : <>
          <dl className="execution-facts">
            {item.inputSummary && <div><dt>条件</dt><dd>{item.inputSummary}</dd></div>}
            {item.resultSummary && <div><dt>结果</dt><dd>{item.resultSummary}</dd></div>}
          </dl>
          {visited && item.toolName === "product_search_tool" && item.toolCallId && item.status === "completed" && loadToolProducts &&
            <ToolProductResult key={`${item.id}:${item.status}`} runId={item.runId} toolCallId={item.toolCallId} load={loadToolProducts} />}
          {!item.inputSummary && !item.resultSummary && <p>已记录执行状态，此步骤没有额外的可展示详情。</p>}
        </>}
        {item.truncated && <p className="execution-source">内容较长，此处展示已保存的片段。</p>}
      </div>
    </details>
  </li>;
});

function ProcessRun({ items, running, compact, index, loadToolProducts }: {
  items: ExecutionActivity[]; running: boolean; compact: boolean; index: number; loadToolProducts?: LoadToolProducts;
}) {
  const [open, setOpen] = useState(running || !compact);
  const touched = useRef(false);
  useEffect(() => { if (!touched.current) setOpen(running || !compact); }, [running, compact]);
  const thoughts = items.filter(item => item.kind === "thinking");
  const tools = items.filter(item => item.kind === "tool");
  const failed = items.some(item => ["error", "cancelled", "interrupted"].includes(item.status));
  const waiting = items.some(item => item.status === "waiting");
  const active = [...items].reverse().find(item => ["preparing", "running"].includes(item.status));
  const missing = !running && items.length === 0;
  const summary = missing ? "本轮未保存过程记录" : waiting ? "等待确认" : running ? active?.label || "正在处理" : failed ? "部分步骤未完成" : "处理完成";
  return <details className="execution-run" open={open} onToggle={event => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open); }}
    onClick={event => { if ((event.target as HTMLElement).closest("summary, button")) touched.current = true; }}>
    <summary className="execution-run-heading" onClick={() => { touched.current = true; }}>
      <Icon name={running ? "clock" : failed ? "info" : "check"} />
      <strong>{compact ? summary : `选购过程 ${index + 1}`}</strong>
      {!missing && <span>{tools.length} 次工具调用{thoughts.length ? ` · ${thoughts.length} 段 Think` : ""}</span>}
      {!running && !thoughts.length && <small title="模型本轮未返回可展示的思考内容，或旧会话没有保存该记录。">无 Think 记录</small>}
      <span className="execution-run-chevron" aria-hidden="true">⌄</span>
    </summary>
    {!thoughts.length && <p className="execution-thinking-note">{running ? "仅展示模型实际返回的思考内容，正在等待过程事件。" : "本轮没有可展示的思考记录。可在模型管理中查看思考设置。"}</p>}
    <ol>{[...items].sort((a, b) => a.sequence - b.sequence).map(item => <ActivityRow key={item.id} item={item} loadToolProducts={loadToolProducts} />)}</ol>
  </details>;
}

export default function ExecutionTrace({ activities, runId, running = false, compact = false, loadToolProducts }: {
  activities: ExecutionActivity[]; runId?: string; running?: boolean; compact?: boolean; loadToolProducts?: LoadToolProducts;
}) {
  const visible = activities.filter(item => !runId || item.runId === runId);
  if (!visible.length && !runId) return compact ? null : <div className="execution-empty">
    <Icon name="spark" /><strong>{running ? "正在接收执行过程" : "执行过程会保存在这里"}</strong>
    <p>工具调用和模型返回的 Think 会逐步出现。旧版会话可能没有这些记录。</p>
  </div>;
  // 分组保留轮次的接收顺序，同一轮按服务端序号排序；同名并行调用不合并。
  const groups = new Map<string, ExecutionActivity[]>();
  for (const item of visible) groups.set(item.runId, [...(groups.get(item.runId) ?? []), item]);
  if (runId && !groups.size) groups.set(runId, []);
  return <section className={`execution-trace${compact ? " is-compact" : ""}`} aria-label="思考与工具执行过程">
    {[...groups].map(([id, items], index) => <ProcessRun key={id} items={items} index={index} compact={compact}
      running={running && (runId === id || items.some(item => ["running", "preparing"].includes(item.status)))} loadToolProducts={loadToolProducts} />)}
  </section>;
}
