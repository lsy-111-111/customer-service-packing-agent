import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Check,
  LogOut,
  MessageCircle,
  Search,
  UserRound,
  X,
} from "lucide-react";
import {
  api,
  getSession,
  patch,
  post,
  requestID,
  setSession as persistSession,
  time,
} from "./api";
import type {
  Case,
  Conversation,
  ConversationData,
  Order,
  Product,
  Session,
  ServiceRequest,
  WorkQueue,
  WorkItem,
  WorkQueueFilters as WorkFilters,
  SupportRecordLink,
  User,
  ResourceState,
} from "./types";
import { Catalog, Comparison, OrdersDrawer, ProductDetail } from "./Products";
import { CaseModal, CasesDrawer, StaffInspector } from "./Cases";
import { Chat } from "./Chat";
import { AdminDrawer } from "./Admin";
import { Empty, ErrorNote, ResourceNotice, Spinner } from "./ui";
import { HandoffSummary, queueLabels, StaffServiceInspector } from "./Support";
import { PreferencePanel } from "./Preferences";
import { ServiceMetrics } from "./ServiceMetrics";
import { CustomerPicker } from "./CustomerPicker";
import { productPath } from "./catalogLinks";
import WorkOperations, { WorkItemOperationsSummary, WorkQueueFilters } from "./WorkOperations";
import "./Packing.css";
const Packing = lazy(() => import("./Packing"));
const Logistics = lazy(() => import("./Logistics"));
const OperationsDashboard = lazy(() => import("./OperationsDashboard"));
type EmployeePage = "packing" | "logistics" | "operations-dashboard";
const employeePageLabels: Record<EmployeePage, string> = { packing: "装箱与箱唛", logistics: "物流跟进", "operations-dashboard": "老板看板" };
type ResourceKey = "products" | "orders" | "cases" | "conversations" | "requests" | "queue";
const resourceLabels: Record<ResourceKey, string> = {
  products: "商品", orders: "订单", cases: "售后申请", conversations: "会话", requests: "人工受理", queue: "未完成事项",
};
const resourceKeys = Object.keys(resourceLabels) as ResourceKey[];
const emptyResourceStates = () => Object.fromEntries(resourceKeys.map(key => [key, { loading: false, loaded: false, error: "" }])) as Record<ResourceKey, ResourceState>;
export default function App() {
  const route = window.location.pathname.replace(/\/$/, "").slice(1);
  if (route === "packing" || route === "logistics" || route === "operations-dashboard") return <EmployeePageEntry page={route} />;
  return <StoreApp />;
}
function StoreApp() {
  const [session, setSession] = useState<Session | null>(getSession);
  const [personas, setPersonas] = useState<User[]>([]);
  const [personasLoading, setPersonasLoading] = useState(false);
  const personaSequence = useRef(0);
  const [products, setProducts] = useState<Product[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [cases, setCases] = useState<Case[]>([]);
  const [serviceRequests, setServiceRequests] = useState<ServiceRequest[]>([]);
  const [workQueue, setWorkQueue] = useState<WorkQueue>({ items: [], total: 0, filtered_total: 0, stage_counts: {} });
  const [serviceID, setServiceID] = useState("");
  const [inspectorKind, setInspectorKind] = useState<"case" | "service">("case");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeID, setActiveID] = useState("");
  const [data, setData] = useState<ConversationData | null>(null);
  const [product, setProduct] = useState<Product | null>(null);
  const [showOrders, setShowOrders] = useState(false);
  const [showCases, setShowCases] = useState(false);
  const [showAdmin, setShowAdmin] = useState(false);
  const [showPacking, setShowPacking] = useState(false);
  const [showLogistics, setShowLogistics] = useState(false);
  const [showDashboard, setShowDashboard] = useState(false);
  const [logisticsExceptionID, setLogisticsExceptionID] = useState("");
  const [caseEdit, setCaseEdit] = useState<{
    item?: Case;
    order: Order;
  } | null>(null);
  const [caseID, setCaseID] = useState("");
  const [compareIDs, setCompareIDs] = useState<string[]>([]);
  const [showCompare, setShowCompare] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [unreadReplies, setUnreadReplies] = useState(0);
  const [switchingMode, setSwitchingMode] = useState(false);
  const seenReplyIDs = useRef<Set<string> | null>(null);
  const resumeRequests = useRef<Record<string, string>>({});
  const chatLauncher = useRef<HTMLButtonElement>(null);
  const [staffView, setStaffView] = useState<"list" | "chat" | "case">("list");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [toast, setToast] = useState("");
  const [staffFilter, setStaffFilter] = useState("all");
  const [staffSearch, setStaffSearch] = useState("");
  const [workFilters, setWorkFilters] = useState<WorkFilters>({ assignee: "all", deadlineScope: "staff", deadline: "all" });
  const [selectedWork, setSelectedWork] = useState<WorkItem | null>(null);
  const [showWorkOperations, setShowWorkOperations] = useState(false);
  const [directoryRevision, setDirectoryRevision] = useState(0);
  const queueQuery = new URLSearchParams({ stage: staffFilter === "history" ? "all" : staffFilter, assignee: workFilters.assignee, deadline_scope: workFilters.deadlineScope, deadline: workFilters.deadline, q: staffFilter === "history" ? "" : staffSearch }).toString();
  const queueQueryRef = useRef(queueQuery);
  queueQueryRef.current = queueQuery;
  const [resources, setResources] = useState(emptyResourceStates);
  const resourceRequests = useRef<Partial<Record<ResourceKey, { sequence: number; controller: AbortController }>>>({});
  const sessionGeneration = useRef(0);
  const loginSequence = useRef(0);
  const conversationSequence = useRef(0);
  const token = session?.token;
  const staff = session?.user.role === "staff";
  function openProduct(p: Product) {
    const selected = p.selected_sku_id || p.skus[0]?.id;
    setProduct({ ...p, selected_sku_id: selected });
    const path = productPath(p, selected) || productPath(p);
    if (path) history.pushState(null, "", path);
  }
  function closeProduct() { setProduct(null); history.pushState(null, "", "/"); }
  useEffect(() => {
    if (!token || !resources.products.loaded) return;
    function readLocation(force = false) {
      const match = window.location.pathname.match(/^\/products\/([^/]+)$/);
      if (!match) { if (force) setProduct(null); return; }
      let pid = "";
      try { pid = decodeURIComponent(match[1]); } catch { setError("商品链接无法识别，请从店铺重新选择商品"); return; }
      const found = products.find(p => p.id === pid);
      if (!found) { setProduct(null); setError("这款商品暂不可用，请从店铺重新选择"); return; }
      const skuID = new URLSearchParams(window.location.search).get("sku");
      if (skuID && !found.skus.some(s => s.id === skuID)) { setProduct(null); setError("商品链接里的规格已不可用，请重新选择商品规格"); return; }
      setProduct(current => {
        if (!force && current?.id === found.id) {
          const freshSkus = found.skus.filter(s => current.skus.some(v => v.id === s.id));
          const kept = freshSkus.length ? freshSkus : found.skus;
          return { ...found, skus: kept, selected_sku_id: kept.some(s => s.id === current.selected_sku_id) ? current.selected_sku_id : kept[0]?.id };
        }
        return { ...found, selected_sku_id: skuID || found.skus[0]?.id };
      });
    }
    readLocation();
    const changed = () => readLocation(true);
    window.addEventListener("popstate", changed);
    window.addEventListener("shop:product-navigation", changed);
    return () => { window.removeEventListener("popstate", changed); window.removeEventListener("shop:product-navigation", changed); };
  }, [token, products, resources.products.loaded]);
  const messageRequest = useRef<Record<string, string>>({});
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeRef = useRef(activeID);
  activeRef.current = activeID;
  const notify = useCallback((text: string) => {
    setToast(text);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 4200);
  }, []);
  const loadPersonas = useCallback(() => {
    const sequence = ++personaSequence.current;
    setPersonasLoading(true);
    api<User[]>("/personas")
      .then(value => {
        if (personaSequence.current === sequence) {
          setPersonas(value);
          setError("");
        }
      })
      .catch(e => { if (personaSequence.current === sequence) setError(e.message); })
      .finally(() => { if (personaSequence.current === sequence) setPersonasLoading(false); });
  }, []);
  useEffect(() => {
    if (token) return;
    loadPersonas();
    const refreshVisible = () => { if (document.visibilityState === "visible") loadPersonas(); };
    window.addEventListener("focus", refreshVisible);
    document.addEventListener("visibilitychange", refreshVisible);
    return () => {
      personaSequence.current += 1;
      window.removeEventListener("focus", refreshVisible);
      document.removeEventListener("visibilitychange", refreshVisible);
    };
  }, [token, loadPersonas]);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);
  const loadResource = useCallback(async (key: ResourceKey, background = false) => {
    // A callback belonging to a previous identity must not issue a new request.
    if (!token || getSession()?.token !== token || (key === "queue" && !staff)) return;
    const previous = resourceRequests.current[key];
    // Polling does not replace an explicit request that is still in flight.
    if (background && previous && !previous.controller.signal.aborted) return;
    previous?.controller.abort();
    const controller = new AbortController();
    const sequence = (previous?.sequence || 0) + 1;
    const generation = sessionGeneration.current;
    resourceRequests.current[key] = { sequence, controller };
    const current = () => !controller.signal.aborted && sessionGeneration.current === generation &&
      getSession()?.token === token && resourceRequests.current[key]?.sequence === sequence;
    setResources(states => ({ ...states, [key]: { ...states[key], loading: true } }));
    try {
      const options = { signal: controller.signal };
      switch (key) {
        case "products": {
          const value = await api<Product[]>("/products", options);
          if (current()) setProducts(sortProducts(value));
          break;
        }
        case "orders": {
          const value = await api<Order[]>("/orders", options);
          if (current()) setOrders(value);
          break;
        }
        case "cases": {
          const value = await api<Case[]>("/cases", options);
          if (current()) setCases(value);
          break;
        }
        case "requests": {
          const value = await api<ServiceRequest[]>("/service-requests", options);
          if (current()) setServiceRequests(value);
          break;
        }
        case "queue": {
          const value = await api<WorkQueue>(`/staff/work-items?${queueQueryRef.current}`, options);
          if (current()) setWorkQueue(value);
          break;
        }
        case "conversations": {
          const list = !staff && !activeRef.current
            ? [await api<Conversation>("/conversations", { ...options, method: "POST", body: "{}" })]
            : await api<Conversation[]>("/conversations", options);
          if (current()) {
            setConversations(list);
            if (!activeRef.current && list.length) {
              activeRef.current = list[0].id;
              setActiveID(list[0].id);
            }
          }
          break;
        }
      }
      if (current()) setResources(states => ({ ...states, [key]: { loading: false, loaded: true, error: "" } }));
    } catch (failure) {
      if (current()) setResources(states => ({ ...states, [key]: { ...states[key], loading: false, error: (failure as Error).message } }));
    } finally {
      if (current()) controller.abort();
    }
  }, [token, staff]);
  useEffect(() => {
    if (!token) return;
    for (const key of resourceKeys) void loadResource(key);
    return () => {
      for (const request of Object.values(resourceRequests.current)) request?.controller.abort();
    };
  }, [token, loadResource]);
  useEffect(() => {
    if (staff && token) void loadResource("queue");
  }, [queueQuery, staff, token, loadResource]);
  const refreshConversation = useCallback(async () => {
    const id = activeRef.current;
    if (!id || !token || getSession()?.token !== token) return;
    const generation = sessionGeneration.current;
    const sequence = ++conversationSequence.current;
    const d = await api<ConversationData>(`/conversations/${id}`);
    if (activeRef.current === id && getSession()?.token === token && sessionGeneration.current === generation && conversationSequence.current === sequence)
      setData(d);
  }, [token]);
  useEffect(() => {
    const done = () => {
      void refreshConversation().catch(() => {});
      void loadResource("cases");
    };
    window.addEventListener("daily-turn-finished", done);
    return () => window.removeEventListener("daily-turn-finished", done);
  }, [refreshConversation, loadResource]);
  const refresh = useCallback(async () => {
    if (!token || getSession()?.token !== token) return;
    await Promise.all([...resourceKeys.map(key => loadResource(key)), refreshConversation()]);
  }, [token, loadResource, refreshConversation]);
  useEffect(() => {
    if (!token || !activeID) return;
    setData(null);
    let live = true;
    let inFlight = false;
    async function tick() {
      if (inFlight || !live) return;
      inFlight = true;
      try {
        await refreshConversation();
        if (live) setConnectionError("");
      } catch (e) {
        if (live) setConnectionError((e as Error).message);
      } finally {
        inFlight = false;
      }
    }
    void tick();
    const timer = setInterval(() => void tick(), 1800);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [activeID, token, refreshConversation]);
  useEffect(() => {
    if (!token) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      for (const key of resourceKeys) void loadResource(key, true);
    }, 4000);
    return () => clearInterval(timer);
  }, [token, loadResource]);
  async function login(persona: User) {
    const sequence = ++loginSequence.current;
    setLoading(true);
    setError("");
    try {
      const s = await post<Session>("/sessions", { persona_id: persona.id });
      if (loginSequence.current !== sequence) throw new Error("身份已切换，请重新选择");
      persistSession(s);
      setSession(s);
      setChatOpen(false);
    } finally {
      if (loginSequence.current === sequence) setLoading(false);
    }
  }
  function closeChat() {
    setChatOpen(false);
    requestAnimationFrame(() => chatLauncher.current?.focus());
  }
  useEffect(() => {
    if (!data) return;
    const replies = data.messages.filter((m) => m.role === "assistant" || m.role === "staff");
    const nextIDs = new Set(replies.map((m) => m.id));
    if (seenReplyIDs.current && !chatOpen && !staff) {
      const added = replies.filter((m) => !seenReplyIDs.current!.has(m.id)).length;
      if (added) setUnreadReplies((count) => count + added);
    }
    seenReplyIDs.current = nextIDs;
    if (chatOpen) setUnreadReplies(0);
  }, [data?.messages, chatOpen, staff]);
  async function resumeAI() {
    if (!data || switchingMode) return;
    const generation = sessionGeneration.current;
    const conversation = data.conversation;
    const key = `${conversation.id}-${conversation.generation}`;
    const request = resumeRequests.current[key] || (resumeRequests.current[key] = requestID());
    setSwitchingMode(true);
    try {
      await run(() => post(`/conversations/${conversation.id}/resume`, {
        request_id: request,
        expected_generation: conversation.generation,
      }), "已切回智能客服，未回复的问题会继续处理");
    } finally {
      if (getSession()?.token === token && sessionGeneration.current === generation) setSwitchingMode(false);
    }
  }
  function logout() {
    sessionGeneration.current += 1;
    loginSequence.current += 1;
    conversationSequence.current += 1;
    for (const request of Object.values(resourceRequests.current)) request?.controller.abort();
    resourceRequests.current = {};
    activeRef.current = "";
    persistSession(null);
    setSession(null);
    setActiveID("");
    setData(null);
    setProducts([]);
    setOrders([]);
    setCases([]);
    setServiceRequests([]);
    setWorkQueue({ items: [], total: 0, filtered_total: 0, stage_counts: {} });
    setServiceID("");
    setInspectorKind("case");
    setConversations([]);
    setCaseEdit(null);
    setShowOrders(false);
    setShowCases(false);
    setShowAdmin(false);
    setShowPacking(false);
    setShowLogistics(false);
    setShowDashboard(false);
    setLogisticsExceptionID("");
    setProduct(null);
    setCompareIDs([]);
    setShowCompare(false);
    setError("");
    setConnectionError("");
    setCaseID("");
    setStaffView("list");
    setChatOpen(false);
    setResources(emptyResourceStates());
    setStaffFilter("all");
    setStaffSearch("");
    setWorkFilters({ assignee: "all", deadlineScope: "staff", deadline: "all" });
    setSelectedWork(null);
    setShowWorkOperations(false);
    setDirectoryRevision(0);
    setLoading(false);
    setSending(false);
    setSwitchingMode(false);
    setToast("");
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setUnreadReplies(0);
    seenReplyIDs.current = null;
    resumeRequests.current = {};
    messageRequest.current = {};
  }
  async function run(action: () => Promise<unknown>, success?: string) {
    if (!token || getSession()?.token !== token) return;
    const generation = sessionGeneration.current;
    const current = () => getSession()?.token === token && sessionGeneration.current === generation;
    try {
      setError("");
      await action();
      if (!current()) return;
      await refresh();
      if (current() && success) notify(success);
    } catch (e) {
      if (current()) setError((e as Error).message);
    }
  }
  async function send(content: string, attachments: string[]) {
    if (!activeID || !token || getSession()?.token !== token) return;
    const generation = sessionGeneration.current;
    const current = () => getSession()?.token === token && sessionGeneration.current === generation;
    setSending(true);
    const key = `${activeID}|${content}|${attachments.join(",")}`;
    const id =
      messageRequest.current[key] ||
      (messageRequest.current[key] = requestID());
    try {
      await post(
        `/conversations/${activeID}/${staff ? "staff-messages" : "messages"}`,
        { content, attachment_ids: attachments, client_message_id: id },
      );
      if (!current()) return;
      delete messageRequest.current[key];
      await refreshConversation().catch(() => {
        if (current()) setError("消息已发送，正在重新同步会话，请勿重复提交。");
      });
      if (current()) await loadResource("cases");
    } finally {
      if (current()) setSending(false);
    }
  }
  async function consult(p: Product) {
    if (!activeID || getSession()?.token !== token) return;
    closeProduct();
    setChatOpen(true);
    await run(async () => {
      await patch(`/conversations/${activeID}/context`, { product_id: p.id });
      await send(
        `想了解一下${p.name}，请帮我看看它适合什么需求，有哪些规格。`,
        [],
      );
    });
  }
  function openCase(c: Case) {
    setShowCases(false);
    setCaseEdit({ item: c, order: c.order });
  }
  function selectWorkRecord(record: SupportRecordLink) {
    setSelectedWork(workQueue.items.find(i => i.type === record.type && i.id === record.id) || null);
    setShowWorkOperations(false);
  }
  function openSupportRecord(record: SupportRecordLink) {
    selectWorkRecord(record);
    if (record.type === "case") { setCaseID(record.id); setInspectorKind("case"); }
    else { setServiceID(record.id); setInspectorKind("service"); }
    setStaffView("case");
  }
  function toggleCompare(p: Product) {
    if (compareIDs.includes(p.id)) {
      setCompareIDs(compareIDs.filter((id) => id !== p.id));
      notify("已移出商品对比");
    } else if (compareIDs.length >= 3) {
      notify("最多同时比较 3 款商品");
    } else {
      setCompareIDs([...compareIDs, p.id]);
      notify("已加入商品对比");
    }
  }
  const currentData = data?.conversation.id === activeID ? data : null;
  const currentCases = cases.filter((c) => c.conversation_id === activeID);
  const activeCase =
    currentCases.find((c) => c.id === caseID) ||
    currentCases.find((c) => c.status === "submitted") ||
    currentCases[0];
  const currentRequests = serviceRequests.filter(r => r.conversation_id === activeID);
  const waitingCount = workQueue.total;
  useEffect(() => {
    if (staff && activeCase && activeCase.id !== caseID)
      setCaseID(activeCase.id);
  }, [staff, activeCase?.id, caseID]);
  if (!session)
    return (
      <div className="login-shell">
        <header className="site-header">
          <span className="wordmark">日常好物</span>
          <div className="header-nav"><a href="/packing" target="_blank" rel="noopener noreferrer">打开装箱与箱唛 ↗</a><span className="header-note">本地演示 · 商品与订单均为模拟数据</span></div>
        </header>
        <main className="login-main">
          <div className="login-intro">
            <span className="login-brand">
              好物相伴，
              <br />
              日常不将就。
            </span>
            <p>
              挑选、咨询、售后。
              <br />
              从一段轻松的对话开始。
            </p>
            <div className="login-images">
              <img src="/assets/e1.png" alt="日常耳机" />
              <img src="/assets/m1.png" alt="日常陶瓷杯" />
            </div>
          </div>
          <div className="login-panel">
            <div>
              <h1>欢迎来到日常好物</h1>
              <p>选一个身份，开始体验。</p>
            </div>
            <CustomerPicker personas={personas} loading={loading} listLoading={personasLoading}
              onLogin={login} onError={setError} onRefresh={loadPersonas}
              onCreated={customer => {
                personaSequence.current += 1;
                setPersonasLoading(false);
                setPersonas(current => current.some(p => p.id === customer.id) ? current : [...current, customer]);
              }} />
            <p className="login-helper">无需密码 · 可体验已有订单与零订单新顾客</p>
            <div className="staff-entry">
              <span>体验人工审核与接待</span>
              {personas
                .filter((p) => p.role === "staff")
                .map((p) => (
                  <button
                    className="text-link"
                    onClick={() => void login(p).catch(e => setError(e.message))}
                    disabled={loading}
                    key={p.id}
                  >
                    进入人工处理台
                    <ArrowRight size={17} />
                  </button>
                ))}
            </div>
            <ErrorNote error={error} />
            {!personas.length && (
              <button className="button" onClick={loadPersonas}>
                {loading ? <Spinner /> : null}重新连接
              </button>
            )}
            <p className="login-foot">
              所有操作仅用于本地体验，不产生真实交易。
            </p>
          </div>
        </main>
      </div>
    );
  // Keep a chat card's verified SKU subset when refreshing catalogue fields.
  const freshDetailProduct = product && (products.find(p => p.id === product.id) || product);
  const detailProduct = freshDetailProduct && product ? {
    ...freshDetailProduct,
    selected_sku_id: product.selected_sku_id,
    skus: freshDetailProduct.skus.filter(sku => product.skus.some(selected => selected.id === sku.id)),
  } : null;
  return (
    <div className={`app-shell ${staff ? "staff-shell" : ""}`}>
      <header className="site-header">
        <div className="brand-group">
          <button
            className="wordmark"
            onClick={() =>
              staff ? setStaffView("list") : setChatOpen(false)
            }
          >
            日常好物
          </button>
          {staff && <span className="workspace-label">人工处理台</span>}
        </div>
        {!staff && (
          <span className="header-note">本地演示 · 商品与订单均为模拟数据</span>
        )}
        <nav className="header-nav">
          {staff ? (
            <>
              <button onClick={() => setShowAdmin(true)}>经营资料</button>
              <button onClick={() => setShowPacking(true)}>装箱与箱唛</button>
              <a href="/packing" target="_blank" rel="noopener noreferrer">独立箱唛页面 ↗</a>
              <button onClick={() => { setLogisticsExceptionID(""); setShowLogistics(true); }}>物流跟进</button>
              <button onClick={() => setShowDashboard(true)}>老板看板</button>
              <button onClick={logout}>顾客体验</button>
            </>
          ) : (
            <>
              <a href="/packing" target="_blank" rel="noopener noreferrer">装箱与箱唛 ↗</a>
              <button onClick={() => setShowOrders(true)}>我的订单</button>
              <button onClick={() => setShowCases(true)}>
                我的售后
                {cases.some(
                  (c) => c.status === "draft" || c.status === "needs_info",
                ) && <span className="notification-dot" />}
              </button>
            </>
          )}
          <span className="header-divider" />
          <span className="identity">
            <UserRound size={17} />
            {session.user.name}
          </span>
          <button
            className="switch-user"
            onClick={logout}
            aria-label="切换身份"
          >
            <span>切换身份</span>
            <LogOut size={16} />
          </button>
        </nav>
      </header>
      {(error || connectionError) && (
        <div className="global-error" role="alert">
          <span>{error || connectionError}</span>
          {connectionError && <button className="text-link" onClick={() => void refreshConversation().then(() => setConnectionError("")).catch(e => setConnectionError(e.message))}>重新连接会话</button>}
          <button
            onClick={() => {
              setError("");
              setConnectionError("");
            }}
            aria-label="关闭错误"
          >
            <X size={16} />
          </button>
        </div>
      )}
      <div className="resource-errors">
        {resourceKeys.filter(key => resources[key].error && (key !== "products" || staff) && (key !== "queue" || staff)).map(key =>
          <ResourceNotice key={key} label={resourceLabels[key]} state={resources[key]} onRetry={() => void loadResource(key)} />)}
      </div>
      {loading ? (
        <div className="app-loading">
          <Spinner />
          正在准备店铺资料
        </div>
      ) : staff ? (
        <>
          <div className="staff-mobile-tabs">
            <button
              className={staffView === "list" ? "active" : ""}
              onClick={() => setStaffView("list")}
            >
              待处理
            </button>
            <button
              className={staffView === "chat" ? "active" : ""}
              onClick={() => setStaffView("chat")}
            >
              顾客对话
            </button>
            <button
              className={staffView === "case" ? "active" : ""}
              onClick={() => setStaffView("case")}
            >
              售后申请
            </button>
          </div>
          {selectedWork && <div className="work-filtered-note"><button className="text-link" onClick={() => setShowWorkOperations(true)}>当前事项：{selectedWork.title} · 打开安排与处理</button>{resources.queue.loaded && !workQueue.items.some(i => i.id === selectedWork.id && i.type === selectedWork.type) && <span> · 此事项已移出当前筛选</span>}</div>}
          <main className={`staff-workspace mobile-${staffView}`}>
            <aside className="conversation-sidebar">
              <div className="sidebar-title">
                <h2>未完成事项</h2>
                {resources.queue.loaded && !resources.queue.error && waitingCount > 0 && <span>{waitingCount}</span>}
              </div>
              <ResourceNotice label={staffFilter === "history" ? "会话" : "未完成事项"} state={resources[staffFilter === "history" ? "conversations" : "queue"]} onRetry={() => void loadResource(staffFilter === "history" ? "conversations" : "queue")} />
              <ServiceMetrics />
              <div className="sidebar-filters">
                {Object.entries(queueLabels).map(([value, label]) => <button key={value} className={staffFilter === value ? "active" : ""} onClick={() => setStaffFilter(value)}>{label} <small>{!resources.queue.loaded || resources.queue.error ? "—" : value === "all" ? workQueue.total : (workQueue.queue_stage_counts ?? workQueue.stage_counts)[value] || 0}</small></button>)}
                <button className={staffFilter === "history" ? "active" : ""} onClick={() => setStaffFilter("history")}>全部对话</button>
              </div>
              <label className="sidebar-search">
                <Search size={15} />
                <input
                  value={staffSearch}
                  onChange={(e) => setStaffSearch(e.target.value)}
                  placeholder="搜索顾客或对话"
                  aria-label="搜索顾客或对话"
                />
              </label>
              {staffFilter !== "history" && <><WorkQueueFilters value={workFilters} onChange={setWorkFilters} directoryRevision={directoryRevision} /><p className="work-queue-count">阶段按钮显示全部待办数量。当前筛选：{resources.queue.loaded && !resources.queue.error ? `${workQueue.filtered_total} 项` : "正在核对"}；时间均为北京时间。</p></>}
              <div className="conversation-list">
                {staffFilter === "history" ? conversations.filter(c => `${c.user_name} ${c.last_message}`.includes(staffSearch)).map(c => <button className={`conversation-item ${c.id === activeID ? "active" : ""}`} key={c.id} onClick={() => { setSelectedWork(null); setShowWorkOperations(false); setActiveID(c.id); setCaseID(""); setServiceID(""); setStaffView("chat"); }}><span className="avatar">{c.user_name}</span><span className="conversation-preview"><span><b>{c.user_name}</b><time>{time(c.updated_at)}</time></span><small>{c.last_message || "开始一段对话"}</small></span></button>) : workQueue.items.map(i => <button className={`conversation-item ${selectedWork?.type === i.type && selectedWork?.id === i.id ? "active" : ""}`} key={`${i.type}:${i.id}`} onClick={() => { if ((i.type as string) === "logistics_exception") { setLogisticsExceptionID(i.id); setShowLogistics(true); return; } setSelectedWork(i); setShowWorkOperations(false); if (i.conversation_id) setActiveID(i.conversation_id); setCaseID(i.type === "case" ? i.id : ""); setServiceID(i.type === "service_request" ? i.id : ""); setInspectorKind(i.type === "service_request" ? "service" : "case"); setStaffView(i.type === "conversation" ? "chat" : "case"); }}><span className="avatar">{i.user_name}</span><span className="conversation-preview"><span><b>{i.user_name}</b><time>{time(i.updated_at)}</time></span><strong>{i.title}</strong><small>{i.reason}</small><span className="badge neutral">{queueLabels[i.stage] || "跟进中"}{i.needs_handoff ? " · 待接入" : ""}</span><WorkItemOperationsSummary item={i} asOf={workQueue.as_of} /></span></button>)}
                {staffFilter !== "history" && resources.queue.loaded && !resources.queue.error && !workQueue.items.length && <Empty title="当前筛选下没有待办">可以调整负责人或期限筛选，已完成记录可在全部对话中查看。</Empty>}
              </div>
            </aside>
            <div className="staff-chat-column">
            <div className="staff-context-tools">{selectedWork && selectedWork.conversation_id === activeID && <button className="work-open-button" onClick={() => setShowWorkOperations(true)}>安排与处理此待办</button>}<HandoffSummary summary={currentData?.summary} onOpenRecord={openSupportRecord} /><button className="text-link" onClick={() => { setInspectorKind(inspectorKind === "case" ? "service" : "case"); setStaffView("case"); }}>{inspectorKind === "case" ? `人工受理 (${currentRequests.length})` : `退换补发 (${currentCases.length})`}</button></div>
            <Chat
              key={`staff-${activeID}`}
              data={currentData}
              user={session.user}
              products={products}
              onSend={send}
              onHandoff={() =>
                void run(
                  () => post(`/conversations/${activeID}/handoff`, {}),
                  "已接管对话，智能客服已暂停",
                )
              }
              onResume={() => void resumeAI()}
              switchingMode={switchingMode}
              onProduct={openProduct}
              onCase={(c) => {
                setCaseID(c.id);
                selectWorkRecord({ type: "case", id: c.id });
                setInspectorKind("case");
                setStaffView("case");
              }}
              onOrders={() => {}}
              onRetry={() => {}}
              onClearContext={() => {}}
              sending={sending}
            />
            </div>
            {((inspectorKind === "service" && !resources.requests.loaded) || (inspectorKind === "case" && !resources.cases.loaded)) ? <aside className="staff-inspector"><ResourceNotice label={inspectorKind === "service" ? "人工受理" : "售后申请"} state={resources[inspectorKind === "service" ? "requests" : "cases"]} onRetry={() => void loadResource(inspectorKind === "service" ? "requests" : "cases")} /></aside> : inspectorKind === "service" ? <StaffServiceInspector requests={currentRequests} selectedID={serviceID} summary={currentData?.summary} onSelect={id => { setServiceID(id); selectWorkRecord({ type: "service_request", id }); }} onUpdated={() => void run(async () => {}, "受理进度已更新")} onCases={() => setInspectorKind("case")} onOpenRecord={openSupportRecord} /> : (
            <StaffInspector
              key={activeCase?.id || activeID}
              item={activeCase}
              items={currentCases}
              products={products}
              onSelect={(c) => { setCaseID(c.id); selectWorkRecord({ type: "case", id: c.id }); }}
              onUpdated={() => void run(async () => {}, "申请进度已更新")}
            />
            )}
          </main>
        </>
      ) : (
        <>
          <main className="customer-workspace">
            <Catalog
              key={session.token}
              products={products}
              loadState={resources.products}
              onRetry={() => void loadResource("products")}
              consultEnabled={!!activeID}
              selected={currentData?.conversation.selected_product_id || null}
              onConsult={(p) => void consult(p)}
              onDetails={openProduct}
            />
            <div
              id="customer-chat"
              className="customer-chat-window"
              hidden={!chatOpen}
              role="dialog"
              aria-label="店铺客服对话"
              onKeyDown={(event) => {
                if (event.key === "Escape") closeChat();
              }}
            >
            <ResourceNotice label="会话" state={resources.conversations} onRetry={() => void loadResource("conversations")} />
            <PreferencePanel key={`preferences-${activeID}`} conversationID={activeID} preferences={currentData?.preferences} products={products} onUpdated={() => void run(async () => {})} />
            {currentRequests.length > 0 && <button className="support-chat-link" onClick={() => setShowCases(true)}>人工受理与跟进 · {currentRequests.filter(r => !["completed", "rejected"].includes(r.status)).length} 项未完成，查看进度</button>}
            <Chat
              data={currentData}
              user={session.user}
              products={products}
              onSend={send}
              onHandoff={() =>
                void run(
                  () => post(`/conversations/${activeID}/handoff`, {}),
                  "已请求人工，请等待售后同事接入",
                )
              }
              onResume={() => void resumeAI()}
              switchingMode={switchingMode}
              visible={chatOpen}
              onClose={closeChat}
              key={`${session.user.id}-${activeID}`}
              onProduct={openProduct}
              onCase={openCase}
              onOrders={() => setShowOrders(true)}
              onRetry={(id) => {
                const key = `retry-${id}`;
                const rid =
                  messageRequest.current[key] ||
                  (messageRequest.current[key] = requestID());
                void run(() =>
                  post(`/conversations/${activeID}/retry`, {
                    turn_id: id,
                    request_id: rid,
                  }),
                );
              }}
              onClearContext={() =>
                void run(() =>
                  patch(`/conversations/${activeID}/context`, {
                    product_id: null,
                  }),
                )
              }
              sending={sending}
            />
            </div>
            {!chatOpen && (
              <button
                ref={chatLauncher}
                className="chat-launcher"
                aria-label={unreadReplies ? `店铺客服，${unreadReplies}条未读回复` : "打开店铺客服"}
                aria-controls="customer-chat"
                aria-expanded={chatOpen}
                onClick={() => setChatOpen(true)}
              >
                <MessageCircle size={22} />
                <span>店铺客服</span>
                {unreadReplies > 0 && <span className="unread-count">{unreadReplies > 9 ? "9+" : unreadReplies}</span>}
              </button>
            )}
          </main>
        </>
      )}
      {compareIDs.length > 0 && !staff && (
        <div className="compare-tray">
          <span>已选 {compareIDs.length} 款好物</span>
          <button
            onClick={() => setShowCompare(true)}
            disabled={compareIDs.length < 2}
          >
            开始对比
            <ArrowRight size={16} />
          </button>
          <button
            className="icon-button"
            aria-label="清空对比"
            onClick={() => setCompareIDs([])}
          >
            <X size={16} />
          </button>
        </div>
      )}
      {detailProduct && (
        <ProductDetail
          key={`${detailProduct.id}-${detailProduct.selected_sku_id || ""}`}
          product={detailProduct}
          readOnly={staff}
          onClose={closeProduct}
          onConsult={(p) => void consult(p)}
          compareIDs={compareIDs}
          onCompare={toggleCompare}
          onPurchased={(order) => {
            if (getSession()?.token !== token) return;
            closeProduct();
            void run(async () => {}, `模拟下单成功，订单 ${order.id} 已签收`);
            setShowOrders(true);
          }}
        />
      )}
      {showOrders && (
        <OrdersDrawer
          orders={orders}
          loadState={resources.orders}
          onRetry={() => void loadResource("orders")}
          onClose={() => setShowOrders(false)}
          onApply={(o) => {
            setShowOrders(false);
            setCaseEdit({ order: o });
          }}
          onConsult={(o) => {
            setShowOrders(false);
            setChatOpen(true);
            void run(async () => {
              await patch(`/conversations/${activeID}/context`, {
                order_id: o.id,
              });
              await send(
                `我想咨询订单 ${o.id}（${o.product_name}）的情况。`,
                [],
              );
            });
          }}
        />
      )}
      {showCases && (
        <CasesDrawer
          items={cases}
          serviceRequests={serviceRequests}
          orders={orders}
          conversationID={activeID}
          loadState={resources.cases}
          requestState={resources.requests}
          orderState={resources.orders}
          onRetry={() => void loadResource("cases")}
          onRetryRequests={() => void loadResource("requests")}
          onRetryOrders={() => void loadResource("orders")}
          onUpdated={() => void run(async () => {})}
          onClose={() => setShowCases(false)}
          onSelect={openCase}
        />
      )}{" "}
      {caseEdit && (
        <CaseModal
          key={caseEdit.order.id}
          item={cases.find((c) => c.id === caseEdit.item?.id) || caseEdit.item}
          order={caseEdit.order}
          conversationID={activeID}
          products={products}
          onClose={() => setCaseEdit(null)}
          onSaved={(c) => {
            if (getSession()?.token !== token) return;
            setCaseEdit((current) =>
              current && current.order.id === c.order_id
                ? { item: c, order: c.order }
                : current,
            );
            void run(async () => {});
          }}
        />
      )}{" "}
      {showCompare && (
        <Comparison
          products={products.filter((p) => compareIDs.includes(p.id))}
          onClose={() => setShowCompare(false)}
          onConsult={() => {
            const names = products
              .filter((p) => compareIDs.includes(p.id))
              .map((p) => p.name);
            setShowCompare(false);
            setChatOpen(true);
            void run(() =>
              send(
                `请帮我比较${names.join("、")}，说明差异以及分别适合什么需求。`,
                [],
              ),
            );
          }}
        />
      )}
      {staff && selectedWork && showWorkOperations && <WorkOperations key={`${session.token}:${selectedWork.type}:${selectedWork.id}`} token={session.token} userID={session.user.id} target={selectedWork} outsideFilters={resources.queue.loaded && !workQueue.items.some(i => i.id === selectedWork.id && i.type === selectedWork.type)} onClose={() => setShowWorkOperations(false)} onUpdated={item => { setSelectedWork(item); void refresh(); }} onDirectoryUpdated={() => setDirectoryRevision(v => v + 1)} />}
      {staff && showPacking && <Suspense fallback={<div className="loading-panel"><Spinner />正在打开装箱与箱唛</div>}><Packing key={session.token} token={session.token} userID={session.user.id} onClose={() => setShowPacking(false)} /></Suspense>}
      {staff && showLogistics && <Suspense fallback={<div className="loading-panel"><Spinner />正在打开物流跟进</div>}><Logistics key={`${session.token}:${logisticsExceptionID}`} token={session.token} userID={session.user.id} initialExceptionID={logisticsExceptionID || undefined} onClose={() => setShowLogistics(false)} onChanged={() => void refresh()} /></Suspense>}
      {staff && showDashboard && <Suspense fallback={<div className="loading-panel"><Spinner />正在打开老板看板</div>}><OperationsDashboard key={session.token} token={session.token} userID={session.user.id} onClose={() => setShowDashboard(false)} /></Suspense>}
      {showAdmin && (
        <AdminDrawer
          onClose={() => setShowAdmin(false)}
          onUpdated={() => void run(async () => {})}
        />
      )}{" "}
      {toast && (
        <div className="toast" role="status">
          <Check size={18} />
          {toast}
        </div>
      )}
    </div>
  );
}
function EmployeePageEntry({ page }: { page: EmployeePage }) {
  const [session, setSession] = useState<Session | null>(getSession);
  const [employees, setEmployees] = useState<User[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);
  const alive = useRef(true);
  const staff = session?.user.role === "staff";
  useEffect(() => {
    alive.current = true;
    if (!staff) {
      const controller = new AbortController();
      api<User[]>("/personas", { signal: controller.signal }).then(rows => { if (!controller.signal.aborted) setEmployees(rows.filter(row => row.role === "staff")); }).catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
      return () => { alive.current = false; controller.abort(); requestSequence.current += 1; };
    }
    return () => { alive.current = false; requestSequence.current += 1; };
  }, [staff]);
  async function enter(employee: User) {
    if (busy) return;
    const sequence = ++requestSequence.current;
    setBusy(true); setError("");
    try {
      const next = await post<Session>("/sessions", { persona_id: employee.id });
      if (!alive.current || sequence !== requestSequence.current) return;
      persistSession(next); setSession(next);
    } catch (failure) { if (alive.current && sequence === requestSequence.current) setError((failure as Error).message); }
    finally { if (alive.current && sequence === requestSequence.current) setBusy(false); }
  }
  function switchIdentity() { requestSequence.current += 1; persistSession(null); setSession(null); setBusy(false); setError(""); }
  return <div className="employee-page-shell">
    <header className="site-header employee-page-header"><div className="brand-group"><a className="wordmark" href="/">日常好物</a><span className="workspace-label">{employeePageLabels[page]}</span></div><nav className="header-nav"><a href="/">返回商城与客服</a>{staff && <><a href="/packing">箱唛</a><a href="/logistics">物流</a><a href="/operations-dashboard">看板</a><span className="identity"><UserRound size={16} />{session.user.name}</span><button onClick={switchIdentity}>切换员工</button></>}</nav></header>
    {!staff ? <main className="employee-page-login"><span className="packing-eyebrow">员工工作区</span><h1>登录后进入{employeePageLabels[page]}</h1><p>{session ? `当前是顾客身份「${session.user.name}」，请选择员工身份继续。` : "使用已有员工身份，继续同一店铺的资料与操作记录。"}</p><ErrorNote error={error} /><div className="packing-toolbar">{employees.map(employee => <button key={employee.id} className="button dark" disabled={busy} onClick={() => void enter(employee)}>{busy ? <Spinner /> : <UserRound size={16} />}以{employee.name}进入</button>)}</div>{!employees.length && <p>正在读取员工身份…</p>}<a className="text-link" href="/">返回商城</a></main> : <Suspense fallback={<div className="loading-panel"><Spinner />正在打开{employeePageLabels[page]}</div>}>
      {page === "packing" ? <Packing key={session.token} token={session.token} userID={session.user.id} standalone onClose={() => { window.location.href = "/"; }} /> : page === "logistics" ? <Logistics key={session.token} token={session.token} userID={session.user.id} onClose={() => { window.location.href = "/"; }} /> : <OperationsDashboard key={session.token} token={session.token} userID={session.user.id} onClose={() => { window.location.href = "/"; }} />}
    </Suspense>}
  </div>;
}
function sortProducts(products: Product[]) {
  const preferred = [
    "E1",
    "K1",
    "T2",
    "M1",
    "E0",
    "E1P",
    "K1P",
    "F1",
    "T1",
    "T3",
    "G1",
    "M2",
  ];
  return [...products].sort(
    (a, b) => preferred.indexOf(a.id) - preferred.indexOf(b.id),
  );
}
