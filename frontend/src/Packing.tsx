import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { ArrowRight, Download, FileCheck2, FileText, LayoutTemplate, Package, ShieldCheck } from "lucide-react";
import { api, date, getSession, post, requestID } from "./api";
import { ErrorNote, Modal, Spinner } from "./ui";
import type { PackingPDF, PackingPDFPage } from "./PackingViewer";
import type { PackingSourceDraft } from "./PackingSources";
import { packingStorage } from "./PackingStorage";
import PackingSkuReview from "./PackingSkuReview";
import type { PackingSkuState, PackingSkuConfirmation } from "./PackingSkuReview";
import "./Packing.css";

const PackingViewer = lazy(() => import("./PackingViewer"));
const PackingSources = lazy(() => import("./PackingSources"));
type Product = { id: string; name: string; skus: { id: string; label: string; product_barcode?: string; packing_enabled?: boolean; packing_version?: number }[] };
type Requirements = { require_batch: boolean; require_expiry: boolean; require_serials: boolean };
type PackageConfig = { product_id: string; product_name: string; version: number; source_label: string; components: { name: string; quantity: number }[]; requirements: Requirements; sku_settings: PackingSkuState[]; reason?: string };
type PackagePreview = { preview_id: string; before: PackageConfig; candidate: PackageConfig; message: string };
const requirementLabels = [["require_batch", "需要批次追溯"], ["require_expiry", "需要效期管理"], ["require_serials", "需要逐件序列号"]] as const;
type Line = { packaging_snapshot?: PackageConfig; packaging_fingerprint?: string; sku_confirmation?: PackingSkuConfirmation; sku_safety_review?: PackingSkuState & { disabled: boolean; barcode_conflict: boolean; reason: string }; sku_id: string; product_name?: string; specification?: string; quantity: number; per_carton: number; batch: string; produced_at: string; expires_at: string; product_barcode: string; kit_note: string; serial_numbers: string | string[]; require_batch?: boolean; require_expiry?: boolean; require_serials?: boolean };
type Material = { id: string; line_index: number; name: string; per_item: number; planned_quantity: number; actual_quantity: number | null };
type Box = { serial_numbers: string[]; id: string; number: number; line_index: number; planned_quantity: number; actual_quantity: number | null; net_weight: number | null; gross_weight: number | null; code: string | null; labeled_at: string | null; scanned_at: string | null };
type Layout = { paper: string; kind: string; width_mm: number; height_mm: number; show_note: boolean };
type Form = { supplier: string; destination: string; platform_order: string; ship_date: string; note: string; lines: Line[]; intake_id?: string; intake_version?: number; layout?: Layout };
type Preview = Form & { materials: Material[]; boxes: Box[]; total_boxes: number; total_quantity: number; warnings: string[] };
type Document = { id: string; label: string; mode?: string; created_at: string; operator: string; box_numbers: number[]; reprint: boolean; revision: number; codes: string[]; layout?: Layout; pages?: PackingPDFPage[]; metadata?: { pages?: PackingPDFPage[] } };
type Task = Preview & { id: string; number: string; version: number; revision: number; stage: string; stage_label: string; approval: { operator: string; at: string; preview_document_id?: string } | null; sku_checks?: (PackingSkuState & { requires_recheck: boolean })[]; documents?: Document[]; intake_snapshot?: { intake_id: string; intake_version: number; customer?: string; title?: string }; demo?: boolean | string; history: { at: string; operator: string; action: string; note: string }[] };
type Summary = { id: string; number: string; stage_label: string; total_quantity: number; total_boxes: number; destination: string; updated_at: string; demo?: boolean | string };
type Change = { id: string; change_type: "revise" | "layout" | "cancel"; status: string; reason: string; source_note: string; task_version: number; created_at: string; differences: { label: string; before: unknown; after: unknown }[]; impact: string[]; resolution?: string };
type Revision = { revision: number; reason: string; created_at: string; task: Task };
type Demo = { normal: Task; abnormal: Task; old_code: string; old_code_rejection: string; comparison: { label: string; normal: unknown; abnormal: unknown }[] };
type Screen = "list" | "form" | "detail" | "packages" | "sources";
const defaultLayout = (): Layout => ({ paper: "a4", kind: "all", width_mm: 100, height_mm: 150, show_note: true });
const emptyLine = (): Line => ({ sku_id: "", quantity: 25, per_carton: 12, batch: "", produced_at: "", expires_at: "", product_barcode: "", kit_note: "", serial_numbers: "" });
const emptyForm = (): Form => ({ supplier: "", destination: "", platform_order: "", ship_date: new Date().toLocaleDateString("sv-SE"), note: "", lines: [emptyLine()] });
const problem = (error: unknown) => error instanceof Error ? error.message : "暂时无法完成，请稍后重试";
const businessValue = (value: unknown): string => value === null || value === undefined || value === "" ? "未填写" : Array.isArray(value) ? value.map(String).join("、") : typeof value === "boolean" ? value ? "是" : "否" : typeof value === "object" ? "资料已调整" : String(value);
const sameLayout = (a: Layout, b: Layout) => a.paper === b.paper && a.kind === b.kind && a.width_mm === b.width_mm && a.height_mm === b.height_mm && a.show_note === b.show_note;
const changeStatus: Record<string, string> = { pending: "待确认", requires_followup: "需要人工跟进", confirmed: "已确认生效", withdrawn: "已撤销", resolved: "人工跟进已记录", superseded: "已由新版替代" };

export default function Packing({ onClose, standalone = false, token, userID }: { onClose: () => void; standalone?: boolean; token: string; userID: string }) {
  const [products, setProducts] = useState<Product[]>([]);
  const [packages, setPackages] = useState<PackageConfig[]>([]);
  const [tasks, setTasks] = useState<Summary[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [screen, setScreen] = useState<Screen>("list");
  const [form, setForm] = useState<Form>(emptyForm);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [editTask, setEditTask] = useState<Task | null>(null);
  const [sourceTargetTask, setSourceTargetTask] = useState<Task | null>(null);
  const sourceTargetRef = useRef<Task | null>(null);
  const [reason, setReason] = useState("");
  const [sourceNote, setSourceNote] = useState("");
  const [inputMode, setInputMode] = useState<"form" | "excel" | "chat">("form");
  const [description, setDescription] = useState("");
  const [actual, setActual] = useState<Box[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewed, setReviewed] = useState(false);
  const [exceptions, setExceptions] = useState<Record<string, boolean>>({});
  const [previewDocumentID, setPreviewDocumentID] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [layout, setLayout] = useState<Layout>(defaultLayout);
  const [layoutReason, setLayoutReason] = useState("按本次打印纸张调整版式");
  const [reprintReason, setReprintReason] = useState("");
  const [changeDraft, setChangeDraft] = useState<Change | null>(null);
  const [changes, setChanges] = useState<Change[]>([]);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [resolution, setResolution] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [demo, setDemo] = useState<Demo | null>(null);
  const [pdf, setPdf] = useState<PackingPDF | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const alive = useRef(true);
  const lock = useRef(false);
  const taskRef = useRef(task);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  taskRef.current = task;
  const storageKey = `packing-view-v2:${userID}`;
  const current = () => alive.current && getSession()?.token === token;

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    setInitialLoading(true);
    Promise.all([api<Product[]>("/packing/catalog", { signal: controller.signal }), api<Summary[]>("/packing/tasks", { signal: controller.signal }), api<PackageConfig[]>("/packing/packages", { signal: controller.signal })]).then(async ([catalog, rows, packaging]) => {
      if (!current() || controller.signal.aborted) return;
      setProducts(catalog); setTasks(rows); setPackages(packaging);
      let restored: { taskID?: string; screen?: Screen; sourceTaskID?: string } = {};
      try { restored = JSON.parse(packingStorage.get(storageKey) || "{}"); } catch { packingStorage.remove(storageKey); }
      if (restored.screen === "sources" || restored.screen === "packages") {
        setScreen(restored.screen);
        if (restored.screen === "sources" && restored.sourceTaskID && rows.some(row => row.id === restored.sourceTaskID)) {
          const value = await api<Task>(`/packing/tasks/${restored.sourceTaskID}`, { signal: controller.signal });
          if (current() && !controller.signal.aborted) { sourceTargetRef.current = value; setSourceTargetTask(value); adopt(value); }
        }
      }
      else if (restored.taskID && restored.screen === "detail" && rows.some(row => row.id === restored.taskID)) await openTask(restored.taskID, controller.signal);
    }).catch(failure => { if (current() && !controller.signal.aborted) setError(problem(failure)); }).finally(() => { if (current() && !controller.signal.aborted) setInitialLoading(false); });
    return () => { alive.current = false; controller.abort(); };
  }, [token, storageKey]);
  useEffect(() => () => { if (pdf) URL.revokeObjectURL(pdf.url); }, [pdf]);
  useEffect(() => { if (pdf && window.innerWidth <= 1120) requestAnimationFrame(() => previewPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })); }, [pdf?.url]);

  function navigate(next: Screen, id = taskRef.current?.id) { if (!current()) return; setScreen(next); packingStorage.set(storageKey, JSON.stringify({ screen: next, taskID: id, sourceTaskID: next === "sources" ? sourceTargetRef.current?.id : undefined })); }
  function openSources(target?: Task) {
    sourceTargetRef.current = target || null; setSourceTargetTask(target || null);
    if (target?.intake_snapshot) packingStorage.set(`packing-intake-v2:${userID}`, target.intake_snapshot.intake_id);
    navigate("sources", target?.id); setPdf(null);
  }
  function adopt(value: Task, resetLayout = true) {
    if (!current()) return;
    const available = new Set(value.boxes.map(box => box.id));
    const sameTask = taskRef.current?.id === value.id;
    setTask(value); taskRef.current = value;
    setActual(value.boxes.map(box => ({ ...box })));
    setMaterials((value.materials || []).map(item => ({ ...item })));
    setSelected(previous => sameTask ? previous.filter(id => available.has(id)) : [...available]);
    setReviewed(false); setExceptions({}); setPreviewDocumentID("");
    if (resetLayout) setLayout({ ...defaultLayout(), ...value.layout });
  }
  async function run(action: () => Promise<void>) {
    if (lock.current || !current()) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (failure) { if (current()) setError(problem(failure)); }
    finally { lock.current = false; if (current()) setBusy(false); }
  }
  async function refreshTasks() { if (!current()) return; const rows = await api<Summary[]>("/packing/tasks"); if (current()) setTasks(rows); }
  async function loadHistory(id: string, signal?: AbortSignal) {
    if (!current()) return;
    const [entries, snapshots] = await Promise.all([api<Change[]>(`/packing/tasks/${id}/changes`, { signal }), api<Revision[]>(`/packing/tasks/${id}/revisions`, { signal })]);
    if (current() && taskRef.current?.id === id && !signal?.aborted) { setChanges(entries); setRevisions(snapshots); }
  }
  async function openTask(id: string, signal?: AbortSignal) {
    if (!current()) return;
    const value = await api<Task>(`/packing/tasks/${id}`, { signal });
    if (!current() || signal?.aborted) return;
    adopt(value); navigate("detail", id); setPdf(null); setChangeDraft(null); setResolution("");
    await loadHistory(id, signal);
  }
  function updateForm(update: Partial<Form>) { setForm(previous => ({ ...previous, ...update })); setPreview(null); setChangeDraft(null); }
  function defaultRequirements(skuID: string) { return packages.find(item => item.product_id === products.find(product => product.skus.some(sku => sku.id === skuID))?.id)?.requirements; }
  function skuState(skuID: string) { return packages.flatMap(item => item.sku_settings || []).find(item => item.sku_id === skuID); }
  function reviewedLines(value: Preview) { return value.lines.map((line, index) => ({ ...line, sku_confirmation: form.lines[index]?.sku_confirmation })); }
  function updateLine(index: number, update: Partial<Line>) { setForm(previous => ({ ...previous, lines: previous.lines.map((line, i) => i === index ? { ...line, ...update } : line) })); setPreview(null); setChangeDraft(null); }
  async function startForm(source?: Task) {
    const latest = await api<PackageConfig[]>("/packing/packages");
    if (!current()) return;
    setPackages(latest);
    sourceTargetRef.current = null; setSourceTargetTask(null);
    setForm(source ? { supplier: source.supplier, destination: source.destination, platform_order: source.platform_order, ship_date: source.ship_date, note: source.note, lines: source.lines.map(line => ({ ...line })), ...(source.intake_snapshot ? { intake_id: source.intake_snapshot.intake_id, intake_version: source.intake_snapshot.intake_version } : {}), layout: source.layout } : emptyForm());
    setEditTask(source || null); setPreview(null); setReason(""); setSourceNote(""); navigate("form"); setError(""); setNotice(""); setPdf(null); setChangeDraft(null);
  }
  async function applySource(source: PackingSourceDraft) {
    const latest = await api<PackageConfig[]>("/packing/packages");
    if (!current()) return;
    setPackages(latest);
    let target = sourceTargetRef.current;
    if (!target && source.bound_task_id) { target = await api<Task>(`/packing/tasks/${source.bound_task_id}`); if (!current()) return; }
    if (target) { target = await api<Task>(`/packing/tasks/${target.id}`); if (!current()) return; adopt(target); }
    const nextLayout = target?.layout || source.layout || { ...defaultLayout(), width_mm: source.label_width_mm, height_mm: source.label_height_mm };
    setForm({ supplier: source.supplier, destination: source.destination, platform_order: source.platform_order, ship_date: source.ship_date, note: source.note, lines: source.lines, intake_id: source.intake_id, intake_version: source.intake_version, layout: nextLayout });
    setPreview(null); setEditTask(target); setReason(target ? "按重新核对的客户资料更新本任务" : ""); setSourceNote(target ? `客户资料第 ${source.intake_version} 版人工核对结果` : ""); setChangeDraft(null); navigate("form", target?.id); setInputMode("form");
    setNotice(target ? `已将最新核对资料用于原任务 ${target.number}。请计算分箱并检查修改前后；${["dispatched", "archived"].includes(target.stage) ? "出库后变更会登记人工跟进。" : "确认变更后原审核与标签失效，需重新审核。"}` : "已应用人工核对的资料。请检查分箱，来源版本会随任务留存。");
    sourceTargetRef.current = null; setSourceTargetTask(null);
  }
  async function perform(action: string, extra: Record<string, unknown> = {}) {
    const target = taskRef.current; if (!target || !current()) return;
    const value = await post<Task>(`/packing/tasks/${target.id}/actions`, { action, version: target.version, request_id: requestID(), ...extra });
    if (!current()) return; adopt(value); await refreshTasks();
  }
  async function fetchPDF(path: string, name: string, metadata?: Document) {
    if (!current()) return;
    const response = await fetch(`/api${path}`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : "文件暂时无法打开，请刷新重试。"); }
    const blob = await response.blob(); if (!current()) return;
    setPdf({ url: URL.createObjectURL(blob), name, id: metadata?.id, pages: metadata?.pages || metadata?.metadata?.pages });
  }
  async function showDocument(document: Document, number: string) {
    await fetchPDF(`/packing/documents/${document.id}`, `${number}-${document.label}.pdf`, document);
    if (current() && document.mode === "draft" && document.layout?.kind === "all" && document.revision === taskRef.current?.revision && document.box_numbers.length === taskRef.current.boxes.length && sameLayout({ ...document.layout, kind: "all" }, { ...defaultLayout(), ...taskRef.current.layout, kind: "all" })) setPreviewDocumentID(document.id);
  }
  async function print(mode: string, reprint = false) {
    const target = taskRef.current; if (!target || !current()) return;
    if (!sameLayout(layout, { ...defaultLayout(), ...target.layout })) throw new Error("版式已修改，请先查看变更对照并保存版式，再预览与审核。");
    const boxIDs = mode === "draft" ? target.boxes.map(box => box.id) : selected;
    const result = await post<{ task: Task; document: Document }>(`/packing/tasks/${target.id}/documents`, { request_id: requestID(), version: target.version, mode, ...layout, kind: mode === "draft" ? "all" : layout.kind, box_ids: boxIDs, reprint, reason: reprintReason });
    if (!current()) return; adopt(result.task); await showDocument(result.document, result.task.number); await refreshTasks();
    if (mode === "draft" && current()) { setPreviewDocumentID(result.document.id); setNotice("已生成本单全部箱子的实际 PDF，请翻页核对后完成最终审核。"); }
  }
  async function prepareChange(changeType: Change["change_type"], proposed: unknown, changeReason: string) {
    const target = editTask && screen === "form" ? editTask : taskRef.current;
    if (!target || !current()) return;
    const value = await post<Change>(`/packing/tasks/${target.id}/changes/preview`, { version: target.version, request_id: requestID(), change_type: changeType, reason: changeReason, source_note: sourceNote, proposed });
    if (!current()) return; setChangeDraft(value); await loadHistory(target.id);
  }
  async function confirmChange(change: Change) {
    const target = taskRef.current; if (!target || !current()) return;
    const result = await post<{ task: Task; change: Change }>(`/packing/tasks/${target.id}/changes/${change.id}/confirm`, { version: target.version, request_id: requestID(), confirmed: true });
    if (!current()) return; adopt(result.task); setChangeDraft(null); setPdf(null); navigate("detail", result.task.id); await Promise.all([loadHistory(result.task.id), refreshTasks()]); setNotice("变更已确认。原审核与旧标签失效，请重新保存实装、预览 PDF 并审核。");
  }
  async function resolveChange(change: Change) {
    const target = taskRef.current; if (!target || !current()) return;
    await post<Change>(`/packing/tasks/${target.id}/changes/${change.id}/resolve`, { request_id: requestID(), resolution });
    if (!current()) return; setResolution(""); setChangeDraft(null); await loadHistory(target.id); setNotice(change.status === "pending" ? "本次变更已撤销，决定与理由已留存。" : "人工核实结果已记录，原出库记录仍可追溯。");
  }
  async function exportFile(kind: string) {
    const target = taskRef.current; if (!target || !current()) return;
    const name = `${target.number}-${({ po: "订单核对单", manifest: "装箱清单", sources: "来源核对报告", xlsx: "装箱清单" } as Record<string, string>)[kind]}`;
    if (kind !== "xlsx") { await fetchPDF(`/packing/tasks/${target.id}/exports/${kind}`, `${name}.pdf`); return; }
    const response = await fetch(`/api/packing/tasks/${target.id}/exports/xlsx`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error("清单暂时无法导出，请刷新重试。"); const blob = await response.blob(); if (!current()) return;
    const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `${name}.xlsx`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const actualDirty = !!task && (JSON.stringify(actual) !== JSON.stringify(task.boxes) || JSON.stringify(materials) !== JSON.stringify(task.materials || []));
  const active = !!task && !["archived", "dispatched", "cancelled"].includes(task.stage);
  const chosen = task?.boxes.filter(box => selected.includes(box.id)) || [];
  const layoutDirty = !!task && !sameLayout(layout, { ...defaultLayout(), ...task.layout });
  const pendingChanges = changes.filter(change => change.status === "pending");
  const finalIssues = task ? [...new Set([...task.warnings, ...(task.boxes.some(box => box.net_weight === null || box.gross_weight === null) ? ["部分箱子未称重，箱唛会明确注明未称重。"] : []), ...(!task.platform_order ? ["平台单号未提供，请核对仓库是否接受。"] : [])])] : [];
  const allExceptionsReviewed = finalIssues.every(issue => exceptions[issue]);
  const content = <div className={`packing-workspace ${standalone ? "packing-fullpage" : "drawer-body"}`}>
    {standalone && <div className="packing-page-intro"><div><span className="packing-eyebrow">员工发货工作区</span><h1>装箱与箱唛</h1><p>归集资料，逐箱核对，再审核打印。</p></div><div className="packing-page-steps"><span><FileText size={16} />资料</span><ArrowRight size={13} /><span><Package size={16} />实装</span><ArrowRight size={13} /><span><ShieldCheck size={16} />审核</span></div></div>}
    <div className="packing-disclaimer">本地试用 · 京东仓库要求待核对。模拟出库不扣减店铺库存，也不创建真实物流单。</div>
    <ErrorNote error={error} />{notice && <div className="packing-notice" role="status">{notice}</div>}
    <div className={`packing-content-layout ${pdf ? "with-pdf" : ""}`}><fieldset disabled={busy || initialLoading} className="packing-fieldset">
      <nav className="packing-nav" aria-label="装箱导航"><button className={`button ${screen === "list" ? "active" : ""}`} onClick={() => void run(async () => { await refreshTasks(); navigate("list"); setPdf(null); setChangeDraft(null); })}>任务列表</button><button className={`button ${screen === "sources" ? "dark" : ""}`} onClick={() => openSources()}>客户资料核对</button><button className="button dark" onClick={() => void run(() => startForm())}>直接新建任务</button><button className="button" onClick={() => void run(async () => { const value = await api<PackageConfig[]>("/packing/packages"); if (current()) { setPackages(value); navigate("packages"); setPdf(null); } })}>包装资料</button>{(busy || initialLoading) && <span><Spinner />正在处理</span>}</nav>
      {screen === "sources" && <>{sourceTargetTask && <div className="packing-notice">正在为原任务 <b>{sourceTargetTask.number}</b> 更新资料。人工核对完成后，将检查本次修改前后与影响。<button className="text-link" onClick={() => openSources()}>改为新建任务资料</button></div>}<Suspense fallback={<div className="packing-notice"><Spinner />正在打开资料核对</div>}><PackingSources key={token} token={token} userID={userID} onApply={applySource} /></Suspense></>}
      {screen === "packages" && <PackageEditor packages={packages} run={run} onSaved={saved => { if (!current()) return; setPackages(previous => previous.map(item => item.product_id === saved.product_id ? saved : item)); setNotice("包装资料与 SKU 设置已保存。旧任务保留快照，未完成订单须按当前状态重新核对。"); }} />}
      {screen === "list" && <section><div className="packing-section-title"><h3>装箱任务</h3><span>{tasks.length} 项</span></div>{!tasks.length ? <div className="packing-empty"><Package size={28} /><h3>从客户资料或一箱开始</h3><p>归集订单、仓库要求与聊天原文，或直接填写商品与每箱数量。</p><div className="packing-toolbar"><button className="button dark" onClick={() => navigate("sources")}>上传客户资料</button><button className="button" onClick={() => void run(() => startForm())}>直接填写</button></div></div> : <div className="packing-task-list">{tasks.map(row => <button className="packing-task-card" key={row.id} onClick={() => void run(() => openTask(row.id))}><span><b>{row.destination || "待填写收货仓库"}</b><small>装箱任务 {row.number}{row.demo ? " · 演示" : ""}</small></span><span>{row.total_quantity} 件 · {row.total_boxes} 箱<small>{row.stage_label} · {date(row.updated_at, true)}</small></span></button>)}</div>}
        <section className="packing-demo"><div><h3>正常与变更演示</h3><p>查看 25 件正常出库，与打印后改为 37 件的重新审核流程。</p><div className="packing-toolbar"><button className="button" onClick={() => void run(async () => { const result = await post<Demo>("/packing/demo/compare", { request_id: requestID() }); if (current()) { setDemo(result); await refreshTasks(); setNotice("两项本地演示任务已建立，变更后的旧箱码拒绝结果已记录。"); } })}>生成并对照两组演示</button><a className="button" href="/packing-demo/箱唛演示资料包.zip" download><Download size={15} />下载完整示例资料</a></div><p className="packing-muted">示例资料从 25 件原单开始。补充通知分时加入；37 件变更用于下一步修改核对。</p></div>{demo && <><div className="packing-demo-cards"><button onClick={() => void run(() => openTask(demo.normal.id))}><span>正常流程</span><b>{demo.normal.total_quantity} 件 · {demo.normal.total_boxes} 箱</b><small>{demo.normal.stage_label}</small></button><button onClick={() => void run(() => openTask(demo.abnormal.id))}><span>订单变更</span><b>{demo.abnormal.total_quantity} 件 · {demo.abnormal.total_boxes} 箱</b><small>{demo.abnormal.stage_label}</small></button></div><ComparisonTable rows={demo.comparison.map(row => ({ label: row.label, before: row.normal, after: row.abnormal }))} beforeLabel="正常流程" afterLabel="订单变更" /><p className="packing-demo-rejection">旧箱码核验：{demo.old_code_rejection}</p></>}</section>
      </section>}
      {screen === "form" && <section><div className="packing-section-title"><h3>{editTask ? "核对本次订单变更" : "填写发货内容"}</h3><span>填写 → 分箱核对 → 保存</span></div>
        {form.intake_id && <div className="packing-notice">此表单来自已核对的第 {form.intake_version} 版客户资料。关键内容需要修改时，请回资料单重新核对后应用。<button className="text-link" onClick={() => openSources(editTask || undefined)}>打开来源资料</button></div>}
        <div className="packing-tabs">{([["form", "直接填写"], ["excel", "导入表格"], ["chat", "用一句话描述"]] as const).map(([key, label]) => <button key={key} className={inputMode === key ? "active" : ""} onClick={() => setInputMode(key)} disabled={!!form.intake_id && key !== "form"}>{label}</button>)}</div>
        {inputMode === "excel" && <div className="packing-input-panel"><p>使用店内商品名称和规格填写标准模板；导入后仍可修改。最多 50 行，序列号用逗号分隔。</p><a className="button" href="/templates/装箱导入模板.xlsx" download>下载装箱模板</a><label className="packing-upload">选择已填写的表格<input type="file" accept=".xlsx" onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file) void run(async () => { const upload = new FormData(); upload.append("file", file); const result = await api<{ lines: Line[]; message: string }>("/packing/import", { method: "POST", body: upload }); if (current()) { updateForm({ lines: result.lines }); setNotice(result.message); } }); }} /></label></div>}
        {inputMode === "chat" && <div className="packing-input-panel"><label>描述你要装的商品<textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="例如：晨间陶瓷杯暖白，总共25件，每箱12件" maxLength={2000} /></label><button className="button" onClick={() => void run(async () => { const result = await post<{ sku_id: string | null; quantity: number | null; per_carton: number | null; questions: string[]; message: string }>("/packing/assist", { text: description }); if (current()) { updateForm({ lines: [{ ...emptyLine(), sku_id: result.sku_id || "", quantity: result.quantity || 0, per_carton: result.per_carton || 0 }] }); setNotice([result.message, ...result.questions].join(" ")); } })}>整理到下方表单</button><small>规格不明确时请手动选择；保存前逐项核对。</small></div>}
        <div className="packing-form-grid"><label>发货单位<input value={form.supplier} maxLength={200} onChange={event => updateForm({ supplier: event.target.value })} placeholder="例如：日常好物演示店" /></label><label>收货仓库<input value={form.destination} maxLength={200} onChange={event => updateForm({ destination: event.target.value })} placeholder="按实际收货资料填写" /></label><label>计划发货日期<input type="date" value={form.ship_date} onChange={event => updateForm({ ship_date: event.target.value })} /></label><label>平台单号（有资料再填）<input value={form.platform_order} maxLength={80} onChange={event => updateForm({ platform_order: event.target.value })} placeholder="未取得单号可以留空" /></label></div>
        {form.lines.map((line, index) => <div className="packing-line" key={index}><div className="packing-section-title"><b>商品 {index + 1}</b>{form.lines.length > 1 && <button className="button" onClick={() => updateForm({ lines: form.lines.filter((_, i) => i !== index) })}>移除此商品</button>}</div><div className="packing-line-primary"><label>商品与规格<select aria-label="商品与规格" value={line.sku_id} onChange={event => updateLine(index, { sku_id: event.target.value, product_barcode: skuState(event.target.value)?.product_barcode || "", sku_confirmation: undefined, require_batch: undefined, require_expiry: undefined, require_serials: undefined, packaging_fingerprint: undefined, packaging_snapshot: undefined })}><option value="">请选择准确规格</option>{products.map(product => <optgroup key={product.id} label={product.name}>{product.skus.map(sku => <option key={sku.id} value={sku.id}>{product.name} · {sku.label}{skuState(sku.id)?.packing_enabled === false ? "（停用，需本单确认）" : ""}</option>)}</optgroup>)}</select></label><label>总发货数量<input type="number" min="1" max="100000" value={line.quantity || ""} onChange={event => updateLine(index, { quantity: Number(event.target.value) })} /></label><label>每箱装多少件<input type="number" min="1" max="10000" value={line.per_carton || ""} onChange={event => updateLine(index, { per_carton: Number(event.target.value) })} /></label></div><details><summary>批次、效期、套装与序列号</summary><p className="packing-muted">默认要求来自包装资料。配件及每套用量请在“包装资料”维护。</p><div className="packing-requirements">{requirementLabels.map(([field, label]) => <label className="packing-check" key={field}><input type="checkbox" checked={line[field] ?? defaultRequirements(line.sku_id)?.[field] ?? false} onChange={event => updateLine(index, { [field]: event.target.checked })} />{label}</label>)}</div><div className="packing-form-grid"><label>批次<input value={line.batch} maxLength={80} onChange={event => updateLine(index, { batch: event.target.value })} /></label><label>提供的商品条码<input value={line.product_barcode} maxLength={80} onChange={event => updateLine(index, { product_barcode: event.target.value, sku_confirmation: undefined })} placeholder="照原资料填写" /></label><label>生产日期<input type="date" value={line.produced_at} onChange={event => updateLine(index, { produced_at: event.target.value })} /></label><label>到期日期<input type="date" value={line.expires_at} onChange={event => updateLine(index, { expires_at: event.target.value })} /></label><label className="packing-span">套装及配件说明<textarea value={line.kit_note} maxLength={500} onChange={event => updateLine(index, { kit_note: event.target.value })} /></label><label className="packing-span">逐件序列号<textarea value={Array.isArray(line.serial_numbers) ? line.serial_numbers.join("\n") : line.serial_numbers} onChange={event => updateLine(index, { serial_numbers: event.target.value })} placeholder="每件一个，用逗号或换行隔开" /></label></div></details><PackingSkuReview state={skuState(line.sku_id)} usedBarcode={line.product_barcode} value={line.sku_confirmation} label={`商品 ${index + 1}`} onChange={value => updateLine(index, { sku_confirmation: value })} /></div>)}
        <button className="button" disabled={form.lines.length >= 50} onClick={() => updateForm({ lines: [...form.lines, emptyLine()] })}>再加一行商品</button><label className="packing-note-field">发货备注<textarea value={form.note} maxLength={500} onChange={event => updateForm({ note: event.target.value })} /></label>
        {editTask && <div className="packing-form-grid"><label>修改原因<input value={reason} onChange={event => { setReason(event.target.value); setChangeDraft(null); }} maxLength={500} placeholder="如：客户通知数量由25改为37" /></label><label>变更通知来源<input value={sourceNote} onChange={event => setSourceNote(event.target.value)} maxLength={1000} placeholder="如：客户10月4日聊天确认" /></label></div>}
        <button className="button dark" onClick={() => void run(async () => { const result = await post<Preview>("/packing/preview", form); if (current()) { setPreview(result); setChangeDraft(null); } })}>计算并核对分箱</button>
        {preview && <div className="packing-preview"><h3>共 {preview.total_quantity} 件，分为 {preview.total_boxes} 箱</h3><p>每种规格、批次单独装箱，尾箱保留实际计划数量。</p><BoxPlan preview={preview} /><PackagingSnapshots lines={preview.lines} /><Materials items={preview.materials} task={preview} editable={false} /><ul>{preview.warnings.map(message => <li key={message}>{message}</li>)}</ul><button className="button dark" disabled={!!editTask && !reason.trim()} onClick={() => void run(async () => { if (editTask) { await prepareChange("revise", { ...form, lines: reviewedLines(preview) }, reason); } else { const value = await post<Task>("/packing/tasks", { ...form, lines: reviewedLines(preview), layout: form.layout || defaultLayout(), request_id: requestID() }); if (!current()) return; adopt(value); navigate("detail", value.id); await Promise.all([refreshTasks(), loadHistory(value.id)]); setNotice("装箱计划已保存，请逐箱记录实装数量。"); } })}>{editTask ? "查看修改前后与影响" : "核对无误，建立装箱任务"}</button></div>}
        {changeDraft && <ChangeReview change={changeDraft} busy={busy} resolution={resolution} onResolution={setResolution} onConfirm={() => void run(() => confirmChange(changeDraft))} onResolve={() => void run(() => resolveChange(changeDraft))} />}
      </section>}
      {screen === "detail" && task && <section><div className="packing-detail-heading"><div><small>装箱任务 {task.number} · 第 {task.revision} 版</small><h3>{task.destination || "收货仓库待填写"}</h3><p>{task.supplier || "发货单位待填写"} · {task.ship_date || "日期待填写"}{task.demo ? " · 本地演示" : ""}</p></div><span className="packing-status">{task.stage_label}</span></div>
        <div className="packing-stat-grid"><div><b>{task.total_quantity}</b><span>计划件数</span></div><div><b>{task.total_boxes}</b><span>总箱数</span></div><div><b>{task.boxes.filter(box => box.labeled_at).length}</b><span>已贴标</span></div><div><b>{task.boxes.filter(box => box.scanned_at).length}</b><span>已核验</span></div></div>
        <div className="packing-toolbar">{task.stage !== "cancelled" && <button className="button" onClick={() => void run(() => startForm(task))}>{active ? "登记资料变更" : "登记出库后变更"}</button>}<button className="button" onClick={() => void run(() => openTask(task.id))}>刷新最新记录</button><button className="button" onClick={() => void run(() => exportFile("po"))}><FileCheck2 size={15} />订单核对单</button><button className="button" onClick={() => void run(() => exportFile("manifest"))}>装箱清单 PDF</button><button className="button" onClick={() => void run(() => exportFile("xlsx"))}><Download size={15} />装箱清单 Excel</button><button className="button" onClick={() => void run(() => exportFile("sources"))}>来源核对报告</button></div>
        {active && task.sku_checks?.some(sku => sku.requires_recheck) && <div className="packing-disclaimer" role="alert">SKU 装箱状态或条码已经变化。请登记资料变更并重新核对本单例外、完整 PDF 和审核，之后才能继续打印或放行。</div>}{task.intake_snapshot && <div className="packing-notice">来源：{task.intake_snapshot.customer || "客户资料"} · 第 {task.intake_snapshot.intake_version} 版。<button className="text-link" onClick={() => openSources(task)}>为本任务更新资料与确认记录</button></div>}
        {pendingChanges.length > 0 && <div className="packing-disclaimer">有 {pendingChanges.length} 项变更等待确认。打印和出库需先处理这些变更。</div>}
        {changeDraft && <ChangeReview change={changeDraft} busy={busy} resolution={resolution} onResolution={setResolution} onConfirm={() => void run(() => confirmChange(changeDraft))} onResolve={() => void run(() => resolveChange(changeDraft))} />}
        <PackagingSnapshots lines={task.lines} saved />
        <section className="packing-block"><div className="packing-section-title"><h3>一、逐箱核对与实装</h3>{active && !task.approval && <button className="button" onClick={() => { setActual(previous => previous.map(box => ({ ...box, actual_quantity: box.planned_quantity }))); setMaterials(previous => previous.map(item => ({ ...item, actual_quantity: item.planned_quantity }))); setReviewed(false); setPreviewDocumentID(""); setNotice("计划数量已填入，请按现场情况核对后保存。"); }}>按计划填入，逐箱核对</button>}</div>
          <div className="packing-table-wrap"><table><thead><tr><th><input aria-label="选择全部箱子" type="checkbox" checked={selected.length === task.boxes.length} onChange={event => setSelected(event.target.checked ? task.boxes.map(box => box.id) : [])} /></th><th>箱号 / 商品</th><th>计划</th><th>实装件数</th><th>净重 / 公斤</th><th>毛重 / 公斤</th><th>标签状态</th></tr></thead><tbody>{actual.map((box, index) => { const line = task.lines[box.line_index]; return <tr key={box.id}><td><input aria-label={`选择第${box.number}箱`} type="checkbox" checked={selected.includes(box.id)} onChange={event => setSelected(previous => event.target.checked ? [...new Set([...previous, box.id])] : previous.filter(id => id !== box.id))} /></td><td><b>第 {box.number} 箱</b><small>{line.product_name} · {line.specification}</small>{line.batch && <small>批次：{line.batch}</small>}{box.serial_numbers?.length > 0 && <details><summary>查看 {box.serial_numbers.length} 个序列号</summary><p className="packing-serials">{box.serial_numbers.join("、")}</p></details>}</td><td>{box.planned_quantity}</td>{(["actual_quantity", "net_weight", "gross_weight"] as const).map(field => <td key={field}><input aria-label={`第${box.number}箱${field === "actual_quantity" ? "实装数量" : field === "net_weight" ? "净重" : "毛重"}`} type="number" min="0" step={field === "actual_quantity" ? "1" : "0.001"} disabled={!!task.approval || !active} value={box[field] ?? ""} placeholder={field === "actual_quantity" ? "待记录" : "未知"} onChange={event => { const value = event.target.value === "" ? null : Number(event.target.value); setActual(previous => previous.map((row, i) => i === index ? { ...row, [field]: value } : row)); setReviewed(false); setPreviewDocumentID(""); }} /></td>)}<td>{box.scanned_at ? "已核验" : box.labeled_at ? "已贴标" : box.code ? "待贴标" : "未生成"}</td></tr>; })}</tbody></table></div>
          <Materials items={materials} task={task} editable={active && !task.approval} onChange={value => { setMaterials(value); setReviewed(false); setPreviewDocumentID(""); }} /><p className="packing-muted">重量未知可留空，箱唛会注明未称重；数量差异须先登记资料变更。</p>{active && !task.approval && <button className="button dark" onClick={() => void run(async () => { await perform("actual", { boxes: actual, materials }); if (current()) setNotice("实装记录已保存。请生成全单草稿逐页核对。"); })}>保存全部实装记录</button>}
        </section>
        {active && <section className="packing-block packing-layout-block"><div className="packing-section-title"><h3>二、版式与实际 PDF 预览</h3><LayoutTemplate size={19} /></div><p>直接调整业务内容和常用版式，再查看实际 PDF。版式更改也需要重新审核。</p>
          <div className="packing-form-grid"><label>常用纸张与尺寸<select aria-label="常用纸张与尺寸" value={layout.paper === "a4" ? "a4" : ["100x150", "100x180", "150x200", "200x270"].includes(`${layout.width_mm}x${layout.height_mm}`) ? `${layout.width_mm}x${layout.height_mm}` : "custom"} onChange={event => { const value = event.target.value; if (value === "a4") setLayout(previous => ({ ...previous, paper: "a4" })); else if (value === "custom") setLayout(previous => ({ ...previous, paper: "label", width_mm: 110, height_mm: 160 })); else { const [width, height] = value.split("x").map(Number); setLayout(previous => ({ ...previous, paper: "label", width_mm: width, height_mm: height })); } setReviewed(false); }}><option value="a4">A4（210 × 297 毫米）</option><option value="100x150">标签纸 100 × 150 毫米</option><option value="100x180">标签纸 100 × 180 毫米</option><option value="150x200">标签纸 150 × 200 毫米</option><option value="200x270">标签纸 200 × 270 毫米</option><option value="custom">自定义标签尺寸</option></select></label><label>打印内容<select value={layout.kind} onChange={event => { setLayout(previous => ({ ...previous, kind: event.target.value })); setReviewed(false); }}><option value="all">正唛、侧唛与内部核验标签</option><option value="main">仅正唛</option><option value="side">仅侧唛</option><option value="internal">仅内部核验标签</option></select></label>{layout.paper === "label" && <><label>标签宽度 / 毫米<input type="number" min="100" max="200" value={layout.width_mm} onChange={event => setLayout(previous => ({ ...previous, width_mm: Number(event.target.value) }))} /></label><label>标签高度 / 毫米<input type="number" min="150" max="270" value={layout.height_mm} onChange={event => setLayout(previous => ({ ...previous, height_mm: Number(event.target.value) }))} /></label></>}</div>
          <div className="packing-column-options"><b>打印栏目</b><label className="packing-check"><input type="checkbox" checked disabled />商品与规格、箱号与数量、收货资料、追溯信息（必填）</label><label className="packing-check"><input type="checkbox" checked disabled />平台单号、称重结果或未称重提示（固定保留）</label><label className="packing-check"><input type="checkbox" checked={layout.show_note} onChange={event => setLayout(previous => ({ ...previous, show_note: event.target.checked }))} />发货备注（可选）</label></div>
          {layoutDirty && <div className="packing-layout-change"><p>当前选择与已保存版式不同，请先保存。已审核任务保存后，原审核和旧标签会失效。</p><label>版式调整原因<input value={layoutReason} onChange={event => setLayoutReason(event.target.value)} maxLength={500} /></label><button className="button" disabled={!layoutReason.trim() || actualDirty} onClick={() => void run(() => prepareChange("layout", layout, layoutReason))}>核对版式修改与影响</button></div>}
          <p className="packing-muted">尺寸是本地可配置模板，入仓前仍需按实际仓库要求核对。预览包含当前全部箱子和附页。</p><button className="button dark" disabled={actualDirty || layoutDirty || pendingChanges.length > 0} onClick={() => void run(() => print("draft"))}>生成全单草稿并预览 PDF</button>
        </section>}
        <section className="packing-block"><h3>三、每单最终审核与锁定</h3>{actualDirty && <p className="packing-disclaimer">实装或备料有未保存的修改，请先保存再审核与打印。</p>}{task.approval ? <div className="packing-approved"><ShieldCheck size={19} /><p>{task.approval.operator} 已于 {date(task.approval.at, true)} 核对通过。资料与版式变更后须重新预览及审核。</p></div> : <><p className="packing-muted">先保存实装，再查看当前全部箱子的 PDF。每项异常必须明确确认。</p><div className="packing-final-issues">{finalIssues.map(issue => <label className="packing-check" key={issue}><input type="checkbox" checked={exceptions[issue] || false} onChange={event => setExceptions(previous => ({ ...previous, [issue]: event.target.checked }))} />已核对：{issue}</label>)}</div><label className="packing-check"><input type="checkbox" checked={reviewed} disabled={!previewDocumentID || actualDirty || layoutDirty} onChange={event => setReviewed(event.target.checked)} />我已逐页核对本单 PDF、实装数量、商品规格、批次效期与收货资料</label>{!previewDocumentID && active && <p className="packing-muted">请先生成并查看全单草稿 PDF，才能最终审核。</p>}<button className="button dark" disabled={!reviewed || !allExceptionsReviewed || !previewDocumentID || !active || actualDirty || layoutDirty || pendingChanges.length > 0} onClick={() => void run(async () => { await perform("approve", { confirmed: reviewed, preview_document_id: previewDocumentID, exceptions_confirmed: finalIssues }); if (current()) setNotice("本单最终审核通过，内容与版式已锁定，可以生成正式箱唛。"); })}>确认本单最终审核并锁定</button></>}</section>
        {active && <section className="packing-block"><h3>四、生成与补印箱唛</h3><p>已选择 {selected.length} 箱。内部核验条码用于本店核对，打印内容按本单保存版式生成。</p><div className="packing-toolbar"><button className="button dark" disabled={!task.approval || !selected.length || actualDirty || layoutDirty || pendingChanges.length > 0} onClick={() => void run(() => print("formal"))}>生成审核后箱唛</button></div><details><summary>标签损坏或丢失，需要补印</summary><p>所选箱子的旧核验码会作废；补印后需重新贴标和扫码核验。</p><label>补印原因<input value={reprintReason} maxLength={300} onChange={event => setReprintReason(event.target.value)} placeholder="如：第二箱标签破损" /></label><button className="button" disabled={!task.approval || !reprintReason.trim() || !selected.length || chosen.some(box => !box.code) || layoutDirty || pendingChanges.length > 0} onClick={() => void run(() => print("formal", true))}>补印所选箱子并作废旧码</button></details></section>}
        {active && <section className="packing-block"><h3>五、贴标与扫码核验</h3><p>扫描内部核验标签上的箱子核验号。重复扫码不会增加出库数量。</p><button className="button" disabled={!selected.length || chosen.some(box => !box.code) || pendingChanges.length > 0} onClick={() => void run(async () => { await perform("label", { box_ids: selected }); if (current()) setNotice("所选箱子已确认贴标，请逐箱扫码。"); })}>确认所选箱子已贴好当前标签</button><form className="packing-scan" onSubmit={event => { event.preventDefault(); void run(async () => { const result = await post<{ task: Task; message: string }>("/packing/scan", { request_id: requestID(), task_id: task.id, code: scanCode.trim() }); if (!current()) return; adopt(result.task); setScanCode(""); setNotice(result.message); }); }}><label>箱子核验号<input value={scanCode} maxLength={80} onChange={event => setScanCode(event.target.value)} placeholder="请扫描内部核验条码" autoComplete="off" /></label><button className="button dark" disabled={!scanCode.trim() || pendingChanges.length > 0}>核验这一箱</button></form><button className="button dark" disabled={!task.boxes.every(box => box.scanned_at) || pendingChanges.length > 0} onClick={() => void run(async () => { await perform("dispatch"); if (current()) setNotice("模拟出库已完成，可以归档保存。"); })}>全部核验后模拟出库</button></section>}
        {task.stage === "dispatched" && <section className="packing-block"><h3>六、归档</h3><p>归档后保留本次出库、全部箱唛与操作记录。</p><button className="button dark" onClick={() => void run(async () => { await perform("archive"); if (current()) setNotice("装箱任务已归档。"); })}>完成并归档</button></section>}
        {task.stage !== "cancelled" && <details className="packing-block"><summary>取消本次装箱安排</summary><p>{active ? "取消须先核对原因与影响，确认后旧标签失效。" : "已出库或归档的任务会登记人工跟进，请核实拦截、退回或补发结果。"}</p><label>取消原因<input value={cancelReason} onChange={event => setCancelReason(event.target.value)} maxLength={500} /></label><button className="button" disabled={!cancelReason.trim()} onClick={() => void run(() => prepareChange("cancel", {}, cancelReason))}>核对取消安排与影响</button></details>}
        <details className="packing-block" open={pendingChanges.length > 0 || changes.some(change => change.status === "requires_followup")}><summary>变更前后与人工处理记录（{changes.length} 项）</summary>{changes.length ? changes.map(change => <article className="packing-change-history" key={change.id}><div className="packing-section-title"><b>{change.reason}</b><span className="packing-status">{changeStatus[change.status] || change.status}</span></div><small>{date(change.created_at, true)}{change.source_note ? ` · ${change.source_note}` : ""}</small><ComparisonTable rows={change.differences} />{change.resolution && <p>处理记录：{change.resolution}</p>}{["pending", "requires_followup"].includes(change.status) && <button className="button" onClick={() => { setChangeDraft(change); setResolution(""); }}>核对并处理此变更</button>}</article>) : <p className="packing-muted">暂无订单或版式变更。</p>}</details>
        <details className="packing-block"><summary>历史版本与箱唛文件</summary><button className="button" onClick={() => void run(() => openTask(task.id))}>更新文件与版本列表</button>{revisions.map(revision => <div className="packing-revision" key={revision.revision}><b>第 {revision.revision} 版</b><span>{revision.task.total_quantity} 件 · {revision.task.total_boxes} 箱 · {revision.task.destination}</span><small>{revision.reason} · {date(revision.created_at, true)}</small></div>)}{task.documents?.map(document => { const valid = document.revision === task.revision && (!document.codes.length || document.codes.every(code => task.boxes.some(box => box.code === code))); return <div className="packing-document" key={document.id}><span>{document.label}{document.reprint ? "（补印）" : ""} · 第 {document.box_numbers.join("、")} 箱<small>{date(document.created_at, true)} · {document.operator}{!valid ? " · 已失效，仅供追溯" : ""}</small></span><button className="button" onClick={() => void run(() => showDocument(document, task.number))}>{valid ? "查看当前 PDF" : "查看留存文件"}</button></div>; })}<ol className="packing-history">{task.history.slice().reverse().map((entry, index) => <li key={index}><b>{entry.action}</b><small>{entry.operator} · {date(entry.at, true)}</small>{entry.note && <p>{entry.note}</p>}</li>)}</ol></details>
      </section>}
    </fieldset>
    {pdf && <div className="packing-side-preview" ref={previewPanelRef}><Suspense fallback={<div className="packing-notice"><Spinner />正在绘制实际 PDF</div>}><PackingViewer document={pdf} onClose={() => setPdf(null)} /></Suspense></div>}</div>
  </div>;
  return standalone ? content : <Modal title="装箱与箱唛" subtitle="归集资料、逐箱实装、最终审核与扫码归档。" onClose={onClose} wide>{content}</Modal>;
}

function ComparisonTable({ rows, beforeLabel = "修改前", afterLabel = "修改后" }: { rows: { label: string; before: unknown; after: unknown }[]; beforeLabel?: string; afterLabel?: string }) {
  return <div className="packing-table-wrap packing-comparison"><table><thead><tr><th>核对项</th><th>{beforeLabel}</th><th>{afterLabel}</th></tr></thead><tbody>{rows.map((row, index) => <tr key={index}><th>{row.label}</th><td>{businessValue(row.before)}</td><td>{businessValue(row.after)}</td></tr>)}</tbody></table></div>;
}
function ChangeReview({ change, busy, resolution, onResolution, onConfirm, onResolve }: { change: Change; busy: boolean; resolution: string; onResolution: (value: string) => void; onConfirm: () => void; onResolve: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => setConfirmed(false), [change.id]);
  return <section className="packing-change-review"><div className="packing-section-title"><h3>{change.status === "requires_followup" ? "出库后变更 · 人工核实" : "请核对变更前后"}</h3><span className="packing-status">{changeStatus[change.status] || change.status}</span></div><p>{change.reason}</p>{change.source_note && <p className="packing-muted">通知来源：{change.source_note}</p>}<ComparisonTable rows={change.differences} /><div className="packing-change-impact"><b>本次影响</b><ul>{change.impact.map((impact, index) => <li key={index}>{impact}</li>)}</ul></div>{change.status === "pending" && <><label className="packing-check"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我已核对修改前后内容，并确认处理受影响的旧标签</label><button className="button dark" disabled={!confirmed || busy} onClick={onConfirm}>确认变更并使旧审核与标签失效</button></>}<details open={change.status === "requires_followup"}><summary>{change.status === "pending" ? "撤销本次变更" : "记录人工核实结果"}</summary><label>{change.status === "pending" ? "撤销原因" : "核实结果、处理决定与依据"}<textarea value={resolution} maxLength={1000} onChange={event => onResolution(event.target.value)} placeholder={change.status === "pending" ? "写明为何保留原安排" : "如：已联系仓库核实；记录实际决定与凭据"} /></label><button className="button" disabled={!resolution.trim() || busy} onClick={onResolve}>{change.status === "pending" ? "撤销本次变更并留存原因" : "保存人工跟进结果"}</button></details></section>;
}

function BoxPlan({ preview }: { preview: Preview }) { return <div className="packing-table-wrap"><table><thead><tr><th>箱号</th><th>商品与规格</th><th>批次</th><th>本箱件数</th></tr></thead><tbody>{preview.boxes.map(box => <tr key={box.id}><td>第 {box.number} 箱</td><td>{preview.lines[box.line_index].product_name}<small>{preview.lines[box.line_index].specification}</small></td><td>{preview.lines[box.line_index].batch || "未填写"}</td><td>{box.planned_quantity}</td></tr>)}</tbody></table></div>; }

function Materials({items, task, editable, onChange}: {items: Material[]; task: Preview; editable: boolean; onChange?: (items: Material[]) => void}) { if (!items.length) return null; return <div className="packing-materials"><h4>套装配件备料清单</h4><p className="packing-muted">按成品件数计算配件需求，配件不另计成品箱。请按实物核对。</p><div className="packing-table-wrap"><table><thead><tr><th>所属商品</th><th>配件</th><th>每件配套</th><th>需备数量</th><th>实备数量</th></tr></thead><tbody>{items.map((item,index) => <tr key={item.id}><td>{task.lines[item.line_index].product_name}</td><td>{item.name}</td><td>{item.per_item}</td><td>{item.planned_quantity}</td><td>{editable ? <input type="number" min="1" aria-label={item.name+"实备数量"} value={item.actual_quantity ?? ""} placeholder="待核对" onChange={event => onChange?.(items.map((row,i) => i===index ? {...row,actual_quantity:event.target.value ? Number(event.target.value) : null} : row))} /> : item.actual_quantity ?? "待核对"}</td></tr>)}</tbody></table></div></div>; }


function PackagingSnapshots({lines, saved = false}: {lines: Line[]; saved?: boolean}) {
  return <details className="packing-package-record"><summary>{saved ? "查看本任务保存的包装资料" : "核对包装资料与追溯要求"}</summary>
    <p className="packing-muted">{saved ? "本任务保留建立或修改时核对的清单，之后维护包装资料不会改变此记录。" : "保存任务后保留这份清单。套装按成品件数分箱，配件只计算备料数量。"}</p>
    {lines.map((line, index) => <div key={index}><b>{line.product_name} · {line.specification}</b><p>{line.packaging_snapshot?.source_label || "本任务原包装清单"}；{requirementLabels.filter(([field]) => line[field]).map(([,label]) => label).join("、") || "未要求批次、效期或逐件序列号"}</p>{line.sku_safety_review && <p>本单核对时：{line.sku_safety_review.packing_enabled ? "启用装箱" : "停用装箱"}；登记条码 {line.sku_safety_review.product_barcode || "未登记"}。{line.sku_safety_review.reason && <>本单例外依据：{line.sku_safety_review.reason}</>}</p>}</div>)}
  </details>;
}

function PackageEditor({packages, run, onSaved}: {packages: PackageConfig[]; run: (fn: () => Promise<void>) => Promise<void>; onSaved: (saved: PackageConfig) => void}) {
  const [draft, setDraft] = useState<PackageConfig | null>(null);
  const [checked, setChecked] = useState<PackagePreview | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [skuReason, setSKUReason] = useState("");
  function change(update: Partial<PackageConfig>) { setDraft(current => current ? {...current, ...update} : null); setChecked(null); }
  return <section className="packing-package-editor"><h3>包装资料</h3><p>设置每套成品附带的配件及用量，分箱时自动列出备料清单。这里只记录装箱安排，不改店铺库存或售后配件资料。</p>
    <label>选择维护包装资料的商品<select aria-label="选择维护包装资料的商品" value={draft?.product_id || ""} onChange={event => { const value = packages.find(item => item.product_id === event.target.value); setDraft(value ? {...value, components: value.components.map(part => ({...part})), requirements: {...value.requirements}, sku_settings: value.sku_settings.map(sku => ({...sku}))} : null); setChecked(null); setSKUReason(""); }}><option value="">请选择商品</option>{packages.map(item => <option key={item.product_id} value={item.product_id}>{item.product_name}</option>)}</select></label>
    {draft && <><p className="packing-muted">目前：{draft.source_label}。此设置适用于这个商品的各个规格；每行配件按每套成品的用量填写。</p><h4>每个 SKU 的条码与装箱启停</h4><p className="packing-muted">商品条码按文本保存，保留前导零。停用后，不会自动套用客户映射；确需装箱须逐单说明原因并确认。</p>{draft.sku_settings.map(sku => <div className="packing-package-row" key={sku.sku_id}><b>{sku.specification} · {sku.sku_id}</b><label>商品条码<input aria-label={`${sku.sku_id}登记商品条码`} value={sku.product_barcode} maxLength={80} onChange={event => change({sku_settings: draft.sku_settings.map(row => row.sku_id === sku.sku_id ? {...row, product_barcode: event.target.value} : row)})} placeholder="无条码可留空，编号按文本填写" /></label><label className="packing-check"><input type="checkbox" aria-label={`${sku.sku_id}启用装箱`} checked={sku.packing_enabled} onChange={event => change({sku_settings: draft.sku_settings.map(row => row.sku_id === sku.sku_id ? {...row, packing_enabled: event.target.checked} : row)})} />启用装箱</label></div>)}<label>SKU 修改原因与依据<textarea aria-label="SKU 修改原因与依据" value={skuReason} maxLength={500} onChange={event => { setSKUReason(event.target.value); setChecked(null); }} placeholder="修改条码或启停时必须填写" /></label><h4>每套配件清单</h4>
      {!draft.components.length && <p>当前无需额外配件。需要时点击下方“添加配件”。</p>}
      {draft.components.map((part, index) => <div className="packing-package-row" key={index}><label>配件名称<input aria-label={`第${index + 1}项配件名称`} value={part.name} maxLength={80} placeholder="例如：杯垫" onChange={event => change({components: draft.components.map((row, i) => i === index ? {...row, name: event.target.value} : row)})} /></label><label>每套用量<input aria-label={`第${index + 1}项配件每套用量`} type="number" min="1" max="10000" value={part.quantity || ""} onChange={event => change({components: draft.components.map((row, i) => i === index ? {...row, quantity: Number(event.target.value)} : row)})} /></label><button className="button" aria-label={`移除第${index + 1}项配件`} onClick={() => change({components: draft.components.filter((_, i) => i !== index)})}>移除配件</button></div>)}
      <button className="button" disabled={draft.components.length >= 30} onClick={() => change({components: [...draft.components, {name: "", quantity: 1}]})}>添加配件</button><h4>新任务默认追溯要求</h4><div className="packing-requirements">{requirementLabels.map(([field, title]) => <label className="packing-check" key={field}><input type="checkbox" checked={draft.requirements[field]} onChange={event => change({requirements: {...draft.requirements, [field]: event.target.checked}})} />{title}</label>)}</div><p className="packing-muted">默认要求仍需员工按实际资料核对，新任务可调整适用选项；未补齐已经要求的资料不能通过审核。</p>
      <button className="button dark" onClick={() => run(async () => { setChecked(await post<PackagePreview>("/packing/packages/preview", {product_id: draft.product_id, expected_version: draft.version, components: draft.components, requirements: draft.requirements, sku_settings: draft.sku_settings, reason: skuReason})); setConfirmation(requestID()); })}>预览包装资料</button>
      {checked && <div className="packing-preview"><h3>请核对 {checked.candidate.product_name} 的包装安排</h3><p>{checked.message}</p><h4>保存后的 SKU 设置</h4><ul>{checked.candidate.sku_settings.map(sku => <li key={sku.sku_id}>{sku.specification}：{sku.packing_enabled ? "启用装箱" : "停用装箱"}；条码 {sku.product_barcode || "未登记"}</li>)}</ul>{skuReason && <p>核对依据：{skuReason}</p>}<h4>保存后的每套用量</h4>{checked.candidate.components.length ? <ul>{checked.candidate.components.map(part => <li key={part.name}>{part.name}：每套 {part.quantity} 个</li>)}</ul> : <p>无需额外配件，备料清单将为空。</p>}<p>默认追溯要求：{requirementLabels.filter(([field]) => checked.candidate.requirements[field]).map(([, label]) => label).join("、") || "暂无"}</p><p className="packing-muted">原清单：{checked.before.components.map(part => `${part.name} 每套 ${part.quantity} 个`).join("；") || "无需额外配件"}</p><button className="button dark" onClick={() => run(async () => { const saved = await post<PackageConfig>("/packing/packages/confirm", {preview_id: checked.preview_id, request_id: confirmation}); onSaved(saved); setDraft(saved); setChecked(null); })}>核对无误，保存包装资料</button></div>}
    </>}
  </section>;
}
