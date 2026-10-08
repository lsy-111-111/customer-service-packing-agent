import { useEffect, useRef, useState } from "react";
import type { HandoffSummaryData, Order, ServiceRequest, ServiceStatus, SupportRecordLink } from "./types";
import { api, date, money, patch, post, requestID } from "./api";
import { Empty, ErrorNote, Spinner } from "./ui";

export const serviceLabels = { repair: "维修受理", logistics: "物流核实", refund_only: "仅退款受理", followup: "未解决跟进" };
export const serviceStatusLabels: Record<ServiceStatus, string> = { submitted: "待人工核实", processing: "正在处理", needs_info: "待补充材料", awaiting_confirmation: "待顾客确认", approved: "待执行", completed: "处理完成", rejected: "已说明拒绝" };
export const queueLabels: Record<string, string> = { all: "全部未完成", pending: "待审核 / 核实", waiting_human: "待接入", processing: "处理中", needs_info: "待补材料", awaiting_confirmation: "待顾客确认", approved: "待执行" };
export function ServiceBadge({ status }: { status: ServiceStatus }) { return <span className={`badge ${status === "completed" ? "success" : status === "rejected" ? "danger" : "olive"}`}>{serviceStatusLabels[status]}</span>; }

type Feedback = { id: string; version: number; resolution: "solved" | "unresolved"; score: number | null; note: string; followup_request_id: string | null };
type FeedbackState = { feedback: Feedback | null; cycle: string; followup: ServiceRequest | null };
export function FeedbackPanel({ targetType, targetID, onUpdated }: { targetType: "case" | "service_request"; targetID: string; onUpdated: () => void }) {
  const [state, setState] = useState<FeedbackState | null>(null);
  const [score, setScore] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const ids = useRef<Record<string, string>>({});
  const lock = useRef(false);
  useEffect(() => {
    let live = true;
    async function read() {
      try { const v = await api<FeedbackState>(`/service-feedback?target_type=${targetType}&target_id=${encodeURIComponent(targetID)}`); if (live && !lock.current) setState(v); }
      catch (e) { if (live) setError((e as Error).message); }
    }
    void read();
    const timer = setInterval(() => { if (!lock.current && document.visibilityState !== "hidden") void read(); }, 5000);
    return () => { live = false; clearInterval(timer); };
  }, [targetType, targetID]);
  async function submit(resolution: "solved" | "unresolved") {
    if (!state || lock.current) return;
    const body = { target_type: targetType, target_id: targetID, resolution };
    const key = `${state.cycle}:${JSON.stringify(body)}`;
    lock.current = true; setBusy(true); setError("");
    try {
      const result = await post<Feedback>("/service-feedback", { ...body, request_id: ids.current[key] || (ids.current[key] = requestID()) });
      setState({ ...state, feedback: result }); setSaved(false); onUpdated();
    } catch (e) { setError((e as Error).message); } finally { lock.current = false; setBusy(false); }
  }
  async function details() {
    if (!state?.feedback || lock.current) return;
    const body = { expected_version: state.feedback.version, score: score ? Number(score) : null, note };
    const key = `${state.feedback.id}:${JSON.stringify(body)}`;
    lock.current = true; setBusy(true); setError("");
    try { const result = await patch<Feedback>(`/service-feedback/${state.feedback.id}`, { ...body, request_id: ids.current[key] || (ids.current[key] = requestID()) }); setState({ ...state, feedback: result }); setSaved(true); }
    catch (e) { setError((e as Error).message); } finally { lock.current = false; setBusy(false); }
  }
  return <section className="feedback-panel" aria-label="处理反馈">
    <h4>这次处理解决问题了吗？</h4>
    {state?.feedback ? <>
      <p role="status">已记录：{state.feedback.resolution === "solved" ? "已解决" : "未解决，已建立关联人工跟进"}{state.feedback.score ? ` · ${state.feedback.score}/5 分` : " · 未评分"}。原处理记录会保留。</p>
      {state.feedback.followup_request_id && <small>跟进编号：{state.feedback.followup_request_id}</small>}
      <details><summary>补充评分或原因（可选）</summary><div className="feedback-options"><select aria-label="服务评分（可选）" value={score} onChange={e => { setScore(e.target.value); setSaved(false); }}><option value="">未评分</option>{[1, 2, 3, 4, 5].map(n => <option key={n} value={n}>{n} 分</option>)}</select></div><textarea aria-label="反馈说明（可选）" rows={2} maxLength={2000} value={note} onChange={e => { setNote(e.target.value); setSaved(false); }} placeholder="还有哪些问题？可以补充说明，不影响跟进受理" /><button className="button" disabled={busy} onClick={() => void details()}>保存补充反馈</button>{saved && <p role="status">补充反馈已保存。</p>}</details>
    </> : <>
      <p className="muted small">点击“未解决”立即建立关联人工跟进。未评价不会记为满意。</p>
      <div className="feedback-options"><button className="button" disabled={busy || !state} onClick={() => void submit("solved")}>已解决</button><button className="button" disabled={busy || !state} onClick={() => void submit("unresolved")}>{busy && <Spinner />}未解决，继续跟进</button></div>
    </>}
    <ErrorNote error={error} />
  </section>;
}

export function HandoffSummary({ summary, onOpenRecord }: { summary?: HandoffSummaryData; onOpenRecord?: (record: SupportRecordLink) => void }) {
  if (!summary) return null;
  return <details className="handoff-summary"><summary>接续摘要与原始依据</summary>{summary.updated_at && <small>整理于 {date(summary.updated_at, true)}</small>}{summary.error && <p className="notice-box">{summary.error}</p>}
    <section className="handoff-goals"><h4>顾客目标</h4>{summary.goals?.length ? summary.goals.map(goal => <details key={goal.id}><summary>{goal.text}</summary><p>{goal.order_ids.length ? `关联订单：${goal.order_ids.join('、')}` : '尚未关联具体订单'}</p><small>来源：{goal.source_type === "intent" ? "已登记诉求" : goal.source_type === "case" ? "售后申请" : "人工受理"} {goal.id}</small>{goal.source_message_ids.length > 0 && <small>原始消息：{goal.source_message_ids.join('、')}，可在下方顾客陈述中展开核对。</small>}{[...goal.case_ids.map(id => ({ id, type: "case" as const })), ...goal.request_ids.map(id => ({ id, type: "service_request" as const }))].map(record => onOpenRecord ? <button className="text-link" key={`${record.type}:${record.id}`} onClick={() => onOpenRecord(record)}>打开{record.type === "case" ? "售后申请" : "人工受理"}</button> : null)}</details>) : <p className="muted small">暂无已登记的待处理目标，请结合原话确认。</p>}</section>
    <h4>系统已核对</h4>{summary.verified.length ? summary.verified.map((i, n) => <p key={n}>{i.text}<small>来源：{i.source === "order_record" ? "订单记录" : "售后核对记录"} {i.source_id}</small></p>) : <p className="muted small">暂未关联已核对订单。</p>}
    <h4>顾客陈述 · 尚不等于已核实</h4>{summary.claims.map(i => <details key={i.source_message_id}><summary>{i.text.slice(0, 65)}{i.text.length > 65 ? "…" : ""}</summary><blockquote>{i.text}</blockquote><small>{i.source === "customer_intake" ? "原始受理" : "原始消息"}：{i.source_message_id}{i.updated_at ? ` · ${date(i.updated_at, true)}` : ""}</small></details>)}
    {Boolean(summary.case_records?.length) && <><h4>正式售后记录 · 点击查看依据</h4>{summary.case_records?.map(record => <details key={record.source_id}><summary>{record.source_id} · {record.status}</summary><p>顾客诉求：{record.reason}</p>{record.condition_note && <p>顾客补充：{record.condition_note}</p>}{record.review_note && <p>人工审核：{record.review_note}</p>}{record.completion_note && <p>已记录结果：{record.completion_note}</p>}{record.history.map((h, n) => <p key={n}>{String(h.note || h.action || "记录已更新")}<small>{date(String(h.at || h.created_at || ""), true)}</small></p>)}</details>)}</>}
    {Boolean(summary.related_records?.length) && <section className="handoff-related"><h4>关联申请与受理</h4>{summary.related_records?.map(record => <p key={`${record.type}:${record.id}`}>{record.label}<small>{record.id}</small>{onOpenRecord && <button className="text-link" aria-label={`打开关联${record.type === "case" ? "申请" : "受理"} ${record.id}`} onClick={() => onOpenRecord(record)}>打开{record.type === "case" ? "申请" : "受理"}详情</button>}</p>)}</section>}
    <h4>未完成诉求</h4>{summary.unanswered.length ? summary.unanswered.map((i, n) => <p key={String(i.id || n)}>{String(i.text || i.summary || i.label || i.kind || "待接续诉求")}</p>) : <p className="muted small">暂无额外已登记诉求，请结合原话核对。</p>}
    {summary.last_action && <><h4>最后措施</h4><p>{summary.last_action.note}<small>{date(summary.last_action.at, true)} · 人工服务记录</small></p></>}
    {Boolean(summary.attempted_actions?.length) && <details><summary>查看已尝试措施与来源</summary>{summary.attempted_actions?.map((action, n) => <p key={n}>{action.note}<small>{date(action.at, true)} · 来源 {action.source_id}</small></p>)}</details>}
    {summary.next_steps.length > 0 && <><h4>下一步</h4>{summary.next_steps.map(i => <p key={i.request_id}>{i.text}</p>)}</>}
  </details>;
}

export function ServiceRequestView({ item, staff = false, onUpdated }: { item: ServiceRequest; staff?: boolean; onUpdated: () => void }) {
  const [note, setNote] = useState("");
  const [proposal, setProposal] = useState(false);
  const [description, setDescription] = useState(item.proposal?.description || "");
  const [amount, setAmount] = useState(String((item.proposal?.amount_cents || 0) / 100));
  const [payer, setPayer] = useState(item.proposal?.shipping_payer || "not_applicable");
  const [customerAction, setCustomerAction] = useState(item.proposal?.customer_action || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ids = useRef<Record<string, string>>({});
  const ended = ["completed", "rejected"].includes(item.status);
  async function act(action: string) {
    let extra = {};
    if (action === "propose") {
      if (!/^\d+(\.\d{1,2})?$/.test(amount)) { setError("金额请填写非负数字，最多两位小数"); return; }
      extra = { proposal: { description, amount_cents: Math.round(Number(amount) * 100), shipping_payer: payer, customer_action: customerAction } };
    }
    const payload = { action, expected_version: item.version, note, ...extra };
    const key = JSON.stringify(payload);
    setBusy(true); setError("");
    try { await post(`/service-requests/${item.id}/actions`, { ...payload, request_id: ids.current[key] || (ids.current[key] = requestID()) }); setNote(""); setProposal(false); onUpdated(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <article className="service-record" data-service-request-id={item.id}>
    <div className="row-between"><h3>{serviceLabels[item.kind]}</h3><ServiceBadge status={item.status} /></div>
    <p>{item.reason}</p><small className="muted">受理编号：{item.id}<br />{item.order_id ? `订单 ${item.order_id} · ` : ""}{date(item.updated_at, true)} 更新{item.assigned_staff_id ? ` · 负责人 ${item.assigned_staff_id}` : " · 等待分配人工"}</small>
    {item.parent_id && <p className="muted small">关联原记录：{item.parent_id}；原处理结果保持不变。</p>}
    {item.proposal && <div className="notice-box"><b>当前方案</b><p>{item.proposal.description}</p><p>方案涉及金额 ¥{money(item.proposal.amount_cents)} · {({ store: "店铺承担费用", customer: "顾客承担费用", not_applicable: "无运费安排" } as Record<string, string>)[item.proposal.shipping_payer]}</p>{item.proposal.customer_action && <p>请你配合：{item.proposal.customer_action}</p>}<small>确认仅代表同意此方案，人工审核和完成记录仍需分别处理。</small></div>}
    {item.review_note && <p className="review-note">人工说明：{item.review_note}</p>}
    {item.completion_note && <p className="review-note">处理结果：{item.completion_note}</p>}
    {!ended && <div className="service-actions">
      {staff || item.status === "needs_info" ? <label className="form-field">{staff ? "顾客可见的处理说明" : "补充说明"}<textarea rows={3} maxLength={2000} value={note} onChange={e => setNote(e.target.value)} /></label> : null}
      {staff ? <>
        {proposal && <div className="proposal-form"><label className="form-field">方案说明<textarea value={description} onChange={e => setDescription(e.target.value)} rows={3} maxLength={2000} /></label><label className="form-field">涉及金额（元）<input value={amount} inputMode="decimal" onChange={e => setAmount(e.target.value)} /></label><label className="form-field">费用承担<select value={payer} onChange={e => setPayer(e.target.value)}><option value="not_applicable">无运费安排</option><option value="store">店铺承担</option><option value="customer">顾客承担</option></select></label><label className="form-field">顾客配合事项<textarea value={customerAction} onChange={e => setCustomerAction(e.target.value)} rows={2} /></label><button className="button primary" disabled={busy || !description.trim() || !note.trim()} onClick={() => void act("propose")}>发送方案，请顾客确认</button></div>}
        <div className="service-action-buttons">
          {["submitted", "processing"].includes(item.status) && !item.assigned_staff_id && <button className="button" disabled={busy} onClick={() => void act("claim")}>接单处理</button>}
          <button className="button" disabled={busy} onClick={() => setProposal(!proposal)}>{item.proposal ? "调整方案并重新确认" : "拟定实质处理方案"}</button>
          {item.status !== "needs_info" && <button className="button" disabled={busy || !note.trim()} onClick={() => void act("needs_info")}>请顾客补充</button>}
          {item.proposal && item.customer_confirmed_version === item.version && ["submitted", "processing"].includes(item.status) && <button className="button primary" disabled={busy || !note.trim()} onClick={() => void act("approve")}>人工审核同意</button>}
          {(item.status === "approved" || (!item.proposal && item.kind !== "refund_only" && ["submitted", "processing", "needs_info"].includes(item.status))) && <button className="button primary" disabled={busy || !note.trim()} onClick={() => void act("complete")}>{item.proposal ? "记录演示处理完成" : "记录调查结论并完成"}</button>}
          <button className="button danger-text" disabled={busy || !note.trim()} onClick={() => void act("reject")}>说明拒绝</button>
        </div>
      </> : <>
        {item.status === "awaiting_confirmation" && <button className="button primary" disabled={busy} onClick={() => void act("confirm")}>{busy && <Spinner />}确认当前方案，提交人工审核</button>}
        {item.status === "needs_info" && <button className="button primary" disabled={busy || !note.trim()} onClick={() => void act("supplement")}>提交补充说明</button>}
      </>}
    </div>}
    <ErrorNote error={error} />
    <details className="service-history"><summary>查看处理记录</summary>{item.history.map((h, i) => <p key={i}>{h.note}<small>{date(h.at, true)} · {h.actor_role === "staff" ? "人工客服" : "顾客"}</small></p>)}</details>
    <p className="muted small">受理与完成均为本地服务记录，不会自动执行真实退款、维修或物流操作。</p>
    {!staff && item.status === "completed" && <FeedbackPanel key={`${item.id}-${item.version}`} targetType="service_request" targetID={item.id} onUpdated={onUpdated} />}
    {staff && <QualityTags targetType="service_request" targetID={item.id} />}
  </article>;
}

export function CustomerSupport({ conversationID, requests, orders, onUpdated }: { conversationID: string; requests: ServiceRequest[]; orders: Order[]; onUpdated: () => void }) {
  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState("logistics");
  const [orderID, setOrderID] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ids = useRef<Record<string, string>>({});
  async function create() {
    const body = { conversation_id: conversationID, order_id: orderID || null, kind, reason, explicit_request: true };
    const key = JSON.stringify(body);
    setBusy(true); setError("");
    try { await post("/service-requests", { ...body, request_id: ids.current[key] || (ids.current[key] = requestID()) }); setCreating(false); setReason(""); onUpdated(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <section className="customer-support"><div className="row-between"><h3>人工受理与跟进</h3><button className="text-link" onClick={() => setCreating(!creating)}>申请人工办理</button></div>
    {creating && <div className="proposal-form"><label className="form-field">办理事项<select value={kind} onChange={e => setKind(e.target.value)}><option value="logistics">物流核实</option><option value="repair">维修受理</option><option value="refund_only">仅退款受理</option></select></label><label className="form-field">相关订单<select value={orderID} onChange={e => setOrderID(e.target.value)}><option value="">暂未关联，请人工帮助核实</option>{orders.map(o => <option key={o.id} value={o.id}>{o.product_name} · {o.id}</option>)}</select></label><label className="form-field">希望我们处理什么<textarea rows={3} maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} /></label><p className="muted small">点击后登记人工受理。涉及费用或实质方案时，还会请你核对确认。</p><button className="button primary" disabled={busy || !reason.trim()} onClick={() => void create()}>{busy && <Spinner />}明确申请办理</button></div>}
    <ErrorNote error={error} />
    {requests.length ? requests.map(item => <ServiceRequestView key={`${item.id}-${item.version}`} item={item} onUpdated={onUpdated} />) : <p className="muted small">维修、物流核实、仅退款与未解决跟进会显示在这里。</p>}
  </section>;
}

export function StaffServiceInspector({ requests, selectedID, summary, onSelect, onUpdated, onCases, onOpenRecord }: { requests: ServiceRequest[]; selectedID: string; summary?: HandoffSummaryData; onSelect: (id: string) => void; onUpdated: () => void; onCases: () => void; onOpenRecord?: (record: SupportRecordLink) => void }) {
  const item = requests.find(i => i.id === selectedID) || requests.find(i => !["completed", "rejected"].includes(i.status)) || requests[0];
  return <aside className="staff-inspector support-inspector"><div className="row-between"><h2>人工受理</h2><button className="text-link" onClick={onCases}>查看退换补发</button></div><div className="inspector-inner"><HandoffSummary summary={summary} onOpenRecord={onOpenRecord} />{requests.length > 1 && <select className="case-selector" aria-label="选择人工受理" value={item?.id || ""} onChange={e => onSelect(e.target.value)}>{requests.map(r => <option key={r.id} value={r.id}>{serviceLabels[r.kind]} · {serviceStatusLabels[r.status]}</option>)}</select>}{item ? <ServiceRequestView key={`${item.id}-${item.version}`} item={item} staff onUpdated={onUpdated} /> : <Empty title="暂无人工受理">顾客明确请求办理后，会显示在这里。</Empty>}</div></aside>;
}

const qualityLabels: Record<string, string> = { factual_error: "事实错误", policy_error: "规则错误", missed_intent: "遗漏诉求", repeated_question: "重复追问", poor_empathy: "回应缺少体谅", unclear_next_step: "下一步不清楚", wrong_promise: "错误承诺" };
export function QualityTags({ targetType, targetID }: { targetType: "case" | "service_request"; targetID: string }) {
  const [tags, setTags] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const ids = useRef<Record<string, string>>({});
  async function submit() {
    const body = { target_type: targetType, target_id: targetID, tags, note };
    const key = JSON.stringify(body);
    setBusy(true); setStatus("");
    try { await post("/staff/service-quality", { ...body, request_id: ids.current[key] || (ids.current[key] = requestID()) }); setStatus("质检标签已记录，不改变顾客反馈或原处理结果。"); }
    catch (e) { setStatus((e as Error).message); } finally { setBusy(false); }
  }
  return <details className="quality-tags"><summary>工作人员质检标记</summary><div className="quality-options">{Object.entries(qualityLabels).map(([key, label]) => <label key={key}><input type="checkbox" checked={tags.includes(key)} onChange={e => setTags(v => e.target.checked ? [...v, key] : v.filter(t => t !== key))} />{label}</label>)}</div><textarea aria-label="质检说明" rows={2} maxLength={2000} value={note} onChange={e => setNote(e.target.value)} placeholder="记录具体问题及其依据，仅供服务改进" /><button className="button" disabled={busy || !tags.length} onClick={() => void submit()}>保存质检标记</button>{status && <p role="status">{status}</p>}</details>;
}
