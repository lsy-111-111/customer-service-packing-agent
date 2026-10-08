import { useEffect, useRef, useState } from "react";
import { api, APIError, getSession, requestID } from "./api";
import type { OperatorDirectory, WorkActionID, WorkActionPreview, WorkActionTemplates, WorkArrangement, WorkItem, WorkOperationResult, WorkOperator, WorkQueueFilters as Filters, WorkTarget, WorkRegistration } from "./types";
import { ErrorNote, Modal, Photo, Spinner } from "./ui";
import "./WorkOperations.css";

const emptyDirectory: OperatorDirectory = { items: [], suggested_operator_id: null, notice: "姓名仅用于内部责任登记，不创建登录账号，也不表示已通知该同事。" };
const sameTarget = (a: WorkTarget, b: WorkTarget) => a.type === b.type && a.id === b.id;
export function chinaDate(value?: string | null) {
  if (!value) return "未设置";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "时间资料待核对" : d.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function localDate(value?: string | null) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const parts = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  return parts.replace(" ", "T");
}
function isoDate(value: string) { return value ? new Date(`${value}:00+08:00`).toISOString() : null; }
function deadlineLabel(value?: string | null, asOf?: string) {
  const overdue = value && asOf && new Date(value).getTime() < new Date(asOf).getTime();
  return `${chinaDate(value)}${overdue ? " · 已超时" : ""}`;
}
// Keep a submission key for the exact payload even if a response was lost or the drawer reopened.
function submissionKey(userID: string, operation: string, payload: unknown) {
  const key = "work-operations-submissions";
  const signature = JSON.stringify([userID, operation, payload]);
  let stored: [string, string][] = [];
  try { const parsed: unknown = JSON.parse(sessionStorage.getItem(key) || "[]"); if (Array.isArray(parsed)) stored = parsed.filter((row): row is [string, string] => Array.isArray(row) && row.length === 2 && row.every(v => typeof v === "string")); } catch { /* A damaged local cache must not prevent work. */ }
  const found = stored.find(([s]) => s === signature);
  if (found) return found[1];
  const id = requestID();
  try { sessionStorage.setItem(key, JSON.stringify([...stored.slice(-79), [signature, id]])); } catch { /* The mounted form still holds its request key. */ }
  return id;
}
export function WorkQueueFilters({ value, onChange, directoryRevision }: { value: Filters; onChange: (v: Filters) => void; directoryRevision: number }) {
  const token = getSession()?.token;
  const [directory, setDirectory] = useState(emptyDirectory);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setDirectory(emptyDirectory); setError("");
    api<OperatorDirectory>("/staff/operators?include_inactive=true", { signal: controller.signal }).then(v => {
      if (!controller.signal.aborted && getSession()?.token === token) setDirectory(v);
    }).catch(e => { if (!controller.signal.aborted && getSession()?.token === token) setError(e.message); });
    return () => controller.abort();
  }, [token, directoryRevision, retry]);
  return <div className="work-queue-filters">
    <label>跟进负责人<select aria-label="筛选跟进负责人" value={value.assignee} onChange={e => onChange({ ...value, assignee: e.target.value })}><option value="all">全部负责人</option><option value="mine">由我跟进</option><option value="unassigned">尚未分配</option>{directory.items.map(o => <option key={o.id} value={o.id}>{o.name}{o.active ? "" : "（已停用）"}</option>)}</select></label>
    <div className="work-filter-pair"><label>查看哪种期限<select aria-label="查看哪种期限" value={value.deadlineScope} onChange={e => onChange({ ...value, deadlineScope: e.target.value as Filters["deadlineScope"] })}><option value="staff">店内跟进时间</option><option value="platform">平台要求时间</option></select></label><label>期限情况<select aria-label="筛选期限情况" value={value.deadline} onChange={e => onChange({ ...value, deadline: e.target.value })}><option value="all">全部时间</option><option value="overdue">已超时</option><option value="today">今天到期</option><option value="unset">未设置</option></select></label></div>
    {error && <div role="alert" className="work-error">负责人目录暂未载入：{error}<button type="button" onClick={() => setRetry(v => v + 1)}>重试</button></div>}
  </div>;
}
export function WorkItemOperationsSummary({ item, asOf }: { item: WorkItem; asOf?: string }) {
  const o = item.operations;
  return <span className="work-item-summary"><span>负责人：{o?.assignee?.name || "尚未分配"}</span><span>下一步：{o?.next_step || "待补充"}</span><span>店内：{deadlineLabel(o?.staff_due_at, asOf)}</span><span>平台：{deadlineLabel(o?.platform_deadline?.due_at, asOf)}{o?.platform_deadline ? "（人工登记）" : ""}</span>{o?.customer_wait.waiting && <span>等待顾客 · {o.customer_wait.reason}</span>}</span>;
}
type ArrangementForm = { assignee: string; nextStep: string; staffDue: string; platformDue: string; platformSource: string; waiting: boolean; waitReason: string; followup: "open" | "done"; reason: string };
const formFor = (o: WorkArrangement): ArrangementForm => ({ assignee: o.assignee?.id || "", nextStep: o.next_step || "", staffDue: localDate(o.staff_due_at), platformDue: localDate(o.platform_deadline?.due_at), platformSource: o.platform_deadline?.source_note || "", waiting: o.customer_wait.waiting, waitReason: o.customer_wait.reason || "", followup: o.followup_state, reason: "" });
type ActionForm = { assignee_id: string; next_step: string; reason: string; order_id: string; missing_item: string; missing_quantity: string; source_kind: "customer" | "staff"; source_message_id: string; source_quote: string; photo_requirements: string; materials_reviewed: boolean; evidence_attachment_ids: string[] };
const actionFormFor = (o: WorkArrangement): ActionForm => ({ assignee_id: o.assignee?.id || "", next_step: o.next_step || "", reason: "", order_id: "", missing_item: "", missing_quantity: "", source_kind: "customer", source_message_id: "", source_quote: "", photo_requirements: "", materials_reviewed: false, evidence_attachment_ids: [] });
function arrangementRows(o: WorkArrangement) { return { "负责人": o.assignee?.name || "尚未分配", "下一步": o.next_step || "待补充", "店内跟进时间": chinaDate(o.staff_due_at), "平台要求时间": o.platform_deadline ? `${chinaDate(o.platform_deadline.due_at)}；人工登记依据：${o.platform_deadline.source_note}` : "未设置", "等待顾客": o.customer_wait.waiting ? `是：${o.customer_wait.reason}` : "否", "店内跟进": o.followup_state === "done" ? "已结束（不改变售后审批）" : "继续跟进" }; }
function OwnerSelect({ value, operators, onChange, label = "跟进负责人" }: { value: string; operators: WorkOperator[]; onChange: (v: string) => void; label?: string }) {
  return <label>{label}<select aria-label={label} value={value} onChange={e => onChange(e.target.value)}><option value="">尚未分配，请选择</option>{operators.filter(o => o.active || o.id === value).map(o => <option key={o.id} value={o.id} disabled={!o.active}>{o.name}{o.active ? "" : "（已停用，请转派）"}</option>)}</select></label>;
}
function RegistrationDetails({ registration, operators, showArrangement = false }: { registration: WorkRegistration; operators: WorkOperator[]; showArrangement?: boolean }) {
  const data = registration.inputs;
  const text = (key: string) => typeof data[key] === "string" || typeof data[key] === "number" ? String(data[key]) : "";
  const attachments = Array.isArray(data.evidence_attachment_ids) ? data.evidence_attachment_ids : [];
  return <div className="work-registration">
    <h4>{registration.label}的登记内容{registration.status ? ` · ${registration.status}` : ""}</h4>
    {text("order_id") && <p>关联订单：{text("order_id")}</p>}
    {text("missing_item") && <><p>缺少的物品：{text("missing_item")} · {text("missing_quantity") || "待补充"} 件</p><p>陈述来源：{registration.source_label}</p><p>登记用于后续核实，不代表已确认少发或完成补发。</p></>}
    {text("source_quote") && <><p>顾客原话：</p><blockquote>{text("source_quote")}</blockquote></>}
    {text("photo_requirements") && <><p>请顾客补充的照片：{text("photo_requirements")}</p><p>现有材料：{data.materials_reviewed === true ? "员工已确认核对" : "尚未确认核对"}</p><p>{attachments.length ? `已核对 ${attachments.length} 份附件` : "此次未登记已核对的附件"}</p></>}
    {showArrangement && <><p>当次负责人：{operators.find(o => o.id === data.assignee_id)?.name || "原负责人资料待核对"}</p><p>当次下一步：{text("next_step") || "待补充"}</p></>}
  </div>;
}
export default function WorkOperations({ target, token, userID, onClose, onUpdated, onDirectoryUpdated, outsideFilters }: { target: WorkItem; token: string; userID: string; onClose: () => void; onUpdated: (item: WorkItem) => void; onDirectoryUpdated: () => void; outsideFilters: boolean }) {
  const [record, setRecord] = useState<WorkOperationResult | null>(null);
  const [templates, setTemplates] = useState<WorkActionTemplates | null>(null);
  const [directory, setDirectory] = useState(emptyDirectory);
  const [form, setForm] = useState<ArrangementForm | null>(null);
  const [arrangementPreview, setArrangementPreview] = useState<{ changes: Record<string, unknown>; rows: { label: string; before: string; after: string }[] } | null>(null);
  const [actionID, setActionID] = useState<WorkActionID | null>(null);
  const [action, setAction] = useState<ActionForm | null>(null);
  const [preview, setPreview] = useState<WorkActionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [stale, setStale] = useState(false);
  const [reload, setReload] = useState(0);
  const [name, setName] = useState("");
  const [operatorNote, setOperatorNote] = useState("");
  const [adding, setAdding] = useState(false);
  const [now, setNow] = useState(Date.now());
  const alive = useRef(true);
  const lock = useRef(false);
  const epoch = useRef(0);
  const confirmKeys = useRef(new Map<string, string>());
  const current = () => alive.current && getSession()?.token === token;
  const targetKey = `${target.type}:${target.id}`;
  const path = `/staff/work-items/operations?${new URLSearchParams({ type: target.type, id: target.id })}`;
  useEffect(() => { alive.current = true; return () => { alive.current = false; epoch.current += 1; }; }, []);
  useEffect(() => {
    const controller = new AbortController(); const generation = ++epoch.current;
    setLoading(true); setError(""); setRecord(null); setTemplates(null); setForm(null); setPreview(null); setActionID(null); setArrangementPreview(null); setStale(false);
    Promise.all([api<WorkOperationResult>(path, { signal: controller.signal }), api<WorkActionTemplates>(`/staff/work-items/action-templates?${new URLSearchParams({ type: target.type, id: target.id })}`, { signal: controller.signal }), api<OperatorDirectory>("/staff/operators?include_inactive=true", { signal: controller.signal })]).then(([r, t, d]) => {
      if (!controller.signal.aborted && current() && generation === epoch.current) {
        if (!sameTarget(r.item, target) || !sameTarget(t.target_summary, target)) throw new Error("待办归属已变化，请返回列表重新选择");
        setRecord(r); setTemplates(t); setDirectory(d); setForm(formFor(r.operations)); setAction(actionFormFor(r.operations));
      }
    }).catch(e => { if (!controller.signal.aborted && current() && generation === epoch.current) setError(e.message); }).finally(() => { if (!controller.signal.aborted && current() && generation === epoch.current) setLoading(false); });
    return () => controller.abort();
  }, [token, targetKey, reload]); // The parent keys this drawer by identity and target, never by conversation alone.
  useEffect(() => { if (!preview?.expires_at) return; const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, [preview?.expires_at]);
  async function perform<T,>(task: () => Promise<T>, success: (v: T) => void, affectsWork = true) {
    if (lock.current || !current()) return;
    lock.current = true; setBusy(true); setError(""); setNotice(""); const generation = epoch.current;
    try { const result = await task(); if (current() && generation === epoch.current) success(result); }
    catch (e) { if (current() && generation === epoch.current) { setError((e as Error).message); if (affectsWork && e instanceof APIError && e.status === 409) { setPreview(null); setArrangementPreview(null); setStale(true); } } }
    finally { lock.current = false; if (current() && generation === epoch.current) setBusy(false); }
  }
  function updateForm(change: Partial<ArrangementForm>) { setForm(f => f && ({ ...f, ...change })); setArrangementPreview(null); setError(""); }
  function updateAction(change: Partial<ActionForm>) { setAction(a => a && ({ ...a, ...change })); setPreview(null); setError(""); }
  function accept(r: WorkOperationResult, message: string) {
    if (!sameTarget(r.item, target)) { setError("返回的事项与当前待办不符，请刷新列表"); setStale(true); return; }
    setRecord(r); setForm(formFor(r.operations)); setAction(actionFormFor(r.operations)); setPreview(null); setArrangementPreview(null); setActionID(null); setNotice(message); onUpdated(r.item);
  }
  function previewArrangement() {
    if (!form || !record) return;
    if (!form.reason.trim()) { setError("请填写本次安排或转派的原因"); return; }
    if (form.waiting && !form.waitReason.trim()) { setError("请填写正在等待顾客补充什么"); return; }
    if (form.platformDue && !form.platformSource.trim()) { setError("请填写平台要求时间的依据，例如平台通知或商家后台记录"); return; }
    if (!form.platformDue && form.platformSource.trim()) { setError("请同时选择平台要求时间，或清空平台依据"); return; }
    const old = record.operations; const changes: Record<string, unknown> = {};
    const operator = directory.items.find(o => o.id === form.assignee) || null;
    if (form.assignee !== (old.assignee?.id || "")) changes.assignee_id = form.assignee || null;
    if (form.nextStep.trim() !== old.next_step) changes.next_step = form.nextStep.trim();
    if (form.staffDue !== localDate(old.staff_due_at)) changes.staff_due_at = isoDate(form.staffDue);
    const platform = form.platformDue ? { due_at: isoDate(form.platformDue)!, source_note: form.platformSource.trim() } : null;
    if (form.platformDue !== localDate(old.platform_deadline?.due_at) || form.platformSource.trim() !== (old.platform_deadline?.source_note || "")) changes.platform_deadline = platform;
    if (form.waiting !== old.customer_wait.waiting || form.waitReason.trim() !== old.customer_wait.reason) changes.customer_wait = { waiting: form.waiting, reason: form.waiting ? form.waitReason.trim() : "" };
    if (target.type === "conversation" && form.followup !== old.followup_state) changes.followup_state = form.followup;
    if (!Object.keys(changes).length) { setError("内容没有变化，请先修改本次安排"); return; }
    const after: WorkArrangement = { ...old, assignee: operator, next_step: form.nextStep.trim(), staff_due_at: isoDate(form.staffDue), platform_deadline: platform ? { ...platform, source_kind: "manual", source_label: "人工登记", collected_at: "", recorded_by: { id: userID, name: "当前员工" } } : null, customer_wait: { ...old.customer_wait, waiting: form.waiting, reason: form.waiting ? form.waitReason.trim() : "" }, followup_state: form.followup };
    const beforeRows = arrangementRows(old); const afterRows = arrangementRows(after);
    setArrangementPreview({ changes, rows: Object.entries(afterRows).filter(([label, value]) => beforeRows[label as keyof typeof beforeRows] !== value).map(([label, value]) => ({ label, before: beforeRows[label as keyof typeof beforeRows], after: value })) }); setError("");
  }
  function saveArrangement() {
    if (!record || !form || !arrangementPreview) return;
    const body = { target: { type: target.type, id: target.id }, expected_version: record.operations.version, changes: arrangementPreview.changes, reason: form.reason.trim() };
    const key = submissionKey(userID, "arrangement", body);
    void perform(() => api<WorkOperationResult>("/staff/work-items/operations", { method: "PATCH", body: JSON.stringify({ ...body, request_id: key }) }), r => accept(r, "店内安排已保存。此次保存没有发送顾客消息，也没有改变售后审批或执行结果。"));
  }
  function previewAction() {
    if (!record || !action || !actionID) return;
    const inputs: Record<string, unknown> = { assignee_id: action.assignee_id, next_step: action.next_step, reason: action.reason };
    if (actionID === "register_shortage") Object.assign(inputs, { order_id: action.order_id, missing_item: action.missing_item, missing_quantity: /^\d+$/.test(action.missing_quantity) ? Number(action.missing_quantity) : action.missing_quantity, source_kind: action.source_kind, source_message_id: action.source_kind === "customer" ? action.source_message_id : "", source_quote: action.source_kind === "customer" ? action.source_quote : "" });
    if (actionID === "wait_photos") Object.assign(inputs, { photo_requirements: action.photo_requirements, materials_reviewed: action.materials_reviewed, evidence_attachment_ids: action.evidence_attachment_ids });
    setPreview(null);
    void perform(() => api<WorkActionPreview>("/staff/work-items/action-preview", { method: "POST", body: JSON.stringify({ target: { type: target.type, id: target.id }, action_id: actionID, expected_version: record.operations.version, inputs }) }), v => {
      if (!sameTarget(v.target_summary, target)) { setError("预览事项与当前待办不符，请重新载入"); setStale(true); return; }
      setNow(Date.now()); setPreview(v);
      if (v.preview_id && v.fingerprint) { const payload = { preview_id: v.preview_id, fingerprint: v.fingerprint, expected_version: v.expected_version }; confirmKeys.current.set(v.preview_id, submissionKey(userID, "action", payload)); }
    });
  }
  function confirmAction() {
    if (!preview?.can_confirm || !preview.preview_id || !preview.fingerprint || stale || (preview.expires_at && Date.parse(preview.expires_at) <= Date.now())) return;
    const body = { preview_id: preview.preview_id, fingerprint: preview.fingerprint, expected_version: preview.expected_version };
    const key = confirmKeys.current.get(preview.preview_id) || submissionKey(userID, "action", body);
    confirmKeys.current.set(preview.preview_id, key);
    void perform(() => api<WorkOperationResult>("/staff/work-items/action-confirm", { method: "POST", body: JSON.stringify({ ...body, request_id: key }) }), r => accept(r, "处理卡已确认，预览中的员工回复已发送一次。内部安排和历史已更新。"));
  }
  const actionTemplate = templates?.items.find(t => t.id === actionID);
  const editable = !!templates?.target_summary.can_edit && record?.operations.followup_state !== "done" && !stale;
  const expired = !!preview?.expires_at && Date.parse(preview.expires_at) <= now;
  return <Modal title="待办安排与处理" subtitle={`${record?.item.user_name || target.user_name} · ${record?.item.title || target.title}`} wide onClose={onClose}>
    <div className="drawer-body work-operations">
      <p className="work-muted">仅处理当前选中的这一项待办。同一位顾客的其他事项需要分别安排。</p>
      {outsideFilters && <p className="work-notice">此事项已移出当前筛选，仍可在这里查看本次处理记录。</p>}
      {loading && <p><Spinner />正在读取待办、负责人和已有材料</p>}
      {error && <ErrorNote error={error} />}
      {notice && <p className="work-success" role="status">{notice}</p>}
      {(stale || (!loading && !record)) && <div className="work-notice">{stale ? "事项或材料已变化。请重新载入后核对，再生成预览。" : "待办资料未能完整载入。"}<button type="button" disabled={busy} onClick={() => setReload(v => v + 1)}>重新载入待办</button></div>}
      {record && form && templates && <>
        <section className="work-section"><h3>当前事项</h3><p><strong>{record.item.user_name} · {record.item.title}</strong></p><p>{record.item.reason || "尚未记录事项说明"}</p>{templates.target_summary.order_id && <p>关联订单：{templates.target_summary.order_id}</p>}<WorkItemOperationsSummary item={record.item} />{!templates.target_summary.can_edit && <p className="work-notice">此事项已结束，安排和处理卡仅供查看。</p>}</section>
        <details className="work-section"><summary>负责人目录 · 添加真实同事</summary><p className="work-muted">{directory.notice}</p><p>{directory.items.map(o => `${o.name}${o.active ? "" : "（已停用）"}`).join("、") || "尚未登记"}</p><div className="work-grid"><label>真实同事姓名<input value={name} maxLength={80} onChange={e => setName(e.target.value)} /></label><label>责任备注（选填）<input value={operatorNote} maxLength={500} onChange={e => setOperatorNote(e.target.value)} /></label></div><button type="button" disabled={busy || adding} onClick={() => {
          if (!name.trim()) { setError("请填写真实同事姓名"); return; }
          const body = { name: name.trim(), note: operatorNote.trim() }; const key = submissionKey(userID, "operator", body); setAdding(true);
          void perform(() => api<WorkOperator>("/staff/operators", { method: "POST", body: JSON.stringify({ ...body, request_id: key }) }), o => { setDirectory(d => ({ ...d, items: [...d.items.filter(v => v.id !== o.id), o] })); setName(""); setOperatorNote(""); setNotice(`已登记${o.name}。请在安排中选择负责人后保存，登记本身不会分配事项。`); onDirectoryUpdated(); }, false).finally(() => { if (current()) setAdding(false); });
        }}>登记同事</button></details>
        <section className="work-section"><h3>店内跟进安排</h3><p className="work-muted">时间均为北京时间。等待顾客不会暂停店内或平台期限。</p>
          <fieldset disabled={busy || !editable}><div className="work-grid"><OwnerSelect value={form.assignee} operators={directory.items} onChange={v => updateForm({ assignee: v })} /><label>下一步<textarea value={form.nextStep} maxLength={500} onChange={e => updateForm({ nextStep: e.target.value })} placeholder="例如：核对出库清单并联系顾客" /></label><label>店内跟进时间<input type="datetime-local" value={form.staffDue} onChange={e => updateForm({ staffDue: e.target.value })} /></label><label>平台要求时间（人工登记）<input type="datetime-local" value={form.platformDue} onChange={e => updateForm({ platformDue: e.target.value })} /></label><label className="work-full">平台时间依据<textarea value={form.platformSource} onChange={e => updateForm({ platformSource: e.target.value })} placeholder="填写平台通知、商家后台要求等依据；未核实请留空" /></label></div>
          {record.operations.platform_deadline && <p className="work-muted">原登记人：{record.operations.platform_deadline.recorded_by.name} · {chinaDate(record.operations.platform_deadline.collected_at)}</p>}
          <label className="work-check"><input type="checkbox" checked={form.waiting} onChange={e => updateForm({ waiting: e.target.checked })} />正在等待顾客补充材料或回复</label>{form.waiting && <label>等待内容<textarea value={form.waitReason} onChange={e => updateForm({ waitReason: e.target.value })} /></label>}
          {target.type === "conversation" && <><label>店内跟进状态<select value={form.followup} onChange={e => updateForm({ followup: e.target.value as ArrangementForm["followup"] })}><option value="open">继续跟进</option><option value="done">结束本条店内跟进</option></select></label><p className="work-muted">结束店内跟进不会批准、退款、补发或完成售后申请。</p></>}
          <label>本次安排或转派原因<textarea value={form.reason} onChange={e => updateForm({ reason: e.target.value })} placeholder="填写为什么调整负责人、下一步或期限" /></label><button type="button" onClick={previewArrangement}>预览内部安排</button>
          {arrangementPreview && <div className="work-preview"><h4>请核对本次内部变更</h4>{arrangementPreview.rows.map(row => <div key={row.label}><strong>{row.label}</strong><p>原来：{row.before}</p><p>保存后：{row.after}</p></div>)}<p>原因：{form.reason}</p><p>不会发送顾客消息。</p><button className="primary" type="button" onClick={saveArrangement}>确认保存内部安排</button></div>}
          </fieldset>
        </section>
        <section className="work-section"><h3>中文处理卡</h3><p className="work-muted">先填写和预览，再确认发送。登记事实和下一步不代表审批通过或已经执行。</p><div className="work-card-options">{templates.items.map(t => <button key={t.id} type="button" className={actionID === t.id ? "selected" : ""} disabled={busy || !editable || !!t.disabled_reason} title={t.disabled_reason || t.description} onClick={() => { setActionID(t.id); setAction(actionFormFor(record.operations)); setPreview(null); setError(""); }}><strong>{t.title}</strong><span>{t.disabled_reason || t.description}</span></button>)}</div>
          {action && actionTemplate && <fieldset disabled={busy || !editable}><h4>{actionTemplate.title}</h4><div className="work-grid"><OwnerSelect label="处理卡负责人" value={action.assignee_id} operators={directory.items} onChange={v => updateAction({ assignee_id: v })} /><label>处理卡下一步<textarea value={action.next_step} onChange={e => updateAction({ next_step: e.target.value })} /></label></div><label>处理原因<textarea value={action.reason} onChange={e => updateAction({ reason: e.target.value })} /></label>
            {actionID === "register_shortage" && <><label>关联订单<select value={action.order_id} onChange={e => updateAction({ order_id: e.target.value })}><option value="">请选择这位顾客的关联订单</option>{templates.available_orders.map(o => <option key={o.id} value={o.id}>{o.id} · {o.product_name} · {o.sku_label} · {o.quantity}件</option>)}</select></label><div className="work-grid"><label>缺少的物品<input value={action.missing_item} onChange={e => updateAction({ missing_item: e.target.value })} /></label><label>缺少数量<input inputMode="numeric" value={action.missing_quantity} onChange={e => updateAction({ missing_quantity: e.target.value })} placeholder="填写正整数" /></label></div><label>陈述来源<select value={action.source_kind} onChange={e => updateAction({ source_kind: e.target.value as ActionForm["source_kind"], source_message_id: "", source_quote: "" })}><option value="customer">顾客原话，待核实</option><option value="staff">员工登记，待核实</option></select></label>{action.source_kind === "customer" ? <><label>选择顾客原话<select value={action.source_message_id} onChange={e => { const m = templates.source_messages.find(m => m.id === e.target.value); updateAction({ source_message_id: m?.id || "", source_quote: m?.content || "" }); }}><option value="">请选择当前事项的顾客原话</option>{templates.source_messages.map(m => <option key={m.id} value={m.id}>{chinaDate(m.created_at)} · {m.content}</option>)}</select></label>{action.source_quote && <blockquote>{action.source_quote}</blockquote>}<p className="work-muted">引用顾客真实原文，保存后仍需核实出库情况。</p></> : <p className="work-notice">本次会标为员工登记的待核实情况，不作为顾客已确认的事实。</p>}</>}
            {actionID === "wait_photos" && <><h4>已有图片和材料</h4>{templates.existing_attachments.length ? <div className="work-attachments">{templates.existing_attachments.map(a => <div key={a.id}><Photo id={a.id} /><label className="work-check"><input type="checkbox" checked={action.evidence_attachment_ids.includes(a.id)} onChange={e => updateAction({ evidence_attachment_ids: e.target.checked ? [...action.evidence_attachment_ids, a.id] : action.evidence_attachment_ids.filter(id => id !== a.id) })} />已核对 {a.name || "顾客附件"}</label></div>)}</div> : <p>当前事项没有已上传的附件。</p>}<label className="work-check"><input type="checkbox" checked={action.materials_reviewed} onChange={e => updateAction({ materials_reviewed: e.target.checked })} />我已核对现有材料，只请顾客补充仍缺少的部分</label><label>需要补充的照片<textarea value={action.photo_requirements} onChange={e => updateAction({ photo_requirements: e.target.value })} placeholder="例如：外箱快递面单和收到的全部物品合照" /></label></>}
            <button type="button" onClick={previewAction}>生成处理卡预览</button>
          </fieldset>}
          {preview && <div className="work-preview" aria-label="处理卡预览"><h4>请核对后再确认</h4><p><strong>{preview.target_summary.user_name} · {preview.target_summary.title}</strong>{preview.target_summary.order_id ? ` · 订单 ${preview.target_summary.order_id}` : ""}</p>{preview.missing_fields.length > 0 && <div className="work-error" role="alert"><strong>请先补齐以下信息</strong><ul>{preview.missing_fields.map(m => <li key={m.field}>{m.label}：{m.message}</li>)}</ul></div>}{preview.planned_operations && <div><h4>将保存的跟进安排</h4><p>负责人：{preview.planned_operations.assignee.name}</p><p>下一步：{preview.planned_operations.next_step}</p><p>原因：{preview.planned_operations.reason}</p><p>等待顾客：{preview.planned_operations.customer_wait.waiting ? preview.planned_operations.customer_wait.reason : "否"}</p></div>}{preview.registration && <RegistrationDetails registration={preview.registration} operators={directory.items} />}<h4>内部将发生的变化</h4><ul>{preview.effects.map((effect, i) => <li key={i}>{effect}</li>)}</ul><h4>顾客将收到的完整回复</h4><p className="work-customer-message">{preview.customer_message || "补齐信息后生成顾客回复"}</p><p className="work-muted">{expired ? "预览已过期，请重新生成。" : preview.expires_at ? `请在 ${chinaDate(preview.expires_at)} 前确认；材料或安排变化后需重新预览。` : "补齐信息后才能确认。"}</p><button type="button" className="primary" disabled={busy || !editable || !preview.can_confirm || !preview.preview_id || expired} onClick={confirmAction}>{busy ? "正在提交…" : "确认处理并发送以上回复"}</button></div>}
        </section>
        <section className="work-section"><h3>安排与转派历史</h3>{record.operations.history.length ? <ol className="work-history">{[...record.operations.history].reverse().map(h => { const before = arrangementRows(h.before); const after = arrangementRows(h.after); return <li key={h.id}><p><strong>{h.action_label}</strong> · {h.actor.name} · {chinaDate(h.at)}</p><p>原因：{h.reason || "未补充"}</p>{h.registration && <RegistrationDetails registration={h.registration} operators={directory.items} showArrangement />}{Object.entries(after).filter(([key, value]) => before[key as keyof typeof before] !== value).map(([key, value]) => <p key={key}>{key}：{before[key as keyof typeof before]} → {value}</p>)}</li>; })}</ol> : <p className="work-muted">还没有保存过内部安排。</p>}</section>
      </>}
    </div>
  </Modal>;
}
