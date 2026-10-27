import type { DiagnosticEvent, SkillUsage } from "../types";
import Icon from "./Icon";
export default function EventTimeline({
  events, skillUsages = [], running = false,
}: {
  events: DiagnosticEvent[];
  skillUsages?: SkillUsage[];
  running?: boolean;
}) {
  return (
    <details className="diagnostics">
      <summary>
        <Icon name="clock" />
        <span>查看运行记录</span>
        <span className="diagnostic-count">{events.length + skillUsages.length}</span>
      </summary>
      <p className="diagnostic-description">
        展示最近的可见执行步骤与状态，不包含模型隐式推理。
      </p>
      {events.length === 0 && skillUsages.length === 0 ? (
        <p className="diagnostic-empty">
          开始一次选购后，执行记录会出现在这里。
        </p>
      ) : (
        <ol>
          {skillUsages.map((usage) => <li key={`skill-${usage.toolCallId}`}>
            <div className="event-heading"><span>{usage.status === "reading" && !running ? "方案读取未完成" : usage.status === "used" ? "方案已读取" : usage.status === "error" ? "方案读取失败" : "正在读取方案"}</span></div>
            <p className="skill-event-name">{usage.title || "选购方案"}{usage.version ? ` · ${usage.version}` : ""}</p>
            <span className="event-type">{usage.status === "used" ? usage.source === "server_preload" ? "服务端已读取指定版本" : "工具成功返回" : usage.status === "reading" && !running ? "本轮已结束，未收到读取成功结果" : usage.source === "server_preload" ? "服务端读取状态" : "工具执行状态"}</span>
          </li>)}
          {events.slice(-60).map((event) => (
            <li key={event.id}>
              <div className="event-heading">
                <span>{event.label || event.type}</span>
                {event.timestamp !== null && <time>
                  {new Date(event.timestamp).toLocaleTimeString("zh-CN", {
                    hour12: false,
                  })}
                </time>}
              </div>
              <span className="event-type">{event.type}</span>
              {event.detail && <p>{event.detail}</p>}
            </li>
          ))}
        </ol>
      )}
    </details>
  );
}
