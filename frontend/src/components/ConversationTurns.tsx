import type { ChatMessage, ExecutionActivity } from "../types";
import Icon from "./Icon";
import Markdown from "./Markdown";
import { displayShoppingMessage } from "./ShoppingForm";
import ExecutionTrace from "./ExecutionTrace";
import type { LoadToolProducts } from "./ToolProductResult";
import "./conversationDensity.css";

export default function ConversationTurns({ messages, activities, activeRunId, running, loadToolProducts }: {
  messages: ChatMessage[]; activities: ExecutionActivity[]; activeRunId?: string; running: boolean;
  loadToolProducts?: LoadToolProducts;
}) {
  // 过程组件以轮次为身份。最终快照替换流式消息 ID 时，用户展开的步骤仍保留。
  const turns = new Map<string, { runId?: string; messages: ChatMessage[] }>();
  for (const message of messages) {
    const key = message.runId ? `run:${message.runId}` : `message:${message.id}`;
    if (!turns.has(key)) turns.set(key, { runId: message.runId, messages: [] });
    turns.get(key)!.messages.push(message);
  }
  return <section className="conversation" aria-label="选购对话">
    {[...turns].map(([key, turn]) => {
      const assistants = turn.messages.filter(message => message.role === "assistant");
      const live = running && turn.runId === activeRunId;
      return <div className="conversation-turn" key={key}>
        {turn.messages.filter(message => message.role === "user").map(message =>
          <div className="query-row" key={message.id}><div className="query-bubble">{displayShoppingMessage(message)}</div><span className="query-avatar">你</span></div>)}
        {(turn.runId || assistants.length > 0) && <div className="assistant-row">
          <div className="assistant-mark"><Icon name="globe" /></div>
          <div className="assistant-response">
            <div className="assistant-label">ShopPilot <span>选购助手</span></div>
            {turn.runId && <ExecutionTrace activities={activities} runId={turn.runId} running={live} compact loadToolProducts={loadToolProducts} />}
            {assistants.map((message, index) => <div className={`assistant-text ${live && index === assistants.length - 1 ? "typing" : ""}`} key={message.id}>
              <Markdown content={message.content} streaming={live && index === assistants.length - 1} />
            </div>)}
          </div>
        </div>}
      </div>;
    })}
  </section>;
}
