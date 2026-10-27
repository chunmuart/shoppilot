import type { ToolApproval } from "../types";
import "./confirmations.css";

const labels: Record<string,string> = { statement: "偏好内容", previous_statement: "原偏好", kind: "偏好类型" };
function details(item: ToolApproval) {
  try {
    const args = typeof item.arguments === "string" ? JSON.parse(item.arguments) : item.arguments;
    return Object.entries(args).filter(([key]) => key in labels).map(([key,value]) =>
      <p key={key}><strong>{labels[key]}：</strong>{key === "kind" ? (value === "dislike" ? "避免" : "喜欢") : String(value)}</p>);
  } catch { return <p>参数无法展示，请拒绝后重新描述需求。</p>; }
}
export function ToolApprovalCards({items,busy,onResolve}:{items:ToolApproval[];busy:boolean;onResolve:(id:string,approved:boolean)=>Promise<void>}) {
  if (!items.length) return null;
  return <section className="confirmation-list confirmations-section" aria-label="待确认的长期记忆操作">
    {items.map(item => <article className="confirmation-card" key={item.id}>
      <h3>{item.label}</h3>
      {details(item)}
      <p>批准后执行本次操作。保存时会提炼稳定购物偏好；拒绝不会修改记忆。</p>
      <div className="confirmation-actions">
        <button className="confirmation-secondary" type="button" disabled={busy} onClick={() => void onResolve(item.id,false)}>拒绝</button>
        <button className="confirmation-primary" type="button" disabled={busy} onClick={() => void onResolve(item.id,true)}>批准本次操作</button>
      </div>
    </article>)}
  </section>;
}
