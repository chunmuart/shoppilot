import {
  HttpAgent,
  type AgentSubscriber,
  type HttpAgentConfig,
} from "@ag-ui/client";
import type { Message } from "@ag-ui/core";
import type {
  ChatMessage,
  CommerceSnapshot,
  ProductCard,
  PrepareOrderInput,
  TradeConfirmation,
  SessionSummary,
  SelectedSkill,
} from "../types";
import { readConfirmations, mergeConfirmations } from "./confirmations";
import { recoveringFetch } from "./recoveringFetch";
import { readActivities, replaceRunActivities, upsertActivities } from "./activities";
import { isSelectedSkill, readPublishedSkills, readSkillUsages } from "./skills";

const STORAGE_KEY = "shoppilot.agui.sessions.v1";
const BUYER_KEY = "shoppilot.buyer";
const ACTIVE_SESSION_KEY = "shoppilot.agui.active-session";
const MAX_SESSIONS = 12;
interface SavedSession {
  id: string;
  title: string;
  updatedAt: number;
  messages: ChatMessage[];
  products: ProductCard[];
  searchCompleted: boolean;
  runId?: string | null;
}
interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}
interface ClientOptions {
  url: string;
  storage?: StorageLike;
  fetch?: HttpAgentConfig["fetch"];
  buyerId?: string;
  accessToken?: string;
}

const newId = (): string => crypto.randomUUID();
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isAmount = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;
const isCount = (value: unknown): value is number =>
  isAmount(value) && Number.isSafeInteger(value);
const isStringArray = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((entry) => typeof entry === "string");

function isCurrency(value: unknown): value is string {
  if (typeof value !== "string" || !/^[A-Z]{3}$/.test(value)) return false;
  try {
    new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: value,
    }).format(0);
    return true;
  } catch {
    return false;
  }
}

function readLandedPrice(
  value: unknown,
  currency: string,
): ProductCard["landed_price"] {
  if (!isRecord(value)) return undefined;
  // 服务端报价失败时只返回原因；保留该真实状态，不补造金额。
  if (
    typeof value.unavailable_reason === "string" &&
    value.unavailable_reason.trim()
  ) {
    return {
      unavailable_reason: value.unavailable_reason,
    } as ProductCard["landed_price"];
  }
  if (
    typeof value.ship_to !== "string" ||
    !value.ship_to.trim() ||
    !isCurrency(value.currency) ||
    value.currency !== currency ||
    !isAmount(value.subtotal_major) ||
    !isAmount(value.freight_major) ||
    !isAmount(value.tariff_major) ||
    !isAmount(value.landed_total_major) ||
    !isAmount(value.tariff_rate) ||
    typeof value.de_minimis_applied !== "boolean"
  )
    return undefined;
  return {
    ship_to: value.ship_to,
    currency: value.currency,
    subtotal_major: value.subtotal_major,
    freight_major: value.freight_major,
    tariff_major: value.tariff_major,
    landed_total_major: value.landed_total_major,
    tariff_rate: value.tariff_rate,
    de_minimis_applied: value.de_minimis_applied,
  };
}

/** 目录卡片是服务端结构化结果，不从模型 Markdown 中猜价格或图片。 */
export function readProducts(value: unknown): ProductCard[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): ProductCard[] => {
    if (
      !isRecord(item) ||
      typeof item.product_id !== "string" ||
      !item.product_id.trim() ||
      typeof item.title !== "string" ||
      typeof item.brand !== "string" ||
      typeof item.category !== "string" ||
      typeof item.origin_country !== "string" ||
      !isAmount(item.price_major) ||
      !isCurrency(item.currency) ||
      typeof item.score !== "number" ||
      !Number.isFinite(item.score) ||
      !isStringArray(item.highlights) ||
      !Array.isArray(item.skus)
    )
      return [];
    const skus: ProductCard["skus"] = [];
    for (const sku of item.skus) {
      if (
        !isRecord(sku) ||
        typeof sku.sku_id !== "string" ||
        !sku.sku_id.trim() ||
        typeof sku.spec !== "string" ||
        !isAmount(sku.price_major) ||
        !isCurrency(sku.currency) ||
        !isCount(sku.stock)
      )
        return [];
      skus.push({
        sku_id: sku.sku_id,
        spec: sku.spec,
        price_major: sku.price_major,
        currency: sku.currency,
        stock: sku.stock,
      });
    }
    const card: ProductCard = {
      product_id: item.product_id,
      title: item.title,
      brand: item.brand,
      category: item.category,
      origin_country: item.origin_country,
      price_major: item.price_major,
      currency: item.currency,
      highlights: [...item.highlights],
      score: item.score,
      skus,
    };
    // 可选展示字段单独清洗，坏的评分/图片信息不能拖垮仍可展示的有效商品。
    for (const key of [
      "description",
      "updated_at",
      "image_alt",
      "source_platform",
      "canonical_product_id",
    ] as const) {
      if (typeof item[key] === "string") card[key] = item[key];
    }
    if (item.image_url === null || typeof item.image_url === "string")
      card.image_url = item.image_url;
    if (item.image_kind === "illustration" || item.image_kind === "placeholder")
      card.image_kind = item.image_kind;
    if (typeof item.rating_is_live === "boolean")
      card.rating_is_live = item.rating_is_live;
    if (item.rating_summary === null) card.rating_summary = null;
    else if (
      isRecord(item.rating_summary) &&
      isAmount(item.rating_summary.average) &&
      item.rating_summary.average <= 5 &&
      isCount(item.rating_summary.review_count)
    ) {
      card.rating_summary = {
        average: item.rating_summary.average,
        review_count: item.rating_summary.review_count,
      };
    }
    for (const key of ["ships_to", "material_tags"] as const) {
      if (isStringArray(item[key])) card[key] = [...item[key]];
    }
    if (isRecord(item.dimensions_cm)) {
      const dimensions: NonNullable<ProductCard["dimensions_cm"]> = {};
      let valid = true;
      for (const key of ["length", "width", "height"] as const) {
        const dimension = item.dimensions_cm[key];
        if (dimension === undefined) continue;
        if (!isAmount(dimension)) {
          valid = false;
          break;
        }
        dimensions[key] = dimension;
      }
      if (valid) card.dimensions_cm = dimensions;
    }
    if (isAmount(item.weight_kg)) card.weight_kg = item.weight_kg;
    if (
      typeof item.default_sku_id === "string" &&
      skus.some((sku) => sku.sku_id === item.default_sku_id)
    ) {
      card.default_sku_id = item.default_sku_id;
    }
    if (isAmount(item.source_price_major) && isCurrency(item.source_currency)) {
      card.source_price_major = item.source_price_major;
      card.source_currency = item.source_currency;
    }
    const landed = readLandedPrice(item.landed_price, card.currency);
    if (landed) card.landed_price = landed;
    return [card];
  });
}

function readMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(
      (item): item is ChatMessage =>
        isRecord(item) &&
        typeof item.id === "string" &&
        typeof item.content === "string" &&
        (item.role === "user" || item.role === "assistant"),
    );
}

// 仅保留工作窗口之前的原文；窗口内由 SDK 快照整体替换，不能把流式草稿再合并回来。
function historyPrefix(history: ChatMessage[], window: ChatMessage[]): ChatMessage[] {
  const first = window[0]?.id;
  const index = history.findIndex(message => message.id === first);
  return index > 0 ? history.slice(0, index) : [];
}

function displayMessages(
  messages: ReadonlyArray<Readonly<Message>>,
  runForMessage: (id: string) => string | undefined = () => undefined,
): ChatMessage[] {
  return messages.flatMap((message) => {
    if (message.role !== "user" && message.role !== "assistant") return [];
    const content = typeof message.content === "string" ? message.content : "";
    return content ? [{ id: message.id, role: message.role, content, ...(runForMessage(message.id) ? {runId: runForMessage(message.id)} : {}) }] : [];
  });
}

function emptySnapshot(sessionId = newId()): CommerceSnapshot {
  return {
    sessionId,
    messages: [],
    products: [],
    productHistory: [],
    events: [],
    activities: [],
    status: "idle",
    step: "随时开始新的选购",
    error: null,
    searchCompleted: false,
    history: [],
    confirmations: [],
    toolApprovals: [],
    confirmationBusy: false,
    confirmationError: null,
    recoverableRunId: null,
    historyError: null,
    skills: [], skillsStatus: "loading", skillsError: null, skillUsages: [],
  };
}

const EVENT_LABELS: Record<string, string> = {
  RUN_STARTED: "开始本轮选购",
  RUN_FINISHED: "本轮已完成",
  RUN_ERROR: "本轮遇到问题",
  TEXT_MESSAGE_START: "正在整理建议",
  TEXT_MESSAGE_END: "建议已生成",
  TOOL_CALL_START: "调用工具",
  TOOL_CALL_END: "工具参数已就绪",
  TOOL_CALL_RESULT: "收到工具结果",
  STATE_SNAPSHOT: "更新商品与进度",
  STATE_DELTA: "更新选购状态",
  MESSAGES_SNAPSHOT: "同步最终建议",
};

function connectionError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  const status = message.match(/HTTP\s+(\d{3})/i)?.[1];
  if (status === "401" || status === "403")
    return "连接被服务拒绝，请检查访问配置后重试。";
  if (status === "422" || status === "400")
    return "这次请求未被接受，请调整内容后重试。";
  if (status) return "选购服务暂时不可用，请稍后重试。";
  return "连接已中断，本轮尚未完成。请检查网络或服务后重试。";
}

/** 一次 run 一个 SDK 实例；切换会话和停止后，迟到事件不能覆盖当前页面。 */
export class CommerceClient {
  private snapshot: CommerceSnapshot = emptySnapshot();
  private sessions: SavedSession[] = [];
  private listeners = new Set<() => void>();
  private active?: { agent: HttpAgent; runId: string };
  private buyerId = "pao-coder";
  private mutationId: string | undefined;
  private confirmationRevision = 0;
  private serverHistory: SessionSummary[] = [];
  private directoryRevision = 0;
  private historyRevision = 0;
  private skillsRevision = 0;
  private activityRevision = 0;
  private restoreLatestSession = true;

  constructor(private options: ClientOptions) {
    // 身份、当前会话和正文缓存分别读取，坏缓存不能阻断服务端恢复。
    let storedBuyer: string | null = null;
    try { storedBuyer = options.storage?.getItem(BUYER_KEY) ?? null; } catch {}
    this.buyerId = options.buyerId || "pao-coder";
    const cacheBelongsToBuyer = storedBuyer === this.buyerId;
    try {
      if (!cacheBelongsToBuyer) {
        options.storage?.setItem(STORAGE_KEY, "[]");
        options.storage?.setItem(ACTIVE_SESSION_KEY, "");
      }
      options.storage?.setItem(BUYER_KEY, this.buyerId);
    } catch { /* 存储不可写时仍使用已读取的买家身份。 */ }
    try {
      const activeId = cacheBelongsToBuyer ? options.storage?.getItem(ACTIVE_SESSION_KEY) : null;
      if (activeId) {
        this.snapshot = emptySnapshot(activeId);
        // 本地空会话不存在于数据库时，启动仍恢复该买家最近的持久会话。
      }
    } catch {}
    try {
      const raw: unknown = JSON.parse(
        options.storage?.getItem(STORAGE_KEY) ?? "[]",
      );
      if (cacheBelongsToBuyer && Array.isArray(raw))
        this.sessions = raw
          .filter(isRecord)
          .flatMap((entry) => {
            if (
              typeof entry.id !== "string" ||
              typeof entry.title !== "string" ||
              typeof entry.updatedAt !== "number"
            )
              return [];
            return [
              {
                id: entry.id,
                title: entry.title,
                updatedAt: entry.updatedAt,
                messages: readMessages(entry.messages),
                products: readProducts(entry.products),
                searchCompleted: entry.searchCompleted === true,
                runId: typeof entry.runId === "string" ? entry.runId : null,
              },
            ];
          })
          .slice(0, MAX_SESSIONS);
    } catch {
      /* 无痕模式、存储配额或旧格式不应阻断选购。 */
    }
    try {
      const saved = this.sessions.find((entry) => entry.id === this.snapshot.sessionId);
      if (saved)
        this.snapshot = {
          ...emptySnapshot(saved.id),
          messages: saved.messages,
          products: saved.products,
          searchCompleted: saved.searchCompleted,
          step: "已恢复本机选购记录",
          recoverableRunId: saved.runId ?? null,
        };
    } catch {
      /* 确认记录仍从服务端恢复。 */
    }
    this.snapshot = { ...this.snapshot, history: this.history() };
  }

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(patch: Partial<CommerceSnapshot>) {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  private history() {
    const local = this.sessions.map(({ id, title, updatedAt }) => ({
      id,
      title,
      updatedAt,
      source: "local" as const,
    }));
    return [...this.serverHistory, ...local.filter((item) => !this.serverHistory.some((saved) => saved.id === item.id))]
      .sort((a, b) => b.updatedAt - a.updatedAt);
  }
  private save() {
    if (!this.snapshot.messages.length && !this.snapshot.confirmations.length)
      return;
    const entry: SavedSession = {
      id: this.snapshot.sessionId,
      title:
        this.snapshot.messages
          .find((message) => message.role === "user")
          ?.content.slice(0, 32) ?? "选购记录",
      updatedAt: Date.now(),
      messages: this.snapshot.messages.slice(-100),
      products: this.snapshot.products,
      searchCompleted: this.snapshot.searchCompleted,
      runId: this.snapshot.recoverableRunId,
    };
    this.sessions = [
      entry,
      ...this.sessions.filter((session) => session.id !== entry.id),
    ].slice(0, MAX_SESSIONS);
    try {
      this.options.storage?.setItem(STORAGE_KEY, JSON.stringify(this.sessions));
    } catch {
      /* 存储不可用时仍保留当前内存会话。 */
    }
    this.saveActiveSession();
    this.update({ history: this.history() });
  }

  submit = async (rawQuery: string, selectedSkill?: SelectedSkill): Promise<void> => {
    const query = rawQuery.trim();
    if (!query || this.active) return;
    if (selectedSkill !== undefined && !isSelectedSkill(selectedSkill)) {
      this.update({ error: "所选方案信息无效，请刷新方案后重新选择。" });
      return;
    }
    if (this.snapshot.toolApprovals?.length) {
      this.update({ error: "请先批准或拒绝待处理的记忆操作。" });
      return;
    }
    if (this.snapshot.recoverableRunId) {
      this.update({ error: "上一轮尚可恢复，请先恢复或明确停止该运行。" });
      return;
    }
    ++this.historyRevision;
    this.restoreLatestSession = false;
    const runId = newId();
    const messages: ChatMessage[] = [
      ...this.snapshot.messages,
      { id: newId(), role: "user", content: query, runId },
    ];
    await this.executeRun(runId, messages, false, selectedSkill);
  };

  submitForm = async (query: string, runId: string): Promise<void> => {
    if (this.active || this.snapshot.toolApprovals?.length || this.snapshot.recoverableRunId) {
      this.update({error:"请先完成或停止当前运行及待处理确认。"});return;
    }
    if (!query.trim() || !/^form-run-[a-f0-9]{32}$/.test(runId)) return;
    ++this.historyRevision;this.restoreLatestSession=false;
    // 使用服务端保存的运行 ID 和稳定消息 ID，刷新后继续不会创建重复运行。
    const messages:ChatMessage[]=[...this.snapshot.messages.filter(m=>m.runId!==runId&&m.id!==runId+":user"),
      {id:runId+":user",role:"user",content:query,runId}];
    await this.executeRun(runId,messages);
  };

  private async executeRun(runId: string, messages: ChatMessage[], resume = false, selectedSkill?: SelectedSkill, approval?: { id: string; approved: boolean }, allowVersionRestart = true): Promise<void> {
    const priorSessionId=this.snapshot.sessionId;
    const activityRevision = ++this.activityRevision;
    let destination=this.snapshot.products.find(p=>p.landed_price?.ship_to)?.landed_price?.ship_to;
    let restartVersion=false;
    const journaled = /\/ag-ui\/run\/?$/.test(this.options.url);
    const baseFetch = this.options.fetch ?? globalThis.fetch;
    const workingMessages = messages.slice(-100);
    const archivedMessages = historyPrefix(messages.length >= this.snapshot.messages.length ? messages : this.snapshot.messages, workingMessages);
    const agent = new HttpAgent({
      url: this.options.url,
      threadId: this.snapshot.sessionId,
      initialMessages: workingMessages.map(({ id, role, content }) => ({
        id,
        role,
        content,
      })),
      initialState: {},
      headers: this.authHeaders(),
      fetch: journaled ? recoveringFetch(baseFetch, {
        runId, resume,
        eventsUrl: `${this.options.url.replace(/\/run\/?$/, "")}/runs/${encodeURIComponent(runId)}/events?buyer_id=${encodeURIComponent(this.buyerId)}`,
        onReconnect: () => { if (current()) this.update({ step: "连接中断，正在恢复已保存的进度" }); },
        onConnected: () => {
          if (current() && this.snapshot.step === "连接中断，正在恢复已保存的进度") this.update({ step: "连接已恢复，继续接收本轮进度" });
        },
      }) : baseFetch,
    });
    const knownRuns = new Map(messages.map(message => [message.id, message.runId]));
    const initialIds = new Set(workingMessages.map(message => message.id));
    const runForMessage = (id: string) => knownRuns.get(id) ?? (initialIds.has(id) ? undefined : runId);
    const active = { agent, runId };
    this.active = active;
    const current = () => this.active === active;
    let terminal = false;
    this.update({
      messages: [...archivedMessages, ...workingMessages],
      products: [],
      skillUsages: [],
      events: [],
      error: null,
      searchCompleted: false,
      status: "running",
      step: "正在理解你的需求",
      recoverableRunId: runId,
    });
    this.save();
    const fail = (message: string) => {
      if (current())
        this.update({
          status: "error",
          error: message,
          step: "暂时没能完成，请重试",
        });
    };
    const subscriber: AgentSubscriber = {
      onMessagesChanged: ({ messages: next }) => {
        if (current()) this.update({ messages: [...archivedMessages, ...displayMessages(next, runForMessage)] });
      },
      onStateChanged: ({ state }) => {
        if (!current() || !isRecord(state)) return;
        if (typeof state.resumeDestination === "string" && /^[A-Z]{2}$/.test(state.resumeDestination)) destination=state.resumeDestination;
        const progress = Array.isArray(state.progress)
          ? state.progress.filter(isRecord)
          : [];
        const ongoing = [...progress]
          .reverse()
          .find((step) => step.status === "running");
        const latest = ongoing ?? progress[progress.length - 1];
        this.update({
          ...(Array.isArray(state.activities) ? { activities: replaceRunActivities(this.snapshot.activities ?? [], readActivities(state.activities, runId), runId) } : {}),
          toolApprovals: readApprovals(state.toolApprovals),
          products: readProducts(state.products),
          shoppingForm: state.shoppingForm ?? null,
      skillUsages: readSkillUsages(state.skillUsages),
          searchCompleted: state.searchCompleted === true,
          confirmations: mergeConfirmations(
            this.snapshot.confirmations,
            this.ownedConfirmations(state.confirmations),
          ),
          ...(typeof latest?.label === "string" ? { step: latest.label } : {}),
        });
      },
      onEvent: ({ event }) => {
        if (current() && event.type === "CUSTOM" && event.name === "activity.upsert") {
          const activities = readActivities([event.value], runId);
          if (activities.length) this.update({activities: upsertActivities(this.snapshot.activities ?? [], activities)});
          return;
        }
        if (
          !current() ||
          ["TEXT_MESSAGE_CONTENT", "TOOL_CALL_ARGS"].includes(event.type)
        )
          return;
        // 诊断只存事件摘要，避免 token 级更新拖慢商品区，也不持久化完整工具参数。
        this.update({
          events: [
            ...this.snapshot.events,
            {
              id: newId(),
              type: event.type,
              label: EVENT_LABELS[event.type] ?? event.type,
              timestamp: event.timestamp ?? Date.now(),
            },
          ].slice(-80),
        });
      },
      onRunFinishedEvent: ({ outcome }) => {
        terminal = true;
        if (current()) this.update({ recoverableRunId: null });
        if (outcome === "interrupt" && !this.snapshot.toolApprovals?.length)
          fail("当前页面暂不支持此确认流程，请重新描述需求。");
        else if (current())
          this.update({ status: "idle", step: this.snapshot.toolApprovals?.length ? "请确认长期记忆变更" : "已为你整理好选购建议" });
      },
      onRunErrorEvent: ({ event }) => {
        terminal = true;
        if (current()) this.update({ recoverableRunId: null });
        if (event.code === "SESSION_VERSION_CHANGED" && current() && allowVersionRestart && !approval && !resume && !selectedSkill) {
          restartVersion=true;
          this.update({status:"idle",error:null,step:"选购环境已更新，正在保留旧记录并继续查询"});
          return;
        }
        if (event.code === "CANCELLED" && current()) this.update({ status: "stopped", error: null, step: "本轮已停止，可继续补充需求" });
        else fail(event.message);
      },
      onRunFailed: ({ error }) => {
        if (!this.snapshot.error) fail(connectionError(error));
      },
    };
    try {
      await agent.runAgent(
        {
          runId,
          tools: [],
          context: [],
          ...(approval ? { resume: [{ interruptId: approval.id, status: "resolved" as const, payload: { approved: approval.approved } }] } : {}),
          forwardedProps: {
            buyerId: this.buyerId,
            locale: "zh-CN",
            currency: "CNY",
            ...(selectedSkill ? { selectedSkill: { ...selectedSkill } } : {}),
          },
        },
        subscriber,
      );
      if (current() && !terminal) fail("连接已中断，本轮尚未完成。请重试。");
    } catch (error) {
      if (!this.snapshot.error) fail(connectionError(error));
    } finally {
      if (current()) {
        this.active = undefined;
        this.save();
      }
    }
    if ((approval || resume) && terminal && journaled) {
      await this.refreshSessionActivities(priorSessionId, activityRevision);
    }
    if (restartVersion && this.snapshot.sessionId===priorSessionId && !this.active) {
      const latest=[...messages].reverse().find(m=>m.role==="user");
      if (latest) {
        // 只续发本轮明确需求与已知目的地，不复制过期 Skill、助手结论或工具上下文。
        const query=latest.content+(destination && !/收货|寄到|寄往|配送至/.test(latest.content) ? `\n收货国家：${destination}。` : "");
        this.reset();
        const nextRun=newId();
        await this.executeRun(nextRun,[{id:newId(),role:"user",content:query,runId:nextRun}],false,undefined,undefined,false);
      }
    }
  }

  private authHeaders(): Record<string, string> {
    return this.options.accessToken ? { Authorization: `Bearer ${this.options.accessToken}` } : {};
  }

  private async refreshSessionActivities(sessionId: string, revision: number) {
    // 审批资源的终态由服务端跨轮核对，不能把点击批准当成工具已成功。
    if (sessionId !== this.snapshot.sessionId || this.active) return;
    const historyRevision = this.historyRevision;
    try {
      const data = await this.journalRequest(`/sessions/${encodeURIComponent(sessionId)}`);
      if (revision === this.activityRevision && historyRevision === this.historyRevision && sessionId === this.snapshot.sessionId && !this.active &&
          isRecord(data.run) && data.run.threadId === sessionId && Array.isArray(data.activities)) {
        this.update({activities: readActivities(data.activities)});
      }
    } catch { /* 保留已有步骤；稍后重开会话会重新读取权威记录。 */ }
  }

  private async journalRequest(path: string, method = "GET"): Promise<Record<string, unknown>> {
    const base = this.options.url.replace(/\/run\/?$/, "");
    const separator = path.includes("?") ? "&" : "?";
    const response = await (this.options.fetch ?? globalThis.fetch)(`${base}${path}${separator}buyer_id=${encodeURIComponent(this.buyerId)}`, {
      method, headers: this.authHeaders(),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data: unknown = await response.json().catch(() => {
      throw new Error("暂时无法读取选购记录，请稍后重试。");
    });
    if (!isRecord(data)) throw new Error("服务端记录格式无效");
    return data;
  }

  refreshHistory = (): Promise<void> => this.initialize(false);

  initialize = async (restoreSession = true): Promise<void> => {
    if (!/\/ag-ui\/run\/?$/.test(this.options.url)) return;
    const revision = ++this.directoryRevision;
    const historyRevision = this.historyRevision;
    try {
      const [data, formData, originData] = await Promise.all([
        this.journalRequest("/sessions"),
        this.workspaceRequest("/shopping-forms").catch(()=>null),
        this.workspaceRequest("/automations/session-origins").catch(()=>null),
      ]);
      if (revision !== this.directoryRevision) return;
      // 来源来自持久执行关联；短暂读取失败时保留已知标记，不猜会话 ID 或标题。
      const automated = new Set(Array.isArray(originData?.session_ids)
        ? originData.session_ids.filter((id): id is string => typeof id === "string")
        : this.serverHistory.filter(item => item.origin === "automation").map(item => item.id));
      this.serverHistory = Array.isArray(data.sessions) ? data.sessions.filter(isRecord).flatMap((item) =>
        typeof item.id === "string" && typeof item.title === "string" && typeof item.updatedAt === "number"
          ? [{ id: item.id, title: item.title, updatedAt: item.updatedAt, source: "server" as const, ...(automated.has(item.id) ? { origin: "automation" as const } : {}) }] : []) : [];
      this.update({ history: this.history(), historyError: null });
      if (!restoreSession || historyRevision !== this.historyRevision) return;
      const form = isRecord(formData?.form) ? formData.form : null;
      const latestHistoryTime = Math.max(0,...this.serverHistory.map(item=>item.updatedAt));
      const restoreForm = form && typeof form.session_id==="string" && typeof form.created_at==="number"
        && (form.session_id===this.snapshot.sessionId || (this.restoreLatestSession && !this.serverHistory.some(item=>item.id===this.snapshot.sessionId) && form.created_at>latestHistoryTime));
      if (restoreForm && !this.active) {
        this.restoreLatestSession=false;
        this.update({sessionId:form.session_id as string,shoppingForm:form});
        this.saveActiveSession();
      }
      const chosen = this.serverHistory.find((item) => item.id === this.snapshot.sessionId)
        ?? (this.restoreLatestSession
          ? [...this.serverHistory].sort((a, b) => b.updatedAt - a.updatedAt)[0] : undefined);
      if (chosen && !this.active) {
        this.restoreLatestSession = false;
        this.update({ sessionId: chosen.id });
        this.saveActiveSession();
        await this.loadSession(chosen.id);
      }
    } catch {
      if (revision === this.directoryRevision) this.update({ historyError: "服务端历史暂不可用，当前显示本机缓存。" });
    }
  };

  private applyServerRun(run: Record<string, unknown>) {
    const state = isRecord(run.state) ? run.state : {};
    const running = run.status === "running";
    const messages = readMessages(run.messages);
    this.update({
      ...(typeof run.runId === "string" ? { activities: replaceRunActivities(this.snapshot.activities ?? [], readActivities(state.activities, run.runId), run.runId) } : {}),
      toolApprovals: readApprovals(state.toolApprovals),
      messages: [...historyPrefix(this.snapshot.messages, messages), ...messages], products: readProducts(state.products),
      shoppingForm: state.shoppingForm ?? null,
      skillUsages: readSkillUsages(state.skillUsages),
      searchCompleted: state.searchCompleted === true,
      confirmations: mergeConfirmations(this.snapshot.confirmations, this.ownedConfirmations(state.confirmations)),
      recoverableRunId: running && typeof run.runId === "string" ? run.runId : null,
      status: running ? "running" : run.status === "completed" ? "idle" : run.status === "stopped" ? "stopped" : "error",
      step: running ? "正在恢复服务端执行进度" : "已恢复服务端选购记录",
      error: ["error", "interrupted"].includes(String(run.status)) ? "该运行未完成，已恢复保存的内容；可重新提交需求。" : null,
    });
  }

  private async loadSession(id: string) {
    const revision = ++this.historyRevision;
    try {
      const data = await this.journalRequest(`/sessions/${encodeURIComponent(id)}`);
      if (revision !== this.historyRevision || id !== this.snapshot.sessionId || this.active) return;
      if (!isRecord(data.run) || data.run.threadId !== id) throw new Error("服务端运行记录与当前会话不一致");
      this.applyServerRun(data.run);
      this.update({
        ...(Array.isArray(data.messages) ? {messages: readMessages(data.messages)} : {}),
        ...(Array.isArray(data.activities) ? {activities: readActivities(data.activities)} : {}),
        productHistory: Array.isArray(data.productHistory) ? data.productHistory.filter(isRecord).flatMap(item =>
          typeof item.runId === "string" && typeof item.updatedAt === "number"
            ? [{runId: item.runId, updatedAt: item.updatedAt, products: readProducts(item.products)}] : []) : [],
        events: Array.isArray(data.events) ? data.events.filter(isRecord).flatMap(item =>
          typeof item.id === "string" && typeof item.type === "string"
            ? [{id: item.id, type: item.type, label: EVENT_LABELS[item.type] ?? item.type,
                timestamp: typeof item.timestamp === "number" ? item.timestamp : null}] : []) : [],
        historyError: null,
      });
      this.save();
      if (data.run.status === "running") await this.resume();
    } catch {
      if (revision === this.historyRevision && id === this.snapshot.sessionId)
        this.update({ historyError: "该会话暂时无法从服务端恢复，显示已有缓存。", status: "error", error: "连接恢复未完成，可再次打开本段历史重试。" });
    }
  }

  resolveToolApproval = async (id: string, approved: boolean): Promise<void> => {
    if (this.active || !this.snapshot.toolApprovals?.some(item => item.id === id)) return;
    const runId = newId();
    const messages: ChatMessage[] = [...this.snapshot.messages,
      { id: newId(), role: "user", content: approved ? "批准这次长期记忆操作" : "拒绝这次长期记忆操作", runId }];
    await this.executeRun(runId, messages, false, undefined, { id, approved });
  };

  resume = async (): Promise<void> => {
    const runId = this.snapshot.recoverableRunId, sessionId = this.snapshot.sessionId;
    if (!runId || this.active) return;
    try {
      const run = await this.journalRequest(`/runs/${encodeURIComponent(runId)}`);
      if (sessionId !== this.snapshot.sessionId) return;
      if (run.threadId !== sessionId) throw new Error("运行不属于当前会话");
      this.applyServerRun(run);
      if (run.status === "running" && isRecord(run.input)) {
        // 新 SDK 实例从本轮日志起点重放，保持 START/工具流协议状态完整，不重复执行模型。
        await this.executeRun(runId, readMessages(run.input.messages), true);
      } else {
        this.save();
        await this.refreshSessionActivities(sessionId, this.activityRevision);
      }
    } catch (error) {
      if (sessionId === this.snapshot.sessionId) this.update({ status: "error", error: `暂时无法恢复本轮：${connectionError(error)}`,
        ...(String(error).includes("404") ? { recoverableRunId: null } : {}) });
    }
  };

  private ownedConfirmations(value: unknown) {
    return readConfirmations(value).filter(
      (item) =>
        item.buyer_id === this.buyerId &&
        item.session_id === this.snapshot.sessionId,
    );
  }

  private async confirmationRequest(
    path: string,
    body?: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const base = this.options.url.replace(/\/ag-ui\/run\/?$/, "");
    const response = await (this.options.fetch ?? globalThis.fetch)(
      `${base}${path}`,
      {
        method: body ? "POST" : "GET",
        headers: { ...this.authHeaders(), ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      },
    );
    const data: unknown = await response.json().catch(() => {
      throw new Error("确认服务暂时不可用，请刷新状态后重试。");
    });
    if (!response.ok) {
      const detail = isRecord(data) ? data.detail : null;
      throw Object.assign(new Error(
        isRecord(detail) && typeof detail.message === "string"
          ? detail.message
          : typeof detail === "string"
            ? detail
            : "确认服务暂时不可用，请刷新状态后重试。",
      ), { status: response.status });
    }
    if (!isRecord(data)) throw new Error("确认服务返回格式无效，请刷新状态。");
    return data;
  }

  workspaceRequest = async (path: string, method = "GET", body?: Record<string, unknown>): Promise<Record<string, unknown>> => {
    const base = this.options.url.replace(/\/ag-ui\/run\/?$/, "");
    const separator = path.includes("?") ? "&" : "?";
    const response = await (this.options.fetch ?? globalThis.fetch)(`${base}${path}${separator}buyer_id=${encodeURIComponent(this.buyerId)}`, {
      method, headers: { ...this.authHeaders(), ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify((path === "/context/compact" || path.startsWith("/models/") || path.startsWith("/model-connections") || /^\/sessions\/[^/]+\/model$/.test(path) || /^\/(automations|automation-runs)(\/|$)/.test(path)) ? {...body,buyer_id:this.buyerId} : body) : undefined,
    });
    const data: unknown = await response.json().catch(() => { throw new Error("服务暂时不可用，请稍后重试。输入已保留。"); });
    if (!response.ok) {
      const detail = isRecord(data) ? data.detail : null;
      throw Object.assign(new Error(typeof detail === "string" ? detail : isRecord(detail) && typeof detail.message === "string" ? detail.message : response.status === 401 || response.status === 403
        ? "无法访问个人资料，请检查当前登录身份。" : "未能保存或读取，请刷新后重试。输入已保留。"),{status:response.status});
    }
    if (!isRecord(data)) throw new Error("个人资料服务返回格式无效");
    return data;
  };

  refreshSkills = async (): Promise<void> => {
    const revision = ++this.skillsRevision;
    this.update({ skillsStatus: "loading", skillsError: null });
    try {
      const base = this.options.url.replace(/\/ag-ui\/run\/?$/, "");
      const response = await (this.options.fetch ?? globalThis.fetch)(`${base}/skills?buyer_id=${encodeURIComponent(this.buyerId)}`, { headers: this.authHeaders() });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const skills = readPublishedSkills(await response.json());
      if (revision === this.skillsRevision) this.update({ skills, skillsStatus: "ready", skillsError: null });
    } catch (error) {
      if (revision !== this.skillsRevision) return;
      const status = error instanceof Error ? error.message : "";
      this.update({ skills: [], skillsStatus: "error", skillsError: status.includes("409")
        ? "选购方案刚刚更新，请刷新后再选。"
        : /401|403/.test(status) ? "暂时无法读取选购方案，请检查访问身份后重试。"
        : "选购方案暂时无法加载，仍可直接描述需求。" });
    }
  };

  refreshConfirmations = async (): Promise<void> => {
    const sessionId = this.snapshot.sessionId;
    const revision = ++this.confirmationRevision;
    // 首轮执行可能先发起确认列表请求，此时服务端尚未建立会话。
    const newSession = !this.snapshot.confirmations.length
      && (!this.snapshot.messages.length || this.snapshot.status === "running")
      && !this.snapshot.history.some(entry => entry.id === sessionId && entry.source === "server");
    try {
      const query = new URLSearchParams({
        buyer_id: this.buyerId,
        session_id: sessionId,
      });
      const data = await this.confirmationRequest(`/confirmations?${query}`);
      if (
        sessionId !== this.snapshot.sessionId ||
        revision !== this.confirmationRevision
      )
        return;
      this.update({
        confirmations: this.ownedConfirmations(data.confirmations),
        confirmationError: null,
      });
      this.save();
    } catch (error) {
      if (
        sessionId === this.snapshot.sessionId &&
        revision === this.confirmationRevision
      ) {
        // 尚未运行的新会话还没有服务端 owner 记录；只对这个正常 404 视作空列表。
        const emptyNewSession = newSession && !this.snapshot.confirmations.length;
        if (emptyNewSession && error instanceof Error && "status" in error && error.status === 404) {
          this.update({ confirmations: [], confirmationError: null });
          return;
        }
        this.update({
          confirmationError:
            error instanceof Error
              ? error.message
              : "暂时无法读取确认状态，请刷新重试。",
        });
      }
    }
  };

  private async mutateConfirmation(
    path: string,
    body: Record<string, unknown>,
  ): Promise<boolean> {
    if (this.mutationId) return false;
    const mutationId = newId(),
      sessionId = this.snapshot.sessionId;
    this.mutationId = mutationId;
    ++this.confirmationRevision;
    this.update({ confirmationBusy: true, confirmationError: null });
    try {
      const data = await this.confirmationRequest(path, {
        ...body,
        buyer_id: this.buyerId,
        session_id: sessionId,
      });
      if (sessionId !== this.snapshot.sessionId) return false;
      const next = this.ownedConfirmations([data.confirmation]);
      if (!next.length)
        throw new Error("确认结果格式无效，请刷新状态，避免重复准备操作。");
      ++this.confirmationRevision;
      this.update({
        confirmations: mergeConfirmations(this.snapshot.confirmations, next),
      });
      this.save();
      return true;
    } catch (error) {
      if (sessionId === this.snapshot.sessionId)
        this.update({
          confirmationError: `${error instanceof Error ? error.message : "连接中断，操作结果尚未确定。"} 可刷新确认状态；重试同一确认不会重复执行。`,
        });
      return false;
    } finally {
      if (this.mutationId === mutationId) this.mutationId = undefined;
      if (sessionId === this.snapshot.sessionId)
        this.update({ confirmationBusy: false });
    }
  }
  prepareOrder = (input: PrepareOrderInput) =>
    this.mutateConfirmation("/confirmations/orders", { ...input });
  prepareCancel = (orderId: string, reason: string) =>
    this.mutateConfirmation(`/orders/${encodeURIComponent(orderId)}/cancel`, {
      reason,
    });
  resolveConfirmation = (confirmation: TradeConfirmation, approved: boolean) =>
    this.mutateConfirmation(
      `/confirmations/${encodeURIComponent(confirmation.confirmation_id)}/resolve`,
      {
        snapshot_hash: confirmation.snapshot_hash,
        approved,
      },
    );

  stop = () => {
    const active = this.active;
    const runId = active?.runId ?? this.snapshot.recoverableRunId;
    if (!runId) return;
    const sessionId = this.snapshot.sessionId;
    this.active = undefined;
    active?.agent.abortRun();
    this.update({
      status: "stopped",
      error: null,
      step: "正在确认停止请求",
    });
    this.save();
    if (/\/ag-ui\/run\/?$/.test(this.options.url)) void this.journalRequest(`/runs/${encodeURIComponent(runId)}/cancel`, "POST").then(async (run) => {
      if (sessionId !== this.snapshot.sessionId) return;
      this.applyServerRun(run);
      if (run.status === "running") {
        this.update({ status: "running", step: "停止请求已送达，等待服务端收尾" });
        for (let attempt = 0; attempt < 30 && run.status === "running"; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 200));
          if (sessionId !== this.snapshot.sessionId) return;
          run = await this.journalRequest(`/runs/${encodeURIComponent(runId)}`);
        }
        if (sessionId !== this.snapshot.sessionId) return;
        this.applyServerRun(run);
        if (run.status === "running") this.update({ status: "error", error: "服务端已记录停止请求，收尾仍在进行。可恢复本轮查询最新状态。" });
      }
      this.save();
    }).catch((error) => {
      if (sessionId === this.snapshot.sessionId) this.update({ status: "error", recoverableRunId: runId,
        error: `停止请求尚未确认，服务端可能仍在执行。${connectionError(error)}` });
    });
    else this.update({ recoverableRunId: null });
  };
  detach = () => {
    const active = this.active;
    this.active = undefined;
    active?.agent.abortRun();
    this.save();
  };
  private saveActiveSession() {
    try {
      this.options.storage?.setItem(
        ACTIVE_SESSION_KEY,
        this.snapshot.sessionId,
      );
    } catch {
      /* 存储不可用不阻断交互。 */
    }
  }
  reset = () => {
    this.restoreLatestSession = false;
    this.detach();
    ++this.historyRevision;
    this.save();
    this.update({ ...emptySnapshot(), history: this.history() });
    this.saveActiveSession();
  };
  setSession = (id: string) => {
    this.restoreLatestSession = false;
    if (id === this.snapshot.sessionId) {
      if (!this.active && /\/ag-ui\/run\/?$/.test(this.options.url)) void this.loadSession(id);
      return;
    }
    const session = this.sessions.find((entry) => entry.id === id);
    const remote = this.serverHistory.some((entry) => entry.id === id);
    if (!session && !remote) return;
    this.detach();
    ++this.historyRevision;
    this.save();
    this.update({
      ...emptySnapshot(id),
      messages: session?.messages ?? [],
      products: session?.products ?? [],
      searchCompleted: session?.searchCompleted ?? false,
      recoverableRunId: session?.runId ?? null,
      history: this.history(),
      step: "已恢复本机选购记录",
    });
    this.saveActiveSession();
    if (/\/ag-ui\/run\/?$/.test(this.options.url)) void this.loadSession(id);
  };
}

function readApprovals(value: unknown): import("../types").ToolApproval[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).flatMap(item => typeof item.id === "string" && typeof item.tool === "string" && typeof item.label === "string"
    ? [{ id: item.id, tool: item.tool, label: item.label, arguments: typeof item.arguments === "string" || isRecord(item.arguments) ? item.arguments : {} }] : []);
}
