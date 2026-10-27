import { useEffect, useRef, useState } from "react";
import "./shoppingForm.css";

type RecordValue = Record<string, any>;
type Request = (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>;
type Question = {
  id: string; type: "text" | "number" | "single_select" | "multi_select";
  label: string; required: boolean; help_text: string; placeholder: string; unit: string;
  minimum: number | null; maximum: number | null; options: {value: string; label: string}[];
};
const record = (v: unknown): v is RecordValue => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown, max: number, required = false) => typeof v === "string" && v.length <= max && (!required || !!v.trim());

function isQuestion(q: unknown): q is Question {
  if (!record(q) || typeof q.id !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(q.id)
      || ["constructor", "prototype"].includes(q.id)
      || !["text", "number", "single_select", "multi_select"].includes(q.type)
      || !text(q.label, 200, true) || typeof q.required !== "boolean"
      || !text(q.help_text, 500) || !text(q.placeholder, 200) || !text(q.unit, 40)
      || ![q.minimum, q.maximum].every(v => v === null || (typeof v === "number" && Number.isFinite(v)))
      || (q.minimum !== null && q.maximum !== null && q.minimum > q.maximum)
      || !Array.isArray(q.options) || q.options.length > 20
      || !q.options.every(o => record(o) && text(o.value, 80, true) && text(o.label, 200, true))
      || new Set(q.options.map(o => o.value)).size !== q.options.length) return false;
  const selection = q.type === "single_select" || q.type === "multi_select";
  return (selection ? q.options.length > 0 : q.options.length === 0)
    && (q.type === "number" || (q.minimum === null && q.maximum === null && !q.unit));
}

/** 校验控件协议；不根据商品品类、字段名字或对话猜测要问的问题。 */
export function readShoppingForm(value: unknown): RecordValue | null {
  if (!record(value) || typeof value.form_id !== "string" || typeof value.session_id !== "string"
      || !Number.isSafeInteger(value.revision) || value.revision < 1
      || !Array.isArray(value.messages) || value.messages.length !== 3) return null;
  const [create, components, data] = value.messages;
  if (!value.messages.every((m: any) => record(m) && m.version === "v0.9")
      || create.createSurface?.catalogId !== "shoppilot.local/shopping-v2"
      || create.createSurface.surfaceId !== value.form_id
      || components.updateComponents?.surfaceId !== value.form_id
      || components.updateComponents?.components?.length !== 1
      || data.updateDataModel?.surfaceId !== value.form_id || data.updateDataModel.path !== "/") return null;
  const component = components.updateComponents.components[0];
  if (component?.component !== "ShoppingForm" || component.id !== "root" || !text(component.title, 100, true)
      || !text(component.context, 1000) || !text(component.description, 500)
      || !Array.isArray(component.questions) || !component.questions.length || component.questions.length > 12
      || !component.questions.every(isQuestion)
      || new Set(component.questions.map((q: Question) => q.id)).size !== component.questions.length
      || component.value?.path !== "/requirements"
      || component.action?.event?.name !== "applyShoppingRequirements") return null;
  const model = data.updateDataModel.value;
  if (!record(model) || !record(model.requirements) || model.revision !== value.revision
      || validateAnswers(model.requirements, component.questions, false)) return null;
  return {...value, title: component.title, context: component.context, description: component.description,
    questions: component.questions, defaults: model.requirements};
}

function validateAnswers(values: RecordValue, questions: Question[], complete = true): string {
  if (Object.keys(values).some(key => !questions.some(q => q.id === key))) return "答案包含本次未提问的字段";
  for (const q of questions) {
    const v = values[q.id];
    const blank = v == null || (typeof v === "string" && !v.trim()) || (Array.isArray(v) && !v.length);
    if (blank) { if (complete && q.required) return `请回答：${q.label}`; continue; }
    const options = q.options.map(o => o.value);
    if ((q.type === "text" && !text(v, 2000))
        || (q.type === "number" && (typeof v !== "number" || !Number.isFinite(v) || Math.abs(v) > 1e15
          || (q.minimum !== null && v < q.minimum) || (q.maximum !== null && v > q.maximum)))
        || (q.type === "single_select" && !options.includes(v))
        || (q.type === "multi_select" && (!Array.isArray(v) || new Set(v).size !== v.length || v.some(x => !options.includes(x)))))
      return `请检查答案：${q.label}`;
  }
  return "";
}

function displayAnswer(question: Question, value: unknown): string {
  if (value == null || value === "" || (Array.isArray(value) && !value.length)) return "未填写";
  const label = (v: unknown) => question.options.find(o => o.value === v)?.label ?? String(v);
  return (Array.isArray(value) ? value.map(label).join("、") : label(value)) + (question.unit ? ` ${question.unit}` : "");
}

/** 仅转换聊天气泡的展示；原始结构化内容继续用于 Agent 输入和持久恢复。 */
export function displayShoppingMessage(message: {id: string; content: string}): string {
  if (!/^form-run-[a-f0-9]{32}:user$/.test(message.id)
      || !message.content.startsWith("买家已提交本轮澄清答案。")) return message.content;
  try {
    const payload = JSON.parse(message.content.slice(message.content.indexOf("\n") + 1));
    const answer = (v: unknown): v is string | number | string[] => typeof v === "string"
      || (typeof v === "number" && Number.isFinite(v)) || (Array.isArray(v) && v.every(x => typeof x === "string"));
    if (!record(payload) || !text(payload.title, 100, true) || !Array.isArray(payload.answers)
        || payload.answers.length > 12 || !payload.answers.every((a: unknown) => record(a)
          && text(a.question, 200, true) && answer(a.display_value) && text(a.unit, 40))
        || !Array.isArray(payload.unanswered) || payload.unanswered.length > 12
        || !payload.unanswered.every((q: unknown) => record(q) && text(q.question, 200, true))) return message.content;
    return [
      `已补充：${payload.title}`,
      ...payload.answers.map((a: RecordValue) => `${a.question}：${Array.isArray(a.display_value) ? a.display_value.join("、") : a.display_value}${a.unit ? ` ${a.unit}` : ""}`),
      ...(payload.unanswered.length ? [`暂未填写：${payload.unanswered.map((q: RecordValue) => q.question).join("、")}`] : []),
    ].join("\n");
  } catch {return message.content;}
}

export default function ShoppingForm({form, busy, request, onApplied}: {
  form: unknown; busy: boolean; request: Request;
  onApplied: (query: string, runId: string) => void | Promise<void>;
}) {
  const parsed = readShoppingForm(form);
  const surface = useRef<HTMLElement>(null);
  useEffect(() => {surface.current?.scrollIntoView?.({block: "start", behavior: "smooth"});}, [parsed?.form_id]);
  const [values, setValues] = useState<RecordValue>(parsed?.defaults ?? {});
  const [saving, setSaving] = useState(false), [error, setError] = useState("");
  const [submitted, setSubmitted] = useState<RecordValue | null>(parsed?.submission ?? null);
  const requestId = useRef(crypto.randomUUID());
  const submitting = useRef(false), generation = useRef(0);
  useEffect(() => {
    ++generation.current; setValues(parsed?.defaults ?? {}); setSubmitted(parsed?.submission ?? null);
    setSaving(false); setError(""); submitting.current = false; requestId.current = crypto.randomUUID();
    return () => {++generation.current;};
  }, [parsed?.form_id, parsed?.revision]);
  if (!parsed) return <p role="status">暂不支持这份选购表单，请通过对话补充需求。</p>;
  const questions: Question[] = parsed.questions;
  const set = (key: string, value: unknown) => {
    setValues(old => {
      const next = {...old};
      if (value === "" || value === null || (Array.isArray(value) && !value.length)) delete next[key];
      else next[key] = value;
      return next;
    });
    requestId.current = crypto.randomUUID();
  };
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || submitting.current || !parsed) return;
    const invalid = validateAnswers(values, questions);
    if (invalid) {setError(invalid); return;}
    const current = generation.current;
    submitting.current = true; setSaving(true); setError("");
    try {
      const result = await request("/shopping-forms/" + encodeURIComponent(parsed.form_id) + "/actions", "POST", {
        session_id: parsed.session_id, expected_revision: parsed.revision, request_id: requestId.current, version: "v0.9",
        action: {name: "applyShoppingRequirements", surfaceId: parsed.form_id, sourceComponentId: "root",
          timestamp: new Date().toISOString(), context: {requirements: values}},
      });
      if (current !== generation.current) return;
      if (typeof result.query !== "string" || typeof result.run_id !== "string") throw new Error("服务返回的需求无效，请刷新");
      setSubmitted(result); await onApplied(result.query, result.run_id);
    } catch (e) {if (current === generation.current) setError(e instanceof Error ? e.message : "暂时未能提交，请重试");}
    finally {if (current === generation.current) {submitting.current = false; setSaving(false);}}
  }
  return <section ref={surface} className="shopping-form" aria-label="本次选购条件">
    <div className="shopping-form-heading"><span>需求澄清</span><h3>{parsed.title}</h3>
      {parsed.context && <p>{parsed.context}</p>}{parsed.description && <p>{parsed.description}</p>}</div>
    {submitted ? <div className="shopping-form-saved">
      {record(submitted.values) ? <dl>{questions.map(q => <div key={q.id}><dt>{q.label}</dt><dd>{displayAnswer(q, submitted.values[q.id])}</dd></div>)}</dl> : <p>{submitted.query}</p>}
      <button type="button" disabled={busy || saving} onClick={() => void onApplied(submitted.query, submitted.run_id)}>按已保存条件继续选购</button></div>
    : <form onSubmit={submit}><fieldset disabled={busy || saving}><div className="shopping-form-grid">
      {questions.map(q => {
        const id = `${parsed.form_id}-${q.id}`, helpId = `${id}-help`;
        const label = q.label + (q.unit ? `（${q.unit}）` : "") + (q.required ? " *" : "");
        return <div className="shopping-form-question" key={q.id}>
          {q.type === "multi_select" ? <fieldset className="shopping-form-choices" aria-describedby={q.help_text ? helpId : undefined}>
            <legend>{label}</legend>{q.options.map(option => <label key={option.value}><input type="checkbox" name={q.id} value={option.value}
              checked={(values[q.id] ?? []).includes(option.value)} onChange={e => set(q.id, e.target.checked
                ? [...(values[q.id] ?? []), option.value] : (values[q.id] ?? []).filter((v: string) => v !== option.value))}/>{option.label}</label>)}
          </fieldset> : <label htmlFor={id}>{label}
            {q.type === "single_select" ? <select id={id} name={q.id} required={q.required} value={values[q.id] ?? ""}
              aria-describedby={q.help_text ? helpId : undefined} onChange={e => set(q.id, e.target.value)}>
              <option value="">{q.placeholder || "请选择"}</option>{q.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select> : <input id={id} name={q.id} type={q.type} required={q.required} value={values[q.id] ?? ""}
              min={q.minimum ?? undefined} max={q.maximum ?? undefined} step={q.type === "number" ? "any" : undefined}
              maxLength={q.type === "text" ? 2000 : undefined} placeholder={q.placeholder}
              aria-describedby={q.help_text ? helpId : undefined}
              onChange={e => set(q.id, q.type === "number" && e.target.value !== "" ? Number(e.target.value) : e.target.value)}/>}
          </label>}
          {q.help_text && <p id={helpId} className="shopping-form-help">{q.help_text}</p>}
        </div>;
      })}</div>
      <p className="shopping-form-note">答案仅用于本次选购，带 * 的问题为必填。</p>
      <button type="submit" disabled={busy || saving}>{saving ? "正在保存…" : "提交答案并继续"}</button>
    </fieldset></form>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
