import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkspaceRequest } from "./BuyerWorkspace";
import type { ModelProfile } from "../lib/modelCatalog";
import {
  modelAvailable,
  modelPreference,
} from "../lib/modelCatalog";
import type { PublishedSkill } from "../types";
import Modal from "./Modal";
import ModelPicker from "./ModelPicker";
import Icon from "./Icon";
import SkillQueryInput from "./SkillQueryInput";
import "./automations.css";

type Schedule = {
  kind: "daily" | "weekly" | "once" | "cron";
  timezone: string;
  time: string;
  weekdays: number[];
  cron: string;
  at: string | null;
  end_date: string | null;
};
type Definition = {
  name: string;
  prompt: string;
  schedule: Schedule;
  output_mode: "merged" | "independent";
  auto_start: boolean;
  model: {
    profile_id: string;
    profile_revision: number;
    window_id: string;
    thinking: string | null;
  };
  skill: { id: string; version: string; contentHash: string } | null;
  currency: string;
  locale: string;
};
type Task = {
  id: string;
  definition: Definition;
  revision: number;
  status: string;
  next_run_at: number | null;
};
type Run = {
  id: string;
  automation_id: string;
  definition: Definition;
  revision: number;
  status: string;
  session_id: string;
  scheduled_for: number;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
  error: string | null;
  catchup_from: number | null;
  attempts?: {
    attempt_no: number;
    agent_run_id: string;
    started_at: number | null;
    finished_at: number | null;
    model: { label?: string; model_id?: string } | null;
    usage: unknown;
  }[];
};
type Template = {
  id: string;
  name: string;
  description: string;
  prompt: string;
  time: string;
  weekdays: number[];
};
const statusName: Record<string, string> = {
  active: "已启用",
  paused: "已暂停",
  expired: "已到期",
  archived: "已归档",
  queued: "排队中",
  starting: "准备中",
  running: "执行中",
  awaiting_start: "待开始",
  awaiting_confirmation: "待确认",
  succeeded: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
  skipped_overlap: "跳过 · 上次未结束",
  missed: "错过执行",
};
function dateLabel(value: number | null, zone = "Asia/Shanghai") {
  return value
    ? new Date(value * 1000).toLocaleString("zh-CN", {
        timeZone: zone,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
}
function scheduleLabel(s: Schedule) {
  return s.kind === "weekly"
    ? `每周${s.weekdays.map((d) => "一二三四五六日"[d - 1]).join("、")} ${s.time}`
    : s.kind === "daily"
      ? `每天 ${s.time}`
      : s.kind === "once"
        ? `单次 ${s.at?.replace("T", " ")}`
        : s.cron;
}
function blank(model?: ModelProfile, template?: Template): Definition {
  const preference = model ? modelPreference(model) : null;
  return {
    name: template?.name ?? "",
    prompt: template?.prompt ?? "",
    schedule: {
      kind: "weekly",
      timezone: "Asia/Shanghai",
      time: template?.time ?? "10:00",
      weekdays: template?.weekdays ?? [1],
      cron: "0 10 * * 1",
      at: null,
      end_date: null,
    },
    output_mode: "merged",
    auto_start: false,
    model: {
      profile_id: model?.profile_id ?? "",
      profile_revision: model?.profile_revision ?? 1,
      window_id: preference?.window_id ?? "",
      thinking: preference?.thinking ?? null,
    },
    skill: null,
    currency: "CNY",
    locale: "zh-CN",
  };
}

export default function Automations({
  request,
  onOpenSession,
  onHistoryChanged,
  sessionId,
  busy = false,
}: {
  request: WorkspaceRequest;
  onOpenSession: (id: string) => void;
  onHistoryChanged?: () => Promise<void>;
  sessionId: string;
  busy?: boolean;
}) {
  const [tab, setTab] = useState<"tasks" | "templates" | "runs">("tasks");
  const [tasks, setTasks] = useState<Task[]>([]),
    [runs, setRuns] = useState<Run[]>([]),
    [templates, setTemplates] = useState<Template[]>([]);
  const [models, setModels] = useState<ModelProfile[]>([]),
    [skills, setSkills] = useState<PublishedSkill[]>([]);
  const [worker, setWorker] = useState<{
    online: boolean;
    last_seen: number | null;
  } | null>(null);
  const [editor, setEditor] = useState<{
      task?: Task;
      template?: Template;
    } | null>(null),
    [detail, setDetail] = useState<Run | null>(null);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [loading, setLoading] = useState(true),
    [saving, setSaving] = useState(false);
  const [offset, setOffset] = useState(0),
    [total, setTotal] = useState(0),
    [filter, setFilter] = useState("");
  const [taskOffset, setTaskOffset] = useState(0),
    [taskTotal, setTaskTotal] = useState(0);
  const historyChanged = useRef(onHistoryChanged);
  historyChanged.current = onHistoryChanged;
  const runDirectoryVersion = useRef("");
  const alive = useRef(true),
    read = useRef(0),
    lock = useRef(false),
    keys = useRef(new Map<string, string>());
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const refresh = useCallback(async () => {
    const version = ++read.current;
    try {
      const [taskData, runData] = await Promise.all([
        request(`/automations?limit=30&offset=${taskOffset}`),
        request(
          `/automation-runs?limit=20&offset=${offset}${filter ? "&status=" + filter : ""}`,
        ),
      ]);
      if (!alive.current || version !== read.current) return;
      setTasks(taskData.items as Task[]);
      setTaskTotal(Number(taskData.total));
      setWorker(taskData.worker as typeof worker);
      setRuns(runData.items as Run[]);
      setTotal(Number(runData.total));
      const nextVersion = JSON.stringify((runData.items as Run[]).map(run => [run.id, run.status, run.updated_at]));
      if (runDirectoryVersion.current !== nextVersion) {
        runDirectoryVersion.current = nextVersion;
        void historyChanged.current?.();
      }
    } catch (e) {
      if (alive.current && version === read.current)
        setError(e instanceof Error ? e.message : "自动化暂时读取失败");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [request, offset, filter, taskOffset]);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  const loadOptions = useCallback(async () => {
    try {
      const [options, list] = await Promise.all([
        request("/automations/options"),
        request("/automations/templates"),
      ]);
      if (!alive.current) return;
      setModels(
        (options.models as ModelProfile[])
          .filter((m) => modelAvailable(m) && !m.catalog_hidden)
          .sort(
            (a, b) =>
              Number(b.profile_id === "current-gateway") -
              Number(a.profile_id === "current-gateway"),
          ),
      );
      setSkills(options.skills as PublishedSkill[]);
      setTemplates(list.items as Template[]);
    } catch (e) {
      if (alive.current)
        setError(e instanceof Error ? e.message : "配置选项暂不可用");
    }
  }, [request]);
  useEffect(() => {
    void loadOptions();
  }, [loadOptions]);
  const mutate = async (
    path: string,
    body: Record<string, unknown>,
    message: string,
    method = "POST",
  ) => {
    if (lock.current) return false;
    lock.current = true;
    setSaving(true);
    setError("");
    setNotice("");
    const key = JSON.stringify([path, body, method]);
    const id = keys.current.get(key) ?? crypto.randomUUID();
    keys.current.set(key, id);
    try {
      await request(path, method, { ...body, request_id: id });
      keys.current.delete(key);
      if (!alive.current) return false;
      setNotice(message);
      await refresh();
      return true;
    } catch (e) {
      if (alive.current)
        setError(e instanceof Error ? e.message : "保存失败，请重试");
      return false;
    } finally {
      lock.current = false;
      if (alive.current) setSaving(false);
    }
  };
  const runAction = async (run: Run, action: string) => {
    if (
      await mutate(
        `/automation-runs/${run.id}/actions`,
        { action, expected_revision: run.revision },
        action === "start"
          ? "已加入执行队列。"
          : action === "retry"
            ? "已创建新的执行尝试，旧记录保留。"
            : "停止请求已保存。",
      )
    )
      setDetail(null);
  };
  const openRun = async (run: Run) => {
    try {
      setDetail(
        (await request("/automation-runs/" + run.id)) as unknown as Run,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "记录读取失败");
    }
  };
  const pending = runs.filter((r) =>
    ["awaiting_start", "awaiting_confirmation"].includes(r.status),
  ).length;
  return (
    <section className="automation-workspace">
      <div className="automation-heading">
        <div>
          <div className="automation-eyebrow">
            <Icon name="clock" />
            定时任务
          </div>
          <h2>按计划执行例行选购</h2>
          <p>
            设置计划后到点生成待开始记录；下单仍需确认，连续失败三次自动暂停。
          </p>
        </div>
        <button
          className="primary"
          onClick={() => {
            setEditor({});
            void loadOptions();
          }}
        >
          <Icon name="plus" />
          创建自动化
        </button>
      </div>
      <div className={`automation-service ${worker?.online ? "online" : ""}`}>
        <span className="automation-dot" />
        <span>
          {worker?.online
            ? "调度服务在线 · 关闭页面后仍会执行"
            : worker
              ? "调度服务离线 · 已保存的计划将在服务恢复后检查"
              : "正在连接调度服务"}
        </span>
        {pending > 0 && (
          <button onClick={() => setTab("runs")}>
            {pending} 条待处理 <Icon name="arrow" />
          </button>
        )}
        <button className="automation-refresh" onClick={() => void refresh()}>
          刷新
        </button>
      </div>
      <div className="automation-tabs" role="tablist" aria-label="自动化视图">
        {(
          [
            ["tasks", "自动化"],
            ["templates", "模板"],
            ["runs", "执行记录"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {error && (
        <p className="automation-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="automation-notice" role="status">
          {notice}
        </p>
      )}
      {loading ? (
        <p className="automation-empty">正在读取已保存的计划…</p>
      ) : tab === "tasks" ? (
        <>
          {!tasks.length ? (
            <div className="automation-empty">
              <Icon name="clock" />
              <h3>给下一次选购，安排一个时间</h3>
              <p>从每周清单开始，或创建你自己的任务。</p>
              <button onClick={() => setTab("templates")}>
                浏览模板 <Icon name="arrow" />
              </button>
            </div>
          ) : (
            <div className="automation-list">
              {tasks.map((task) => (
                <article className="automation-row" key={task.id}>
                  <div className="automation-row-icon">
                    <Icon name="clock" />
                  </div>
                  <div className="automation-row-main">
                    <h3>
                      {task.definition.name}
                      <span className={"automation-status " + task.status}>
                        {statusName[task.status]}
                      </span>
                    </h3>
                    <p>{task.definition.prompt}</p>
                    <small>
                      {scheduleLabel(task.definition.schedule)} ·{" "}
                      {task.definition.schedule.timezone} ·{" "}
                      {task.definition.output_mode === "merged"
                        ? "合并输出"
                        : "独立输出"}
                    </small>
                  </div>
                  <div className="automation-next">
                    <small>下次执行</small>
                    <strong>
                      {dateLabel(
                        task.next_run_at,
                        task.definition.schedule.timezone,
                      )}
                    </strong>
                  </div>
                  <div className="automation-row-actions">
                    <button
                      disabled={saving || task.status !== "active"}
                      onClick={() =>
                        void mutate(
                          `/automations/${task.id}/runs`,
                          { expected_revision: task.revision },
                          "已加入执行队列。",
                        )
                      }
                    >
                      立即执行
                    </button>
                    <button
                      disabled={saving || task.status === "archived"}
                      onClick={() => {
                        setEditor({ task });
                        void loadOptions();
                      }}
                    >
                      编辑
                    </button>
                    <button
                      disabled={
                        saving || ["archived", "expired"].includes(task.status)
                      }
                      onClick={() =>
                        void mutate(
                          `/automations/${task.id}/actions`,
                          {
                            action:
                              task.status === "active" ? "pause" : "resume",
                            expected_revision: task.revision,
                          },
                          "计划状态已更新。",
                        )
                      }
                    >
                      {task.status === "active" ? "暂停" : "恢复"}
                    </button>
                    <button
                      aria-label={"归档 " + task.definition.name}
                      disabled={saving || task.status === "archived"}
                      onClick={() =>
                        void mutate(
                          `/automations/${task.id}/actions`,
                          {
                            action: "archive",
                            expected_revision: task.revision,
                          },
                          "已归档，历史执行仍保留。",
                        )
                      }
                    >
                      归档
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
          {taskTotal > 30 && (
            <div className="automation-pager">
              <button
                disabled={!taskOffset}
                onClick={() => setTaskOffset((v) => Math.max(0, v - 30))}
              >
                上一页
              </button>
              <span>
                {taskOffset + 1}–{Math.min(taskOffset + 30, taskTotal)} /{" "}
                {taskTotal}
              </span>
              <button
                disabled={taskOffset + 30 >= taskTotal}
                onClick={() => setTaskOffset((v) => v + 30)}
              >
                下一页
              </button>
            </div>
          )}
        </>
      ) : tab === "templates" ? (
        <div className="automation-templates">
          {templates.map((template) => (
            <article key={template.id}>
              <div className="automation-template-icon">
                <Icon
                  name={
                    template.id === "review"
                      ? "chat"
                      : template.id === "compare"
                        ? "compare"
                        : "bag"
                  }
                />
              </div>
              <h3>{template.name}</h3>
              <p>{template.description}</p>
              <footer>
                <span>
                  <Icon name="clock" />
                  每周{"一二三四五六日"[template.weekdays[0] - 1]}{" "}
                  {template.time}
                </span>
                <button
                  onClick={() => {
                    setEditor({ template });
                    void loadOptions();
                  }}
                >
                  使用模板 <Icon name="arrow" />
                </button>
              </footer>
            </article>
          ))}
        </div>
      ) : (
        <>
          <div className="automation-filters">
            {[
              ["", "全部"],
              ["awaiting_start", "待开始"],
              ["awaiting_confirmation", "待确认"],
              ["succeeded", "已完成"],
              ["failed", "失败"],
            ].map(([value, label]) => (
              <button
                key={value}
                aria-pressed={filter === value}
                onClick={() => {
                  setFilter(value);
                  setOffset(0);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {!runs.length ? (
            <p className="automation-empty">
              这里会保存每次执行，即使选择合并输出。
            </p>
          ) : (
            <div className="automation-list">
              {runs.map((run) => (
                <article key={run.id} className="automation-run-row">
                  <div>
                    <button
                      className="automation-run-title"
                      onClick={() => void openRun(run)}
                    >
                      {run.definition.name}
                    </button>
                    <small>
                      {dateLabel(
                        run.scheduled_for,
                        run.definition.schedule.timezone,
                      )}{" "}
                      ·{" "}
                      {run.definition.output_mode === "merged"
                        ? "合并输出"
                        : "独立输出"}
                      {run.catchup_from ? " · 恢复补跑" : ""}
                    </small>
                  </div>
                  <span className={"automation-status " + run.status}>
                    {statusName[run.status] ?? run.status}
                  </span>
                  <div className="automation-row-actions">
                    {run.status === "awaiting_start" && (
                      <button
                        disabled={saving}
                        onClick={() => void runAction(run, "start")}
                      >
                        开始执行
                      </button>
                    )}
                    <button onClick={() => void openRun(run)}>查看记录</button>
                    {[
                      "running",
                      "succeeded",
                      "awaiting_confirmation",
                      "failed",
                      "interrupted",
                    ].includes(run.status) && (
                      <button
                        disabled={busy}
                        onClick={() => onOpenSession(run.session_id)}
                      >
                        打开结果 <Icon name="arrow" />
                      </button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}
          {total > 20 && (
            <div className="automation-pager">
              <button
                disabled={!offset}
                onClick={() => setOffset((v) => Math.max(0, v - 20))}
              >
                上一页
              </button>
              <span>
                {offset + 1}–{Math.min(offset + 20, total)} / {total}
              </span>
              <button
                disabled={offset + 20 >= total}
                onClick={() => setOffset((v) => v + 20)}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
      {editor && (
        <AutomationEditor
          initial={editor.task?.definition ?? blank(models[0], editor.template)}
          models={models}
          sessionId={sessionId}
          skills={skills}
          request={request}
          saving={saving}
          error={error}
          editing={!!editor.task}
          onClose={() => {
            if (!saving) setEditor(null);
          }}
          onSave={async (definition) => {
            const task = editor.task;
            const ok = await mutate(
              task ? "/automations/" + task.id : "/automations",
              {
                definition,
                ...(task ? { expected_revision: task.revision } : {}),
              },
              "自动化已保存。",
              task ? "PATCH" : "POST",
            );
            if (ok) {
              setEditor(null);
              setTab("tasks");
            }
          }}
        />
      )}
      {detail && (
        <Modal title="执行记录" onClose={() => setDetail(null)}>
          <div className="automation-run-detail">
            <h2>{detail.definition.name}</h2>
            <span className={"automation-status " + detail.status}>
              {statusName[detail.status]}
            </span>
            <p>
              计划时间：
              {dateLabel(
                detail.scheduled_for,
                detail.definition.schedule.timezone,
              )}{" "}
              · {detail.definition.schedule.timezone}
            </p>
            {detail.error && <p className="automation-error">{detail.error}</p>}
            {detail.attempts?.map((a) => (
              <div className="automation-attempt" key={a.attempt_no}>
                <strong>第 {a.attempt_no} 次尝试</strong>
                <p>{a.model?.label ?? a.model?.model_id ?? "模型尚未启动"}</p>
                <small>
                  开始 {dateLabel(a.started_at)} · 结束{" "}
                  {dateLabel(a.finished_at)}
                </small>
                <details>
                  <summary>调用消耗{a.usage ? "" : " · 暂无实际用量"}</summary>
                  <pre>
                    {a.usage
                      ? JSON.stringify(a.usage, null, 2)
                      : "未收到实际 usage，不能按 0 计算。"}
                  </pre>
                </details>
              </div>
            ))}
            <footer>
              <button
                disabled={busy || !detail.attempts?.some((a) => a.started_at)}
                onClick={() => {
                  onOpenSession(detail.session_id);
                  setDetail(null);
                }}
              >
                打开结果会话
              </button>
              {["failed", "interrupted", "cancelled"].includes(
                detail.status,
              ) && (
                <button
                  disabled={saving}
                  onClick={() => void runAction(detail, "retry")}
                >
                  重新执行
                </button>
              )}
              {["queued", "starting", "running", "awaiting_start"].includes(
                detail.status,
              ) && (
                <button
                  disabled={saving}
                  onClick={() => void runAction(detail, "stop")}
                >
                  停止本次
                </button>
              )}
            </footer>
          </div>
        </Modal>
      )}
    </section>
  );
}

function AutomationEditor({
  initial,
  sessionId,
  models,
  skills,
  request,
  saving,
  error,
  editing,
  onClose,
  onSave,
}: {
  initial: Definition;
  sessionId: string;
  models: ModelProfile[];
  skills: PublishedSkill[];
  request: WorkspaceRequest;
  saving: boolean;
  error: string;
  editing: boolean;
  onClose: () => void;
  onSave: (value: Definition) => Promise<void>;
}) {
  const [draft, setDraft] = useState(initial),
    [times, setTimes] = useState<string[]>([]),
    [previewError, setPreviewError] = useState("");
  const [expiry, setExpiry] = useState(!!initial.schedule.end_date);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const schedule = (patch: Partial<Schedule>) =>
    setDraft((v) => ({ ...v, schedule: { ...v.schedule, ...patch } }));
  const patch = (value: Partial<Definition>) =>
    setDraft((v) => ({ ...v, ...value }));
  useEffect(() => {
    if (!draft.model.profile_id && models[0])
      setDraft((v) => ({ ...v, model: blank(models[0]).model }));
  }, [models, draft.model.profile_id]);
  const encodedSchedule = JSON.stringify(draft.schedule);
  useEffect(() => {
    let current = true;
    setTimes([]);
    setPreviewError("");
    const timeout = setTimeout(() => {
      void request("/automations/preview", "POST", {
        schedule: JSON.parse(encodedSchedule),
      })
        .then((data) => {
          if (current) {
            setTimes(data.times as string[]);
            if (!(data.times as string[]).length)
              setPreviewError("有效期内没有下一次执行，请调整计划。");
          }
        })
        .catch((e) => {
          if (current)
            setPreviewError(e instanceof Error ? e.message : "时间规则有误");
        });
    }, 250);
    return () => {
      current = false;
      clearTimeout(timeout);
    };
  }, [encodedSchedule, request]);
  return (
    <Modal
      title={editing ? "编辑自动化" : "创建自动化"}
      onClose={onClose}
      initialFocusRef={inputRef}
    >
      <form
        className="automation-editor"
        onSubmit={(e) => {
          e.preventDefault();
          if (!saving && times.length && !previewError) void onSave(draft);
        }}
      >
        <header>
          <h2>{editing ? "编辑自动化" : "创建自动化"}</h2>
          <p>安排时间，写下需求，选择每次结果如何保存。</p>
        </header>
        <div className="automation-editor-columns">
          <div className="automation-editor-content">
            <label>
              任务名称
              <input
                required
                maxLength={100}
                value={draft.name}
                placeholder="例如：每周通勤清单"
                onChange={(e) => patch({ name: e.target.value })}
              />
            </label>
            <label
              className="automation-prompt-label"
              htmlFor="automation-prompt"
            >
              工作内容
            </label>
            <SkillQueryInput
              id="automation-prompt"
              multiline
              maxLength={12000}
              ref={inputRef}
              value={draft.prompt}
              skills={skills}
              status="ready"
              error={null}
              busy={saving}
              onChange={(prompt) => patch({ prompt })}
              onSelect={(skill, prompt) =>
                patch({
                  prompt,
                  skill: {
                    id: skill.id,
                    version: String(skill.version),
                    contentHash: skill.content_hash,
                  },
                })
              }
              onSubmit={() => {}}
              onRefresh={() => {}}
              onMenuOpenChange={() => {}}
            />
            <small className="automation-prompt-hint">
              输入 / 选择 Skill。说清商品范围、预算、收货地和你期待的输出。
            </small>
            {draft.skill && (
              <div className="automation-selected-skill">
                <Icon name="leaf" />
                {skills.find((s) => s.id === draft.skill?.id)?.title ??
                  draft.skill.id}
                <button
                  type="button"
                  aria-label="移除 Skill"
                  onClick={() => patch({ skill: null })}
                >
                  <Icon name="close" />
                </button>
              </div>
            )}
            <div className="automation-model">
              <span className="automation-model-label">执行模型</span>
              <ModelPicker sessionId={sessionId} request={request} busy={false} pending={false} disabled={saving}
                draft={{ value: draft.model, onChange: selection => patch({ model: {
                  profile_id: selection.profile_id, profile_revision: selection.profile_revision!,
                  window_id: selection.window_id, thinking: selection.thinking,
                } }) }} />
            </div>
          </div>
          <div className="automation-editor-settings">
            <h3>计划时间</h3>
            <div className="automation-segments">
              {(["daily", "weekly", "once", "cron"] as const).map((kind) => (
                <button
                  type="button"
                  key={kind}
                  aria-pressed={draft.schedule.kind === kind}
                  onClick={() => schedule({ kind })}
                >
                  {
                    {
                      daily: "每天",
                      weekly: "每周",
                      once: "单次",
                      cron: "自定义",
                    }[kind]
                  }
                </button>
              ))}
            </div>
            <div className="automation-time-fields">
            {["daily", "weekly"].includes(draft.schedule.kind) && (
              <label>
                执行时间
                <input
                  aria-label="执行时间"
                  type="time"
                  value={draft.schedule.time}
                  required
                  onChange={(e) => schedule({ time: e.target.value })}
                />
              </label>
            )}
            <label>
              时区
              <input
                list="automation-timezones"
                aria-label="时区"
                value={draft.schedule.timezone}
                onChange={(e) => schedule({ timezone: e.target.value })}
              />
              <datalist id="automation-timezones">
                {[
                  "Asia/Shanghai",
                  "Asia/Tokyo",
                  "Asia/Singapore",
                  "Europe/London",
                  "America/New_York",
                  "America/Los_Angeles",
                  "UTC",
                ].map((z) => (
                  <option key={z} value={z} />
                ))}
              </datalist>
            </label>
            </div>
            {draft.schedule.kind === "weekly" && (
              <div className="automation-weekdays" aria-label="星期">
                {[1, 2, 3, 4, 5, 6, 7].map((d) => (
                  <button
                    type="button"
                    key={d}
                    aria-label={"周" + "一二三四五六日"[d - 1]}
                    aria-pressed={draft.schedule.weekdays.includes(d)}
                    onClick={() =>
                      schedule({
                        weekdays: draft.schedule.weekdays.includes(d)
                          ? draft.schedule.weekdays.filter((v) => v !== d)
                          : [...draft.schedule.weekdays, d].sort(),
                      })
                    }
                  >
                    {"一二三四五六日"[d - 1]}
                  </button>
                ))}
              </div>
            )}
            {draft.schedule.kind === "once" && (
              <label>
                执行日期时间
                <input
                  required
                  aria-label="执行日期时间"
                  type="datetime-local"
                  value={draft.schedule.at ?? ""}
                  onChange={(e) => schedule({ at: e.target.value })}
                />
              </label>
            )}
            {draft.schedule.kind === "cron" && (
              <label>
                五字段 cron
                <input
                  aria-label="cron"
                  value={draft.schedule.cron}
                  onChange={(e) => schedule({ cron: e.target.value })}
                />
                <small>
                  分 时 日 月 星期；日与星期同时限定时满足其一即执行。
                </small>
              </label>
            )}
            <label className="automation-toggle-row">
              <span>
                设置到期日期<small>当天结束后不再启动新执行。</small>
              </span>
              <input
                type="checkbox"
                checked={expiry}
                onChange={(e) => {
                  setExpiry(e.target.checked);
                  if (!e.target.checked) schedule({ end_date: null });
                }}
              />
            </label>
            {expiry && (
              <input
                aria-label="到期日期"
                required
                type="date"
                value={draft.schedule.end_date ?? ""}
                onChange={(e) => schedule({ end_date: e.target.value })}
              />
            )}
            <fieldset className="automation-output">
              <legend>任务输出</legend>
              {(["independent", "merged"] as const).map((mode) => (
                <label key={mode}>
                  <input
                    type="radio"
                    name="output"
                    value={mode}
                    checked={draft.output_mode === mode}
                    onChange={() => patch({ output_mode: mode })}
                  />
                  <span>
                    {mode === "merged" ? "合并任务输出" : "独立任务输出"}
                    <small>
                      {mode === "merged"
                        ? "每次在同一会话继续，结合已有历史。"
                        : "每次新建会话，不带入上次对话。"}
                    </small>
                  </span>
                </label>
              ))}
            </fieldset>
            <label className="automation-toggle-row">
              <span>
                自动开始<small>到点自动查询与分析；关闭时需点击开始。</small>
              </span>
              <input
                type="checkbox"
                checked={draft.auto_start}
                onChange={(e) => patch({ auto_start: e.target.checked })}
              />
            </label>
            <p className="automation-authorization">
              <Icon name="info" />
              下单、取消订单及修改长期偏好仍需你确认。
              {editing ? "保存即确认本次设置，正在运行的轮次保持原配置。" : ""}
            </p>
            <div className="automation-preview">
              <strong>接下来执行</strong>
              {previewError ? (
                <p role="alert">{previewError}</p>
              ) : times.length ? (
                <details>
                  <summary>{times[0].slice(0, 16).replace("T", " ")} <small>查看计划时间</small></summary>
                  <ol>{times.map(t => <li key={t}>{t.slice(0, 16).replace("T", " ")} <small>{t.slice(19)}</small></li>)}</ol>
                </details>
              ) : (
                <small>正在核对时间…</small>
              )}
            </div>
          </div>
        </div>
        {error && (
          <p className="automation-error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <span>计划与结果保存到你的账户，刷新后仍可查看。</span>
          <button type="button" disabled={saving} onClick={onClose}>
            取消
          </button>
          <button
            className="primary"
            type="submit"
            disabled={
              saving ||
              !draft.model.profile_id ||
              !draft.prompt.trim() ||
              !times.length ||
              !!previewError
            }
          >
            {saving ? "保存中…" : editing ? "保存设置" : "创建自动化"}
          </button>
        </footer>
      </form>
    </Modal>
  );
}
