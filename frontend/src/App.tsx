import Automations from "./components/Automations";
import ModelPicker from "./components/ModelPicker";
import HistorySearch from "./components/HistorySearch";
import ContextWorkspace, { useContextWorkspace } from "./components/ContextWorkspace";
import ShoppingForm from "./components/ShoppingForm";
import { ToolApprovalCards } from "./components/ToolApprovalCards";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useCommerceAgent } from "./hooks/useCommerceAgent";
import { readProducts } from "./lib/commerceClient";
import type { ProductCard, PublishedSkill } from "./types";
import Icon from "./components/Icon";
import ConversationTurns from "./components/ConversationTurns";
import EventTimeline from "./components/EventTimeline";
import ProductCards, { ProductImage } from "./components/ProductCards";
import ProductHistory from "./components/ProductHistory";
import ProductDetail from "./components/ProductDetail";
import ConfirmationCards from "./components/ConfirmationCards";
import OrderIntentForm from "./components/OrderIntentForm";
import ProductComparison from "./components/ProductComparison";
import ShoppingPlans, { SkillRunStatus } from "./components/ShoppingPlans";
import SkillQueryInput from "./components/SkillQueryInput";
import MyOrders from "./components/MyOrders";
import FavoritesList from "./components/FavoritesList";
import { CatalogMediaProvider } from "./components/CatalogMedia";
import BuyerWorkspace from "./components/BuyerWorkspace";
import { skillQueryDraft, submitSkillQuery } from "./lib/skills";

type View = "shopping" | "favorites" | "skills" | "preferences" | "orders" | "automations";
const STARTERS = [
  "我想先逛逛旅行装备。请用“旅行装备”这个关键词一次搜索，返回 12 件候选，暂不扩展关键词或按子品类拆分搜索。收货国家中国，单件商品价格不超过 500 元人民币，运费和税费另列。先展示这批商品卡，再简要说明用途；符合条件不足 12 件时请如实说明。",
  "想买日常通勤耳机，帮我理一理选购思路。",
  "预算100元以内，找适合短途出行的背包。",
];
const VIEW_KEY = "shoppilot.workspace.view";
function readView(): View {
  try {
    const saved = sessionStorage.getItem(VIEW_KEY);
    if (saved && ["shopping", "favorites", "skills", "preferences", "orders", "automations"].includes(saved))
      return saved as View;
  } catch { /* 存储受限时使用首页。 */ }
  return "shopping";
}
export default function App() {
  const agent = useCommerceAgent();
  const [shoppingForm,setShoppingForm]=useState<unknown>(null);
  const formGeneration=useRef(0);
  useEffect(()=>{
    setShoppingForm(null);
  },[agent.sessionId]);
  useEffect(()=>{
    const generation=++formGeneration.current;
    // AG-UI 快照可能早于表单提交；以数据库最新版为准，不能复活已提交的旧表单。
    void agent.workspaceRequest("/shopping-forms?session_id="+encodeURIComponent(agent.sessionId))
      .then(data=>{if(generation===formGeneration.current)setShoppingForm(data.form??null);}).catch(()=>{});
    return()=>{if(generation===formGeneration.current)++formGeneration.current;};
  },[agent.sessionId,agent.workspaceRequest,agent.shoppingForm]);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const navigationRef = useRef<HTMLElement>(null);
  const [historySearchOpen, setHistorySearchOpen] = useState(false);
  const [atmosphere, setAtmosphere] = useState(() => {
    try { return localStorage.getItem("shoppilot.atmosphere") !== "off"; } catch { return true; }
  });
  useEffect(() => {
    try { localStorage.setItem("shoppilot.atmosphere", atmosphere ? "on" : "off"); } catch { /* 不依赖本地存储显示背景。 */ }
  }, [atmosphere]);
  useEffect(() => {
    if (!mobileNavOpen) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = () => Array.from(navigationRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)") ?? []);
    focusable()[0]?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNavOpen(false);
      if (event.key !== "Tab") return;
      const items = focusable();
      const first = items[0], last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    const onResize = () => { if (window.innerWidth > 760) setMobileNavOpen(false); };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onResize);
      previousFocus?.focus();
    };
  }, [mobileNavOpen]);
  const [selectedSkill, setSelectedSkill] = useState<PublishedSkill | null>(null);
  const [planPickerOpen, setPlanPickerOpen] = useState(false);
  const [slashMenuOpen, setSlashMenuOpen] = useState(false);
  const [view, setView] = useState<View>(readView),
    [input, setInput] = useState("");
  // 会话入口收敛到最近选购，打开已有会话时让对应条目保持可见。
  useEffect(() => {
    if (view === "shopping") navigationRef.current?.querySelector('.history-short.current')?.scrollIntoView?.({ block: "nearest" });
  }, [view, agent.sessionId, agent.history.length]);
  const [favorites, setFavorites] = useState<ProductCard[]>([]),
    [compared, setCompared] = useState<ProductCard[]>([]);
  const [detail, setDetail] = useState<ProductCard | null>(null),
    [showCompare, setShowCompare] = useState(false),
    [toast, setToast] = useState("");
  const [orderIntent, setOrderIntent] = useState<{
    product: ProductCard;
    skuId: string;
  } | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null),
    bottomRef = useRef<HTMLDivElement>(null),
    autoScroll = useRef(true),
    programmaticScroll = useRef(false);
  useEffect(() => {
    try { sessionStorage.setItem(VIEW_KEY, view); } catch {}
  }, [view]);
  const contextWorkspace = useContextWorkspace({
    sessionId:agent.sessionId,
    busy:agent.status==="running",
    pending:!!agent.toolApprovals?.length||agent.confirmations.some(c=>c.status==="pending"&&!c.expired),
    hasMessages:agent.messages.length>0,
    request:agent.workspaceRequest,
  });
  const agentRunning = agent.status === "running";
  const busy = agentRunning || contextWorkspace.running;
  const favoriteIds = useMemo(
    () => new Set(favorites.map((p) => p.product_id)),
    [favorites],
  );
  const comparedIds = useMemo(
    () => new Set(compared.map((p) => p.product_id)),
    [compared],
  );
  const lastUser = [...agent.messages]
    .reverse()
    .find((message) => message.role === "user");
  const activities = agent.activities ?? [];
  const loadToolProducts = useCallback((runId: string, toolCallId: string) =>
    agent.workspaceRequest(`/ag-ui/runs/${encodeURIComponent(runId)}/tools/${encodeURIComponent(toolCallId)}/products`),
  [agent.workspaceRequest]);
  const hasConversation = agent.messages.length > 0 || !!shoppingForm;
  const landedDestination = agent.products.find(
    (product) => product.landed_price?.ship_to,
  )?.landed_price?.ship_to;

  const favoriteBusy = useRef(false);
  useEffect(() => {
    let disposed=false;
    void agent.workspaceRequest("/favorites").then(data => {
      if (!disposed) setFavorites(readProducts(data.products));
    }).catch(() => { if (!disposed) setToast("收藏暂时读取失败，请稍后刷新；数据库中的收藏仍保留。"); });
    return () => {disposed=true;};
  }, [agent.workspaceRequest]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    setCompared([]);
    setSelectedSkill(null);
    setPlanPickerOpen(false);
    setInput("");
    setDetail(null);
    setOrderIntent(null);
    setShowCompare(false);
    setMobileNavOpen(false);
  }, [agent.sessionId]);
  useEffect(() => {
    if (selectedSkill && agent.skillsStatus === "ready" && !agent.skills.some((skill) => skill.id === selectedSkill.id
      && skill.version === selectedSkill.version && skill.content_hash === selectedSkill.content_hash)) {
      setSelectedSkill(null);
      setToast("这份方案已更新或暂不可用，已恢复为直接提问。 ");
    }
  }, [agent.skills, agent.skillsStatus, selectedSkill]);
  useEffect(() => {
    let previousY = window.scrollY;
    const onScroll = () => {
      const currentY = window.scrollY;
      const distance =
        document.documentElement.scrollHeight - currentY - window.innerHeight;
      if (!programmaticScroll.current) {
        // 用户向上查看历史即暂停跟随，不要求先滚出某个距离。
        if (currentY < previousY - 1 || distance > 240)
          autoScroll.current = false;
        else if (currentY > previousY && distance < 28)
          autoScroll.current = true;
      }
      previousY = currentY;
    };
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) autoScroll.current = false;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("wheel", onWheel, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("wheel", onWheel);
    };
  }, []);
  useEffect(() => {
    if (
      view !== "shopping" ||
      detail ||
      showCompare ||
      !autoScroll.current ||
      !agent.messages.length
    )
      return;
    let releaseFrame = 0;
    const frame = requestAnimationFrame(() => {
      if (!autoScroll.current) return;
      programmaticScroll.current = true;
      bottomRef.current?.scrollIntoView({ block: "end", behavior: "auto" });
      releaseFrame = requestAnimationFrame(() => {
        programmaticScroll.current = false;
      });
    });
    return () => {
      cancelAnimationFrame(frame);
      cancelAnimationFrame(releaseFrame);
      programmaticScroll.current = false;
    };
  }, [agent.messages, agent.products, view, detail, showCompare]);

  const submit = useCallback(
    (query: string, selection: PublishedSkill | null = null) => {
      if (!query.trim() || busy) return;
      submitSkillQuery(query, selection, (request, selected) => {
        setView("shopping");
        setMobileNavOpen(false);
        setInput("");
        setSelectedSkill(null);
        setPlanPickerOpen(false);
        setCompared([]);
        setShowCompare(false);
        autoScroll.current = true;
        void agent.submit(request, selected);
      }, (request) => {
        setView("shopping");
        setInput(request);
        setSelectedSkill(null);
        setPlanPickerOpen(false);
        setToast("这份方案已过期，已保留你的需求。请确认后再发送。 ");
        void agent.refreshSkills();
        inputRef.current?.focus();
      });
    },
    [agent.submit, agent.refreshSkills, busy],
  );
  const chooseSkill = (skill: PublishedSkill, draft = input) => {
    if (busy) return;
    if (skill.expires_at && Date.parse(skill.expires_at) <= Date.now()) {
      setToast("这份方案已过期，正在刷新可用方案。 ");
      void agent.refreshSkills();
      return;
    }
    const nextInput = skillQueryDraft(draft);
    if (nextInput.length > 4000) {
      setToast("需求较长，请稍作精简后再选择方案。 ");
      return;
    }
    setInput(nextInput);
    setSelectedSkill(skill);
    setPlanPickerOpen(false);
    setView("shopping");
    inputRef.current?.focus();
  };
  const cancelSkill = () => {
    setSelectedSkill(null);
    inputRef.current?.focus();
  };
  const planProps = { skills: agent.skills, status: agent.skillsStatus, error: agent.skillsError,
    selected: selectedSkill, disabled: busy, onSelect: chooseSkill, onRefresh: agent.refreshSkills };

  const newShopping = () => {
    agent.reset();
    setView("shopping");
    setMobileNavOpen(false);
    setInput("");
    autoScroll.current = true;
    window.scrollTo({ top: 0 });
    inputRef.current?.focus();
  };
  const openSession = (id: string) => {
    if (busy) return;
    setHistorySearchOpen(false);
    agent.setSession(id);
    setView("shopping");
    setMobileNavOpen(false);
    window.scrollTo({ top: 0 });
  };
  const switchView = (next: View) => {
    setView(next);
    setMobileNavOpen(false);
    window.scrollTo({ top: 0 });
  };
  const toggleFavorite = useCallback(
    (product: ProductCard) => {
      if (favoriteBusy.current) return;
      favoriteBusy.current=true;
      const removing=favoriteIds.has(product.product_id);
      void agent.workspaceRequest("/favorites/"+encodeURIComponent(product.product_id),removing ? "DELETE" : "PUT",removing ? undefined : {product})
        .then(data => {setFavorites(readProducts(data.products));setToast(removing ? "已从心选收藏移除。" : "已保存到心选收藏。");})
        .catch(() => setToast("收藏未能保存，请重试。"))
        .finally(() => {favoriteBusy.current=false;});
    },
    [favoriteIds,agent.workspaceRequest],
  );
  const toggleCompare = useCallback(
    (product: ProductCard) => {
      if (comparedIds.has(product.product_id))
        setCompared((current) =>
          current.filter((item) => item.product_id !== product.product_id),
        );
      else if (compared.length >= 3)
        setToast("一次可以比较 3 件商品，先移出一件再试试。 ");
      else setCompared((current) => [...current, product]);
    },
    [comparedIds, compared.length],
  );
  const upsertCompare = useCallback(
    (product: ProductCard) => {
      if (!comparedIds.has(product.product_id) && compared.length >= 3) {
        setToast("一次可以比较 3 件商品，先移出一件再试试。 ");
        return;
      }
      // 详情选择规格是新增或更新；只有商品卡复选框负责移除比较。
      setCompared((current) =>
        current.some((item) => item.product_id === product.product_id)
          ? current.map((item) =>
              item.product_id === product.product_id ? product : item,
            )
          : current.length < 3
            ? [...current, product]
            : current,
      );
      setToast("已按当前所选规格更新比较信息。 ");
    },
    [comparedIds, compared.length],
  );
  const renderCards = (products: ProductCard[]) => (
    <ProductCards
      products={products}
      favoriteIds={favoriteIds}
      comparedIds={comparedIds}
      onFavorite={toggleFavorite}
      onCompare={toggleCompare}
      onDetail={setDetail}
    />
  );
  const navItems: { id: View; label: string; icon: string }[] = [
    { id: "orders", label: "订单与收藏", icon: "order" },
    { id: "skills", label: "我的 Skill", icon: "leaf" },
    { id: "preferences", label: "长期偏好", icon: "spark" },
    { id: "automations", label: "自动化", icon: "clock" },
  ];

  return (
    <CatalogMediaProvider request={agent.workspaceRequest}><div className={`shoppilot-workbench ${atmosphere ? "has-atmosphere" : "quiet-background"}`}>
      <div className="workbench-atmosphere" aria-hidden="true" />
      {mobileNavOpen && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
      <aside ref={navigationRef} id="workspace-navigation" className={`sidebar ${mobileNavOpen ? "is-open" : ""}`} aria-label="主导航">
        <button className="sidebar-close icon-button" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)}><Icon name="close" /></button>
        <button
          className="brand"
          onClick={() => switchView("shopping")}
          aria-label="ShopPilot 环球好物首页"
        >
          <Icon name="globe" className="brand-mark" />
          <span>
            <span className="brand-name">ShopPilot</span>
            <span className="brand-subtitle">YOUR SHOPPING COMPANION</span>
          </span>
        </button>
        <button className="new-chat" onClick={newShopping} disabled={busy}>
          <Icon name="plus" />
          新的选购
        </button>
        <nav className="nav">
          {navItems.map((item) => (
            <button
              key={item.id}
              className={`nav-item ${(view === item.id || (item.id === "orders" && view === "favorites")) ? "active" : ""}`}
              onClick={() => switchView(item.id)}
              aria-current={(view === item.id || (item.id === "orders" && view === "favorites")) ? "page" : undefined}
            >
              <Icon name={item.icon} />
              {item.label}
            </button>
          ))}
          <button className="nav-item sidebar-search-trigger" aria-haspopup="dialog" aria-expanded={historySearchOpen}
            onClick={() => { setMobileNavOpen(false); setHistorySearchOpen(true); }}>
            <Icon name="search" />搜索
          </button>
        </nav>
        <div className="nav-label"><span>最近选购</span><span>{agent.history.length.toString().padStart(2, "0")}</span></div>
        <div className="sidebar-history">
        {agent.history.map((item) => (
          <button
            className={`history-short ${view === "shopping" && item.id === agent.sessionId ? "current" : ""}`}
            aria-current={view === "shopping" && item.id === agent.sessionId ? "page" : undefined}
            key={item.id}
            onClick={() => openSession(item.id)}
            disabled={busy}
            title={item.title}
          >
            <Icon name="chat" /><span>{item.title}</span>
            {item.origin === "automation" && <span className="session-automation-mark" title="自动化会话" aria-label="自动化会话"><Icon name="clock" /></span>}
          </button>
        ))}
        {!agent.history.length && <p className="sidebar-empty">第一段选购，等你开启。</p>}
        </div>
        <div className="sidebar-bottom">
          <div className="sidebar-note"><span className="sidebar-note-icon"><Icon name="spark" /></span><div><strong>让选择，轻一点。</strong><p>找好物，也找到自己的偏好。</p></div></div>
          <div className="profile">
            <span className="avatar">泡</span>
            <span>
              <span className="profile-name">{import.meta.env.VITE_BUYER_ID || "pao-coder"}</span>
              <span className="profile-caption">每一次选择，都有新发现</span>
            </span>
            <Icon name="leaf" />
          </div>
        </div>
      </aside>
      <main className={view === "shopping" ? "" : "management-main"}>
        <div className="content">
          <header className="topbar">
            <div className="workspace-page-title">
              <button className="mobile-menu-toggle icon-button" aria-label="打开导航" aria-expanded={mobileNavOpen} aria-controls="workspace-navigation" onClick={() => setMobileNavOpen(!mobileNavOpen)}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
              </button>
              <div><div className="breadcrumb"><span>ShopPilot</span><span>／</span><span>环球好物</span></div>
              <h1>{view === "shopping" ? agent.history.find(item => item.id === agent.sessionId)?.title ?? "新的选购" : navItems.find((item) => item.id === (view === "favorites" ? "orders" : view))?.label}</h1></div>
            </div>
            <div className="topbar-actions">
              <div className="location"><Icon name="pin" />{landedDestination ? `配送至 ${landedDestination}` : "探索世界好物"}</div>
              <button className="atmosphere-toggle icon-button" title={atmosphere ? "关闭氛围背景" : "开启氛围背景"} aria-label={atmosphere ? "关闭氛围背景" : "开启氛围背景"} aria-pressed={atmosphere} onClick={() => setAtmosphere(!atmosphere)}><Icon name="spark" /></button>
            </div>
          </header>
          {view === "automations" && <Automations sessionId={agent.sessionId} request={agent.workspaceRequest} onHistoryChanged={agent.refreshHistory} onOpenSession={id => { void agent.refreshHistory().then(() => openSession(id)); }} busy={busy} />}
          {(view === "orders" || view === "favorites") && <section className="purchases-workspace" aria-label="订单与收藏">
            <div className="purchase-tabs" role="tablist" aria-label="订单与收藏切换">
              <button id="orders-tab" role="tab" aria-selected={view === "orders"} aria-controls="purchase-panel" onClick={() => switchView("orders")}><Icon name="order" />我的订单</button>
              <button id="favorites-tab" role="tab" aria-selected={view === "favorites"} aria-controls="purchase-panel" onClick={() => switchView("favorites")}><Icon name="heart" />心选收藏<span>{favorites.length}</span></button>
            </div>
            <div id="purchase-panel" role="tabpanel" aria-labelledby={view === "orders" ? "orders-tab" : "favorites-tab"}>
              {view === "orders" ? <MyOrders request={agent.workspaceRequest} confirmations={agent.confirmations} busy={agent.confirmationBusy || busy} error={agent.confirmationError} onPrepare={agent.prepareCancel} onResolve={agent.resolveConfirmation} onRefresh={agent.refreshConfirmations} /> : <FavoritesList products={favorites} onDetail={setDetail} onRemove={toggleFavorite} onBrowse={() => switchView("shopping")} />}
            </div>
          </section>}
          {(view === "skills" || view === "preferences") && <BuyerWorkspace key={view} mode={view} busy={busy}
            request={agent.workspaceRequest} onSkillsChanged={agent.refreshSkills} />}
          {view === "shopping" && (
            <>
              <div id="chat-panel" aria-label="选购对话">
              {!hasConversation ? (
                <section className="welcome-panel">
                  <div className="welcome-symbol"><Icon name="globe" /><span><Icon name="spark" /></span></div>
                  <div className="eyebrow">A WORLD OF GOOD FINDS</div>
                  <h2>你的下一件好物，<br /><em>从这里开始。</em></h2>
                  <p>告诉我你的期待，剩下的，一起慢慢选。</p>
                  <div className="welcome-ideas">
                    <button disabled={busy} onClick={() => submit(STARTERS[0])}><span className="starter-icon"><Icon name="bag" /></span><span><strong>旅行好物，一起挑</strong><small>看看 12 款装备 · 单件 500 元内</small></span><Icon name="arrow" /></button>
                    <button disabled={busy} onClick={() => submit(STARTERS[1])}><span className="starter-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 14v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="12" width="4" height="8" rx="2"/><rect x="17" y="12" width="4" height="8" rx="2"/></svg></span><span><strong>日常好声音</strong><small>挑一副适合通勤的耳机</small></span><Icon name="arrow" /></button>
                    <button disabled={busy} onClick={() => submit(STARTERS[2])}><span className="starter-icon"><Icon name="spark" /></span><span><strong>小预算，也出彩</strong><small>发现 100 元以内的好物</small></span><Icon name="arrow" /></button>
                  </div>
                  <span className="welcome-caption">认真挑选 · 清楚比较 · 从容决定</span>
                </section>
              ) : (
                <ConversationTurns messages={agent.messages} activities={activities} activeRunId={lastUser?.runId}
                  running={agentRunning} loadToolProducts={loadToolProducts} />
              )}
              {!hasConversation && <details className="welcome-plan-disclosure"><summary><Icon name="leaf" />或者，从一份选购方案开始<span>查看方案 <Icon name="arrow" /></span></summary><ShoppingPlans {...planProps} /></details>}
              <SkillRunStatus usages={agent.skillUsages} running={agentRunning} />
              {agentRunning && (
                <div className="progress-line running" role="status">
                  <span className="live-dot" />
                  <span>{agent.step || "正在为你整理合适的选择…"}</span>
                </div>
              )}
              {agent.status === "stopped" && (
                <div className="progress-line stopped" role="status">
                  <Icon name="stop" />
                  <span>
                    {agent.recoverableRunId ? "停止请求已发送，正在等待服务端确认。已有内容已保留。" : "已停止本次回复。已有内容为你保留，可以继续补充需求。"}
                  </span>
                </div>
              )}
              {agent.error && (
                <div className="error-panel" role="alert">
                  <Icon name="info" />
                  <div>
                    <strong>暂时没能完成这次选购</strong>
                    <p>{agent.error}</p>
                  </div>
                  {agent.recoverableRunId ? (
                    <>
                      <button disabled={busy} onClick={() => void agent.resume()}>恢复本轮</button>
                      <button disabled={busy} onClick={agent.stop}>停止本轮</button>
                    </>
                  ) : lastUser && (
                    <button
                      disabled={busy}
                      onClick={() => submit(lastUser.content)}
                    >
                      再试一次
                    </button>
                  )}
                </div>
              )}
              {!!shoppingForm&&<ShoppingForm key={agent.sessionId} form={shoppingForm}
                busy={busy||!!agent.toolApprovals?.length||agent.confirmations.some(c=>c.status==="pending"&&!c.expired)}
                request={agent.workspaceRequest} onApplied={agent.submitForm}/>}
              <ToolApprovalCards items={agent.toolApprovals ?? []} busy={busy} onResolve={agent.resolveToolApproval} />
              {(agent.confirmations.length > 0 || agent.confirmationError) && (
                <ConfirmationCards
                  confirmations={agent.confirmations}
                  busy={agent.confirmationBusy || busy}
                  error={agent.confirmationError}
                  onResolve={agent.resolveConfirmation}
                  onCancelOrder={agent.prepareCancel}
                  onRefresh={agent.refreshConfirmations}
                />
              )}
              {agent.products.length > 0 && (
                <section className="search-results" aria-label="商品搜索结果">
                  <div className="results-heading">
                    <div className="results-label">
                      <strong>这次找到的好物</strong> · {agent.products.length}{" "}
                      件
                    </div>
                    <button
                      className="results-action"
                      onClick={() =>
                        setToast(
                          "勾选商品卡下方的“加入比较”，可并排比较 2 至 3 件商品。 ",
                        )
                      }
                    >
                      <Icon name="compare" />
                      勾选商品，轻松对比
                    </button>
                  </div>
                  {renderCards(agent.products)}
                  <div className="results-footnote">
                    <Icon name="info" />
                    <span>
                      结果来自商品目录。示意图与样例评分均已标注，价格以具体规格及配送条件为准。
                    </span>
                  </div>
                </section>
              )}
              <ProductHistory batches={agent.productHistory}/>
              {!busy &&
                agent.searchCompleted &&
                !agent.products.length &&
                !agent.error && (
                  <section className="empty-state search-empty">
                    <Icon name="bag" />
                    <h2>
                      {agent.status === "stopped"
                        ? "先停在这里，也没关系"
                        : "这次，还没有合适的结果"}
                    </h2>
                    <p>
                      {agent.status === "stopped"
                        ? "本轮已停止，尚未收到商品结果。调整需求后可以重新开始。"
                        : "这次检索没有返回符合条件的商品。可以调整预算、品类或配送地区，再一起看看。"}
                    </p>
                    <button
                      onClick={() => {
                        setInput(lastUser?.content || "");
                        inputRef.current?.focus();
                      }}
                    >
                      调整一下需求
                      <Icon name="arrow" />
                    </button>
                  </section>
                )}
              {agentRunning && !agent.products.length && (
                <div
                  className="product-grid loading-results"
                  aria-label="正在查找商品"
                >
                  <div className="loading-card" />
                  <div className="loading-card" />
                  <div className="loading-card" />
                </div>
              )}
              {agent.messages.length > 0 && (
                <div className="suggestions">
                  <button
                    className="suggestion"
                    disabled={busy}
                    onClick={() => {
                      setInput("预算想再少一点，");
                      inputRef.current?.focus();
                    }}
                  >
                    调整预算
                    <Icon name="arrow" />
                  </button>
                  {agent.products.length > 1 && (
                    <button
                      className="suggestion"
                      onClick={() => {
                        setCompared(agent.products.slice(0, 3));
                        setShowCompare(true);
                      }}
                    >
                      一起比较看看
                      <Icon name="compare" />
                    </button>
                  )}
                  <button
                    className="suggestion"
                    onClick={() => switchView("favorites")}
                  >
                    看看我的收藏
                    <Icon name="heart" />
                  </button>
                </div>
              )}
              {(agent.events.length > 0 || agent.skillUsages.length > 0) && <EventTimeline events={agent.events} skillUsages={agent.skillUsages} running={agentRunning} />}
              <div ref={bottomRef} className="scroll-anchor" />
              </div>

            </>
          )}

        </div>
      </main>
      {view === "shopping" && <div className={`composer-dock ${!hasConversation && view === "shopping" ? "composer-dock--welcome" : ""}`}>
        <div className="composer-wrap">
          {compared.length > 0 && (
            <div className="compare-bar">
              <div className="compare-mini">
                {compared.map((p) => (
                  <ProductImage product={p} key={p.product_id} />
                ))}
              </div>
              <span>
                已选 {compared.length} 件
                {compared.length === 1 ? "，再选一件比比看" : ""}
              </span>
              <button
                className="compare-go"
                onClick={() => setShowCompare(true)}
                disabled={compared.length < 2}
              >
                开始比较
                <Icon name="arrow" />
              </button>
              <button className="compare-clear" onClick={() => setCompared([])}>
                清空
              </button>
            </div>
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              if (agentRunning) agent.stop();
              else if (contextWorkspace.running) return;
              else if (!slashMenuOpen) submit(input, selectedSkill);
            }}
          >
            <div className="composer-plan-controls">
              <button type="button" className="plan-picker-toggle" aria-expanded={planPickerOpen} aria-controls="composer-plans"
                disabled={busy} onClick={() => setPlanPickerOpen(!planPickerOpen)}><Icon name="leaf" />选购方案</button>
              {selectedSkill ? <div className="selected-plan"><span>已选择 · {selectedSkill.title}</span>
                <button type="button" aria-label="取消已选方案" disabled={busy} onClick={cancelSkill}><Icon name="close" /></button></div>
                : <span className="plan-automatic">直接提问 · 自动匹配</span>}
            </div>
            {planPickerOpen && <div className="plan-picker-panel" id="composer-plans" onKeyDown={(event) => { if (event.key === "Escape") { setPlanPickerOpen(false); inputRef.current?.focus(); } }}>
              <ShoppingPlans {...planProps} compact />
            </div>}
            <label htmlFor="query" className="sr-only">
              告诉 ShopPilot 你想寻找的好物
            </label>
            <button type="button" className="composer-plan-toggle"
              disabled={busy||!!agent.toolApprovals?.length||!!agent.recoverableRunId||agent.confirmations.some(c=>c.status==="pending"&&!c.expired)}
              onClick={()=>submit([
                input.trim(),
                compared.length ? "正在比较的商品："+compared.map(p=>`${p.title}（${p.product_id}）`).join("、") : "",
                "请根据本次选购目标，调用选购需求澄清工具，让我填写还缺少的必要条件；提交后再继续核验商品。",
              ].filter(Boolean).join("\n"),selectedSkill)}>澄清选购需求</button>
            <SkillQueryInput
              key={agent.sessionId}
              ref={inputRef}
              value={input}
              skills={agent.skills}
              status={agent.skillsStatus}
              error={agent.skillsError}
              busy={busy}
              onChange={setInput}
              onSelect={chooseSkill}
              onSubmit={() => submit(input, selectedSkill)}
              onRefresh={agent.refreshSkills}
              onMenuOpenChange={setSlashMenuOpen}
            />
            <div className="composer-bottom">
              <span className="composer-hint">
                <Icon name="spark" />
                用途、预算和小偏好，都可以告诉我
              </span>
              <div className="composer-actions">
              <ModelPicker key={"model:"+agent.sessionId} sessionId={agent.sessionId} request={agent.workspaceRequest} busy={agentRunning} pending={contextWorkspace.pending} onChanged={contextWorkspace.reload}/>
              <ContextWorkspace key={agent.sessionId} state={contextWorkspace}/>
              <button
                type="submit"
                className={`send-button ${agentRunning ? "stop" : ""}`}
                disabled={!agentRunning && (contextWorkspace.running || !input.trim() || slashMenuOpen)}
                aria-label={agentRunning ? "停止生成" : "发送选购需求"}
              >
                <Icon name={agentRunning ? "stop" : "up"} />
                {agentRunning && <span>停止</span>}
              </button>
              </div>
            </div>
          </form>
          <footer className="preview-footer">
            <span>ShopPilot 环球好物</span>
            <span>·</span>
            <span>好物跨越距离，选择回到你心里</span>
          </footer>
        </div>
      </div>}
      {historySearchOpen && <HistorySearch history={agent.history} currentSessionId={agent.sessionId}
        busy={busy} error={agent.historyError} onOpen={openSession} onClose={() => setHistorySearchOpen(false)} />}
      {detail && (
        <ProductDetail
          key={detail.product_id}
          product={detail}
          busy={busy}
          onClose={() => setDetail(null)}
          onCompare={upsertCompare}
          onAsk={submit}
          onPrepare={(product, skuId) => {
            setDetail(null);
            setOrderIntent({ product, skuId });
          }}
        />
      )}
      {orderIntent && (
        <OrderIntentForm
          product={orderIntent.product}
          skuId={orderIntent.skuId}
          busy={agent.confirmationBusy}
          error={agent.confirmationError}
          onClose={() => setOrderIntent(null)}
          onPrepare={async (input) => {
            const success = await agent.prepareOrder(input);
            if (success) {
              setView("shopping");
              setToast("确认单已准备好，请核对后决定。");
            }
            return success;
          }}
        />
      )}
      {showCompare && compared.length >= 2 && (
        <ProductComparison
          products={compared}
          onClose={() => setShowCompare(false)}
        />
      )}
      <div className={`toast ${toast ? "visible" : ""}`} role="status">
        {toast}
      </div>
    </div></CatalogMediaProvider>
  );
}
