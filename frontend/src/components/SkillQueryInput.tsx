import { forwardRef, useEffect, useRef, useState } from "react";
import type { PublishedSkill } from "../types";
import Icon from "./Icon";
import "./shoppingPlans.css";

interface SlashCommand { start: number; end: number; query: string }
function commandAt(value: string, caret: number, end = caret): SlashCommand | null {
  if (caret !== end) return null;
  // 仅独立词首的 / 是命令，https://、路径中间和比例写法保持普通文字。
  const match = value.slice(0, caret).match(/(?:^|\s)\/([\p{L}\p{N}_.-]*)$/u);
  return match ? { start: caret - match[1].length - 1, end: caret, query: match[1] } : null;
}

interface Props {
  id?: string;
  multiline?: boolean;
  maxLength?: number;
  value: string;
  skills: PublishedSkill[];
  status: "loading" | "ready" | "error";
  error: string | null;
  busy: boolean;
  onChange: (value: string) => void;
  onSelect: (skill: PublishedSkill, draft: string) => void;
  onSubmit: () => void;
  onRefresh: () => void;
  onMenuOpenChange: (open: boolean) => void;
}

/** 菜单只完成选择；实际读取由提交后的服务端状态确认。 */
export default forwardRef<HTMLTextAreaElement, Props>(function SkillQueryInput({
  value, skills, status, error, busy, onChange, onSelect, onSubmit, onRefresh, onMenuOpenChange, id = "query", multiline = false, maxLength = 4000,
}, ref) {
  const [command, setCommand] = useState<SlashCommand | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const composing = useRef(false);
  const dismissed = useRef<string | null>(null);
  const observedValue = useRef(value);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const query = command?.query.toLocaleLowerCase() ?? "";
  const matches = skills.filter((skill) => `${skill.title}\n${skill.id}`.toLocaleLowerCase().includes(query));
  const index = Math.min(activeIndex, Math.max(0, matches.length - 1));
  const open = !!command && !busy;
  useEffect(() => {
    // 发送、换场景或恢复草稿等外部更新不能沿用旧光标命令。
    if (value !== observedValue.current) {
      observedValue.current = value;
      setCommand(null);
    }
  }, [value]);
  useEffect(() => { onMenuOpenChange(open); }, [open, onMenuOpenChange]);
  useEffect(() => {
    if (open) optionRefs.current[index]?.scrollIntoView?.({ block: "nearest" });
  }, [index, open, query]);
  const sync = (element: HTMLTextAreaElement, changed = false) => {
    observedValue.current = element.value;
    if (composing.current) return;
    if (changed) dismissed.current = null;
    const next = commandAt(element.value, element.selectionStart, element.selectionEnd);
    if (next && dismissed.current === `${next.start}:${next.end}:${next.query}`) return;
    setCommand(next);
    if (next?.query !== command?.query) setActiveIndex(0);
  };
  const close = () => {
    if (command) dismissed.current = `${command.start}:${command.end}:${command.query}`;
    setCommand(null);
  };
  const choose = (skill: PublishedSkill) => {
    if (!command || busy) return;
    const draft = `${value.slice(0, command.start)}${value.slice(command.end)}`;
    close();
    onSelect(skill, draft);
  };
  return <div className="skill-query-input">
    {open && <div className="slash-skill-menu" aria-label="选购方案菜单" onMouseDown={(event) => event.preventDefault()}>
      <div className="slash-skill-heading"><span><Icon name="leaf" />选择选购方案</span>
        <button type="button" onClick={onRefresh} disabled={status === "loading"}>刷新</button></div>
      {status === "loading" ? <p className="slash-skill-empty" role="status">正在查看可用方案…</p>
        : status === "error" ? <p className="slash-skill-empty" role="status">{error || "选购方案暂时无法加载，可直接描述需求。"}</p>
        : skills.length === 0 ? <p className="slash-skill-empty" role="status">选购方案筹备中，可直接描述需求</p>
        : matches.length === 0 ? <p className="slash-skill-empty" role="status">没有匹配的方案，换个名称试试。</p> : null}
      <div role="listbox" id="slash-skill-options" aria-label="可选方案" className="slash-skill-options">
        {status === "ready" && matches.map((skill, position) => <button type="button" role="option"
          id={`slash-skill-option-${position}`} key={`${skill.id}@${skill.version}`} aria-selected={position === index}
          ref={(element) => { optionRefs.current[position] = element; }} tabIndex={-1}
          onMouseDown={(event) => event.preventDefault()} onClick={() => choose(skill)} onMouseMove={() => setActiveIndex(position)}>
          <span className="slash-skill-name"><strong>{skill.title}</strong><small>{skill.version}</small></span>
          <span className="slash-skill-description">{skill.description}</span>
          <span className="slash-skill-id">{skill.id}</span>
        </button>)}
      </div>
      <p className="slash-skill-help"><span>↑ ↓ 选择 · Enter 确定</span><span>Esc 关闭</span></p>
    </div>}
    <textarea ref={ref} id={id} value={value} rows={value.includes("\n") ? 3 : 1} maxLength={maxLength}
      role="combobox" aria-autocomplete="list" aria-expanded={open}
      aria-controls={open ? "slash-skill-options" : undefined}
      aria-activedescendant={open && status === "ready" && matches.length ? `slash-skill-option-${index}` : undefined}
      placeholder="描述你的需求，或输入 / 选择选购方案"
      onChange={(event) => { onChange(event.target.value); sync(event.target, true); }}
      onSelect={(event) => sync(event.currentTarget)}
      onBlur={close}
      onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={(event) => { composing.current = false; sync(event.currentTarget, true); }}
      onKeyDown={(event) => {
        if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (open && ["ArrowDown", "ArrowUp", "Escape"].includes(event.key)) {
          event.preventDefault();
          if (event.key === "Escape") close();
          else if (matches.length) setActiveIndex((index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length);
          return;
        }
        if (event.key === "Enter" && !event.shiftKey && (open || !multiline)) {
          event.preventDefault();
          if (open) { if (status === "ready" && matches[index]) choose(matches[index]); }
          else if (!busy) onSubmit();
        }
      }} />
  </div>;
});
