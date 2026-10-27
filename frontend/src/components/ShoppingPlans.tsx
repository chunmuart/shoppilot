import type { PublishedSkill, SkillUsage } from "../types";
import Icon from "./Icon";
import { skillUsageLabel } from "../lib/skills";
import "./shoppingPlans.css";

interface Props {
  skills: PublishedSkill[];
  status: "loading" | "ready" | "error";
  error: string | null;
  selected: PublishedSkill | null;
  disabled?: boolean;
  compact?: boolean;
  onSelect: (skill: PublishedSkill) => void;
  onRefresh: () => void;
}

export default function ShoppingPlans({ skills, status, error, selected, disabled, compact, onSelect, onRefresh }: Props) {
  return <section className={`shopping-plans ${compact ? "shopping-plans--compact" : ""}`} aria-label="选购方案">
    <div className="shopping-plans-heading">
      <div><span className="plan-eyebrow"><Icon name="leaf" />从场景出发</span><h2>选购方案</h2></div>
      <button type="button" className="plan-refresh" onClick={onRefresh} disabled={status === "loading"}>刷新方案</button>
    </div>
    <p className="plan-introduction">选一个场景，再补充你的预算与偏好。直接描述需求，也会自动匹配。</p>
    {status === "loading" ? <p className="plan-empty" role="status">正在查看可用的选购方案…</p>
      : status === "error" ? <p className="plan-empty plan-load-error" role="status">{error || "选购方案暂时无法加载，仍可直接描述需求。"}</p>
      : skills.length === 0 ? <p className="plan-empty" role="status"><Icon name="leaf" />选购方案筹备中，可直接描述需求</p>
      : <div className="plan-grid">{skills.map((skill) => {
        const chosen = selected?.id === skill.id && selected.version === skill.version && selected.content_hash === skill.content_hash;
        return <button type="button" className={`plan-card ${chosen ? "is-selected" : ""}`} key={`${skill.id}@${skill.version}`}
          aria-label={`选择选购方案：${skill.title}`} aria-pressed={chosen} disabled={disabled} onClick={() => onSelect(skill)}>
          <span className="plan-card-top"><strong>{skill.title}</strong><span className="plan-version">{skill.version}</span></span>
          <span className="plan-description">{skill.description || "选好方案后，说说这次的实际需求。"}</span>
          <span className="plan-card-action">{chosen ? "已选择" : "选择方案"}<Icon name={chosen ? "check" : "arrow"} /></span>
        </button>;
      })}</div>}
  </section>;
}

export function SkillRunStatus({ usages, running }: { usages: SkillUsage[]; running: boolean }) {
  if (!usages.length) return null;
  // 同版本被预读和模型再次读取时，成功状态只展示一次；完整调用仍留在运行记录。
  const seen = new Set<string>();
  const visible = usages.filter((usage) => {
    if (usage.status !== "used") return true;
    const key = `${usage.id}@${usage.version}:${usage.contentHash}`;
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
  return <div className="skill-run-status" aria-label="方案读取状态" role="status">
    {visible.map((usage) => <p key={usage.toolCallId} className={`skill-run-item skill-run-item--${usage.status}`}>
      <Icon name={usage.status === "used" ? "check" : usage.status === "reading" && running ? "clock" : "info"} />
      <span>{usage.status === "reading" && !running
        ? `方案读取未完成：${usage.title || "选购方案"}${usage.version ? ` · ${usage.version}` : ""}`
        : skillUsageLabel(usage)}</span>
    </p>)}
  </div>;
}
