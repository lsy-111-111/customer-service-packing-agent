import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { CheckCircle2, FileText, Plus, Upload } from "lucide-react";
import { api, date, getSession, post, requestID } from "./api";
import { ErrorNote, Spinner } from "./ui";
import type { PackingPDF } from "./PackingViewer";
import { packingStorage } from "./PackingStorage";
import PackingSkuReview, { skuNeedsReview } from "./PackingSkuReview";
import type { PackingSkuState, PackingSkuConfirmation } from "./PackingSkuReview";
import "./PackingSources.css";

const PackingViewer = lazy(() => import("./PackingViewer"));
type Citation = { source_id: string; chunk_id?: string; quote: string; image_id?: string; locator?: Locator };
type Locator = { page?: number; sheet?: string; cell?: string; row?: number; paragraph?: number; table?: number; column?: string };
type Chunk = { id: string; text: string; locator: Locator; image_id?: string };
type Source = { id: string; name: string; kind: string; role: string; url: string; chunks: Chunk[]; pages: { page: number; image_id: string; url: string; scanned?: boolean }[] };
type Candidate = { id: string; value: unknown; origin: string; citations: Citation[] };
type RuleCandidate = { field: string; value: unknown; citations: Citation[]; id?: string };
type Field = { id: string; path: string; line_id?: string; title: string; value: unknown; origin: string; confirmed: boolean; issue?: string; candidates: Candidate[]; citations: Citation[]; rejected_candidates?: { id: string; value: unknown; error: string; citations: Citation[] }[]; active_rejections?: string[] };
type SourceLine = { id: string; index: number; sku_id?: string; mapping_status: string; candidates: { sku_id: string; product_name: string; specification: string; product_barcode?: string; packing_enabled?: boolean; packing_version?: number; barcode_match?: boolean; barcode_conflict?: boolean }[]; field_ids: string[] };
type CatalogProduct = { id: string; name: string; skus: { id: string; label: string; product_barcode: string; packing_enabled: boolean; packing_version: number }[] };
type IntakeSummary = { id: string; title: string; customer: string; version: number; status: string; updated_at?: string; ready?: boolean };
type Intake = IntakeSummary & { sources: Source[]; fields: Field[]; lines: SourceLine[]; rules: { required_fields: string[]; label_width_mm: number | null; label_height_mm: number | null; confirmed: boolean; candidates: RuleCandidate[]; citations: Citation[] }; conflicts: { title: string; field_id?: string }[]; missing: string[]; history: { version: number; operator: string; action: string; reason: string; at: string }[]; report?: { title: string; messages: string[]; model_status?: string } };
export type PackingSourceDraft = { intake_id: string; intake_version: number; bound_task_id?: string | null; supplier: string; destination: string; ship_date: string; platform_order: string; note: string; layout?: { paper: string; kind: string; width_mm: number; height_mm: number; show_note: boolean }; lines: { sku_id: string; quantity: number; per_carton: number; batch: string; produced_at: string; expires_at: string; product_barcode: string; sku_confirmation?: PackingSkuConfirmation; kit_note: string; serial_numbers: string | string[]; require_batch?: boolean; require_expiry?: boolean; require_serials?: boolean }[]; label_width_mm: number; label_height_mm: number };
const ruleNames: Record<string, string> = { supplier: "发货单位", destination: "收货仓库", ship_date: "发货日期", platform_order: "平台单号", "lines.external_code": "客户商品编码", "lines.product_name": "商品名称", "lines.specification": "规格", "lines.quantity": "发货数量", "lines.per_carton": "每箱数量", "lines.batch": "批次", "lines.produced_at": "生产日期", "lines.expires_at": "到期日期", "lines.product_barcode": "商品条码", "lines.serial_numbers": "逐件序列号" };
const sourceKind: Record<string, string> = { pdf: "PDF", xlsx: "表格", csv: "表格", docx: "文档", image: "图片", png: "图片", jpg: "图片", jpeg: "图片", webp: "图片", text: "聊天原文" };
const displayValue = (value: unknown): string => value === null || value === undefined ? "" : Array.isArray(value) ? value.map(String).join("\n") : typeof value === "object" ? "请按原始资料补录" : String(value);
function sourceLocation(locator: Locator = {}) { return [locator.page ? `第 ${locator.page} 页` : "", locator.sheet ? `工作表「${locator.sheet}」` : "", locator.cell || (locator.row ? `第 ${locator.row} 行` : ""), locator.paragraph ? `第 ${locator.paragraph} 段` : "", locator.table ? `表 ${locator.table}` : ""].filter(Boolean).join(" · ") || "原始文本"; }
function citationLocation(source: Source | undefined, citation: Citation) {
  const chunk = source?.chunks.find(item => item.id === citation.chunk_id);
  const page = source?.pages.find(item => item.image_id === citation.image_id);
  return sourceLocation(page ? { ...citation.locator, ...chunk?.locator, page: page.page } : chunk?.locator || citation.locator);
}
function fieldInputValue(field: Field, value: string): unknown {
  const key = field.path.split(".").pop() || "";
  if (["quantity", "per_carton"].includes(key)) return value === "" ? null : Number(value);
  if (key.startsWith("require_")) return value === "true";
  if (key === "serial_numbers") return value.split(/[\n,，]/).map(item => item.trim()).filter(Boolean);
  return value;
}

export default function PackingSources({ token, userID, onApply }: { token: string; userID: string; onApply: (draft: PackingSourceDraft) => void | Promise<void> }) {
  const [intakes, setIntakes] = useState<IntakeSummary[]>([]);
  const [intake, setIntake] = useState<Intake | null>(null);
  const [catalog, setCatalog] = useState<CatalogProduct[]>([]);
  const [customer, setCustomer] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [role, setRole] = useState<"shipment" | "rule" | "packaging">("shipment");
  const [reason, setReason] = useState("按客户原始资料逐项核对");
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [draftCitations, setDraftCitations] = useState<Record<string, Citation[]>>({});
  const [skuChoices, setSKUChoices] = useState<Record<string, string>>({});
  const [skuChecks, setSKUChecks] = useState<Record<string, boolean>>({});
  const [skuConfirmations, setSKUConfirmations] = useState<Record<string, PackingSkuConfirmation>>({});
  const [saveMappings, setSaveMappings] = useState(true);
  const [ruleWidth, setRuleWidth] = useState(100);
  const [ruleHeight, setRuleHeight] = useState(150);
  const [ruleReviewed, setRuleReviewed] = useState(false);
  const [conflictsReviewed, setConflictsReviewed] = useState(false);
  const [extraRules, setExtraRules] = useState<string[]>([]);
  const [sourceID, setSourceID] = useState("");
  const [chunkID, setChunkID] = useState("");
  const [media, setMedia] = useState<(PackingPDF & { kind: "pdf" | "image" }) | null>(null);
  const [mediaLoading, setMediaLoading] = useState(false);
  const [imageCitation, setImageCitation] = useState<{ location: string; quote: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  const lock = useRef(false);
  const intakeRef = useRef(intake);
  const mediaGeneration = useRef(0);
  const mediaController = useRef<AbortController | null>(null);
  intakeRef.current = intake;
  const storageKey = `packing-intake-v2:${userID}`;
  const current = () => alive.current && getSession()?.token === token;

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    Promise.all([api<IntakeSummary[]>("/packing/intakes", { signal: controller.signal }), api<CatalogProduct[]>("/packing/catalog", { signal: controller.signal })]).then(async ([rows, products]) => {
      if (!current() || controller.signal.aborted) return;
      setIntakes(rows); setCatalog(products);
      const restored = packingStorage.get(storageKey);
      if (restored && rows.some(row => row.id === restored)) { const value = await api<Intake>(`/packing/intakes/${encodeURIComponent(restored)}`, { signal: controller.signal }); if (current() && !controller.signal.aborted) accept(value); }
    }).catch(failure => { if (current() && !controller.signal.aborted) setError(failure.message); });
    return () => { alive.current = false; controller.abort(); mediaGeneration.current += 1; mediaController.current?.abort(); };
  }, [token, storageKey]);
  useEffect(() => () => { if (media) URL.revokeObjectURL(media.url); }, [media]);

  function chooseSKU(lineID: string, skuID: string) {
    setSKUChoices(values => ({ ...values, [lineID]: skuID }));
    setSKUChecks(values => ({ ...values, [lineID]: false }));
    setSKUConfirmations(values => Object.fromEntries(Object.entries(values).filter(([key]) => key !== lineID)));
  }
  function skuState(line: SourceLine): PackingSkuState | undefined {
    const skuID = skuChoices[line.id];
    const sku = catalog.flatMap(product => product.skus).find(item => item.id === skuID);
    const candidate = line.candidates.find(item => item.sku_id === skuID);
    if (candidate?.packing_version !== undefined && candidate.packing_version >= (sku?.packing_version || 0)) return { sku_id: skuID, specification: candidate.specification, product_barcode: candidate.product_barcode || "", packing_enabled: candidate.packing_enabled !== false, version: candidate.packing_version };
    return sku ? { sku_id: skuID, specification: sku.label, product_barcode: sku.product_barcode, packing_enabled: sku.packing_enabled, version: sku.packing_version } : undefined;
  }
  function sourceBarcode(line: SourceLine) { return displayValue(intake?.fields.find(field => field.line_id === line.id && field.path.endsWith(".product_barcode"))?.value); }

  function accept(value: Intake) {
    if (!current()) return;
    clearMedia(); setChunkID("");
    setIntake(value); intakeRef.current = value; packingStorage.set(storageKey, value.id);
    setIntakes(rows => [value, ...rows.filter(row => row.id !== value.id)]);
    setDraftValues(Object.fromEntries(value.fields.map(field => [field.id, displayValue(field.value)])));
    setDraftCitations(Object.fromEntries(value.fields.map(field => [field.id, field.citations || []])));
    setSKUChoices(Object.fromEntries(value.lines.map(line => [line.id, line.sku_id || ""])));
    setSKUChecks({}); setSKUConfirmations({}); setRuleReviewed(false); setConflictsReviewed(false);
    setRuleWidth(value.rules.label_width_mm || 100); setRuleHeight(value.rules.label_height_mm || 150); setExtraRules(value.rules.required_fields || []);
    setSourceID(previous => value.sources.some(source => source.id === previous) ? previous : value.sources[0]?.id || "");
  }
  async function run(action: () => Promise<void>) {
    if (lock.current || !current()) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (failure) { if (current()) setError(failure instanceof Error ? failure.message : "资料暂时无法更新，请重试。"); }
    finally { lock.current = false; if (current()) setBusy(false); }
  }
  async function mutate(path: string, data: Record<string, unknown>) {
    const target = intakeRef.current;
    if (!target || !current()) throw new Error("请先选择资料单。");
    const value = await post<Intake>(`/packing/intakes/${target.id}/${path}`, { version: target.version, reason, request_id: requestID(), ...data });
    if (current()) accept(value);
    return value;
  }
  async function fileBlob(url: string, signal?: AbortSignal) {
    if (!current()) throw new Error("员工身份已切换，请重新打开资料。");
    const response = await fetch(url, { signal, headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : "原始资料暂时无法打开。"); }
    const blob = await response.blob();
    if (!current()) throw new Error("员工身份已切换，请重新打开资料。");
    return blob;
  }
  function clearMedia() {
    mediaGeneration.current += 1; mediaController.current?.abort(); mediaController.current = null;
    setMedia(null); setMediaLoading(false); setImageCitation(null);
  }
  function selectSource(source: Source, chunk = "") {
    clearMedia(); setSourceID(source.id); setChunkID(chunk);
    return mediaGeneration.current;
  }
  async function loadMedia(source: Source, url: string, kind: "pdf" | "image", name: string, generation: number) {
    const intakeID = intakeRef.current?.id;
    const controller = new AbortController(); mediaController.current = controller;
    const selected = () => current() && !controller.signal.aborted && mediaGeneration.current === generation && intakeRef.current?.id === intakeID;
    setMediaLoading(true); setError("");
    try {
      const blob = await fileBlob(url, controller.signal);
      if (selected()) setMedia({ url: URL.createObjectURL(blob), name, kind, pages: kind === "pdf" ? source.pages.map(page => ({ page: page.page, label: `原资料第 ${page.page} 页` })) : undefined });
    } catch (failure) {
      if (selected()) setError(failure instanceof Error ? failure.message : "所选原图暂时无法打开，请重试。");
    } finally {
      if (selected()) { setMediaLoading(false); mediaController.current = null; }
    }
  }
  function previewSource(source: Source) {
    const generation = selectSource(source);
    if (source.kind === "pdf" || ["image", "png", "jpg", "jpeg", "webp"].includes(source.kind)) {
      void loadMedia(source, source.url, source.kind === "pdf" ? "pdf" : "image", source.name, generation);
    }
  }
  function previewPage(source: Source, page: Source["pages"][number]) {
    const generation = selectSource(source);
    void loadMedia(source, page.url, "image", `${source.name} · 第${page.page}页`, generation);
  }
  async function downloadSource(source: Source) {
    const blob = await fileBlob(source.url); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = source.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function selectCitation(citation: Citation) {
    const source = intakeRef.current?.sources.find(item => item.id === citation.source_id);
    if (!source || !current()) return;
    const generation = selectSource(source, citation.chunk_id || "");
    const page = source.pages.find(item => item.image_id === citation.image_id);
    if (page) {
      setImageCitation({ location: citationLocation(source, citation), quote: citation.quote });
      void loadMedia(source, page.url, "image", `${source.name} · 第${page.page}页`, generation);
    }
  }
  function confirmField(field: Field) { return run(async () => { await mutate("fields/confirm", { changes: [{ field_id: field.id, value: fieldInputValue(field, draftValues[field.id] || ""), confirmed: true, citations: draftCitations[field.id] || [] }] }); setNotice(`已记录「${field.title}」的人工核对与理由。`); }); }
  const activeSource = intake?.sources.find(source => source.id === sourceID);
  const activeChunk = activeSource?.chunks.find(chunk => chunk.id === chunkID);
  const confirmedCount = intake?.fields.filter(field => field.confirmed).length || 0;
  const selectedFields = intake?.fields.filter(field => !field.path.startsWith("rules.")) || [];
  const rulesNeedDecision = !!intake && (intake.conflicts.length > 0 || intake.rules.candidates.some(candidate => candidate.field === "label_width_mm" ? candidate.value !== ruleWidth : candidate.field === "label_height_mm" ? candidate.value !== ruleHeight : candidate.field === "required_fields" && Array.isArray(candidate.value) ? JSON.stringify([...candidate.value].sort()) !== JSON.stringify([...extraRules].sort()) : false));

  return <section className="packing-sources">
    <div className="packing-section-title"><div><span className="packing-eyebrow">从资料到装箱</span><h3>客户资料与来源核对</h3></div><span>每个候选保留来源，人工确认后应用</span></div>
    <div className="packing-source-example"><a className="button" href="/packing-demo/箱唛演示资料包.zip" download>下载客户资料示例包</a><small>先上传25件采购单与箱唛要求，再按说明分次加入补充通知。</small></div><ErrorNote error={error} />{notice && <div className="packing-notice" role="status">{notice}</div>}
    <fieldset className="packing-fieldset" disabled={busy}>
      <div className="packing-source-selector"><label>继续核对已有资料<select value={intake?.id || ""} onChange={event => { const id = event.target.value; clearMedia(); setChunkID(""); if (id) void run(async () => { accept(await api<Intake>(`/packing/intakes/${encodeURIComponent(id)}`)); setMedia(null); }); else { intakeRef.current = null; setIntake(null); packingStorage.remove(storageKey); } }}><option value="">新建客户资料单</option>{intakes.map(row => <option key={row.id} value={row.id}>{row.customer} · {row.title} · {row.ready ? "已核对" : "待核对"}</option>)}</select></label><button className="button" onClick={() => { clearMedia(); setChunkID(""); intakeRef.current = null; setIntake(null); setCustomer(""); setTitle(""); packingStorage.remove(storageKey); }}><Plus size={16} />新资料单</button>{busy && <span className="packing-inline-status"><Spinner />正在处理</span>}</div>
      {!intake ? <div className="packing-source-create"><p>先填写客户，再归集本次订单、聊天原文及仓库要求。同一客户的外部商品编码按原值保留。</p><div className="packing-form-grid"><label>客户名称或客户编号<input value={customer} onChange={event => setCustomer(event.target.value)} placeholder="如：华东采购客户 A" maxLength={200} /></label><label>本次资料名称<input value={title} onChange={event => setTitle(event.target.value)} placeholder="如：十月补货与入仓要求" maxLength={200} /></label></div><button className="button dark" disabled={!customer.trim()} onClick={() => void run(async () => { accept(await post<Intake>("/packing/intakes", { customer: customer.trim(), title: title.trim() || `${customer.trim()}发货资料`, request_id: requestID() })); setNotice("资料单已建立，可以一次选择多份资料上传。"); })}>建立资料单</button></div> : <>
        <div className="packing-source-summary"><div><b>{intake.customer} · {intake.title}</b><small>第 {intake.version} 版 · {intake.sources.length} 份来源 · {confirmedCount} / {intake.fields.length} 字段已核对</small></div><span className={`packing-source-badge ${intake.ready ? "confirmed" : ""}`}>{intake.ready ? "资料已核对" : "待人工核对"}</span></div>
        <div className="packing-source-ingest"><div className="packing-form-grid"><label>这批资料的用途<select value={role} onChange={event => setRole(event.target.value as "shipment" | "rule" | "packaging")}><option value="shipment">客户发货内容</option><option value="rule">仓库或平台要求</option><option value="packaging">包装清单与编码资料</option></select></label><label>核对或补录理由<input value={reason} onChange={event => setReason(event.target.value)} maxLength={500} placeholder="冲突时写明采用哪份资料及原因" /></label></div><label className="packing-multi-upload"><Upload size={20} /><b>上传多份资料</b><span>订单表格、PDF、文档或图片；原文件留存供核对</span><input aria-label="上传多份客户资料" type="file" multiple accept=".xlsx,.csv,.docx,.pdf,.png,.jpg,.jpeg,.webp" disabled={!reason.trim()} onChange={event => { const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = ""; if (!files.length) return; void run(async () => { let target = intakeRef.current; for (const file of files) { if (!target || !current()) return; const upload = new FormData(); upload.append("file", file); upload.append("version", String(target.version)); upload.append("reason", reason); upload.append("request_id", requestID()); upload.append("source_role", role); target = await api<Intake>(`/packing/intakes/${target.id}/sources`, { method: "POST", body: upload }); if (current()) accept(target); } setNotice(`${files.length} 份资料已留存，请整理候选并逐项核对。`); }); }} /></label><details><summary>粘贴客户聊天或补充原文</summary><label>聊天原文<textarea value={text} onChange={event => setText(event.target.value)} placeholder="保留客户原话、商品编码及规格，不需要改写成标准格式。" /></label><button className="button" disabled={!text.trim() || !reason.trim()} onClick={() => void run(async () => { await mutate("texts", { text, name: role === "rule" ? "仓库要求原文" : "客户聊天原文", source_role: role }); setText(""); setNotice("聊天原文已留存。"); })}>留存这段原文</button></details><button className="button dark" disabled={!intake.sources.length || !reason.trim()} onClick={() => void run(async () => { await mutate("extract", {}); setNotice("资料整理已完成，请核对候选、缺项与来源报告。"); })}>整理资料候选</button><p className="packing-muted">候选用于辅助填写。多个值相互矛盾时，请选择依据并写明理由。</p></div>
        {intake.report && <div className="packing-source-report" role="status"><b>{intake.report.title}</b>{intake.report.messages.map((message, index) => <p key={index}>{message}</p>)}</div>}
        {(intake.conflicts.length > 0 || intake.missing.length > 0) && <div className="packing-source-issues"><h4>尚需核对</h4>{intake.conflicts.map((conflict, index) => <p key={`conflict-${index}`}>冲突：{conflict.title}</p>)}{intake.missing.map((message, index) => <p key={`missing-${index}`}>{message}</p>)}</div>}
        <div className="packing-source-review-grid">
          <section className="packing-source-fields"><div className="packing-section-title"><h4>字段与 SKU 核对</h4><span>{confirmedCount} 项已确认</span></div>
            {!selectedFields.length && <div className="packing-empty"><FileText size={25} /><p>上传或粘贴资料后，点击“整理资料候选”得到待核对字段。</p></div>}
            {selectedFields.map(field => { const edited = draftValues[field.id] !== displayValue(field.value); const hasRejection = !!field.active_rejections?.length; const booleanField = field.path.split(".").pop()?.startsWith("require_"); return <article className="packing-source-field" key={field.id}><div className="packing-source-field-heading"><b>{field.title}</b><span className={`packing-source-badge ${field.confirmed && !edited && !hasRejection ? "confirmed" : ""}`}>{field.confirmed && !edited && !hasRejection ? <><CheckCircle2 size={13} />已人工核对</> : edited ? "待确认修改" : field.origin === "ai" ? "候选待核对" : "待人工核对"}</span></div>{(field.rejected_candidates || []).filter(rejection => field.active_rejections?.includes(rejection.id)).map(rejection => <div className="packing-rejected-candidate" key={rejection.id}><b>需要人工更正的候选：{displayValue(rejection.value) || "留空"}</b><p>{rejection.error}</p><small>按原始资料填入有效值，或明确清空不适用的可选项，再写明理由确认。</small></div>)}{field.issue && !field.confirmed && <p className="packing-source-field-issue">{field.issue}</p>}{booleanField ? <label className="packing-check"><input type="checkbox" checked={draftValues[field.id] === "true"} onChange={event => setDraftValues(values => ({ ...values, [field.id]: String(event.target.checked) }))} />本次需要{field.title}</label> : <label><span className="sr-only">{field.title}确认值</span><textarea aria-label={`${field.title}确认值`} rows={field.path.endsWith("serial_numbers") ? 3 : 1} value={draftValues[field.id] || ""} onChange={event => { if (mediaLoading) clearMedia(); setDraftValues(values => ({ ...values, [field.id]: event.target.value })); setDraftCitations(values => ({ ...values, [field.id]: [] })); }} /></label>}{field.candidates.length > 1 && <div className="packing-field-candidates"><span>可选候选：</span>{field.candidates.map((candidate, index) => <button className="button" key={candidate.id || index} onClick={() => { clearMedia(); setDraftValues(values => ({ ...values, [field.id]: displayValue(candidate.value) })); setDraftCitations(values => ({ ...values, [field.id]: candidate.citations || [] })); }}>{displayValue(candidate.value) || "留空"}</button>)}</div>}<div className="packing-field-evidence">{(draftCitations[field.id]?.length ? draftCitations[field.id] : field.citations || []).map((citation, index) => { const source = intake.sources.find(item => item.id === citation.source_id); return <button key={index} onClick={() => selectCitation(citation)}><small>{source?.name || "原资料"} · {citationLocation(source, citation)}{citation.image_id ? " · 原图" : ""}</small><q>{citation.quote}</q></button>; })}{!(field.citations || []).length && <small>未识别到原文证据；补录时请填写核对理由。</small>}</div><button className="button" disabled={!reason.trim() || (field.confirmed && !edited && !hasRejection)} onClick={() => void confirmField(field)}>{field.confirmed && !edited && !hasRejection ? "已核对" : "确认此字段"}</button></article>; })}
            <button className="button" onClick={() => void run(async () => { const products = await api<CatalogProduct[]>("/packing/catalog"); if (current()) { setCatalog(products); setSKUChecks({}); setSKUConfirmations({}); setNotice("已读取最新 SKU 条码与装箱状态，请重新核对本单选择。"); } })}>刷新 SKU 条码与装箱状态</button>
            {intake.lines.map(line => <article className="packing-sku-review" key={line.id}>
              <div className="packing-source-field-heading"><h4>第 {line.index} 行 · 商品映射</h4><span className={`packing-source-badge ${["confirmed_mapping", "human_confirmed"].includes(line.mapping_status) ? "confirmed" : ""}`}>{["confirmed_mapping", "human_confirmed"].includes(line.mapping_status) ? "映射已确认" : line.mapping_status === "disabled_sku" ? "停用 SKU 需本单确认" : line.mapping_status === "barcode_conflict" ? "条码冲突需核对" : "规格需核对"}</span></div>
              {line.candidates.length > 0 && <div className="packing-sku-candidates">{line.candidates.map(candidate => <button className={`packing-sku-candidate ${skuChoices[line.id] === candidate.sku_id ? "active" : ""}`} key={candidate.sku_id} onClick={() => chooseSKU(line.id, candidate.sku_id)}><b>{candidate.product_name}</b><span>{candidate.specification}</span><small>店内编码：{candidate.sku_id}{candidate.barcode_match ? " · 条码匹配" : ""}{candidate.barcode_conflict ? " · 条码不同" : ""}{candidate.packing_enabled === false ? " · 停用装箱" : ""}</small></button>)}</div>}
              <label>选择店内准确规格<select aria-label={`第${line.index}行店内准确规格`} value={skuChoices[line.id] || ""} onChange={event => chooseSKU(line.id, event.target.value)}><option value="">请选择规格</option>{catalog.map(product => <optgroup label={product.name} key={product.id}>{product.skus.map(sku => <option value={sku.id} key={sku.id}>{product.name} · {sku.label} · {sku.id}{!sku.packing_enabled ? "（停用）" : ""}</option>)}</optgroup>)}</select></label>
              <PackingSkuReview state={skuState(line)} usedBarcode={sourceBarcode(line)} value={skuConfirmations[line.id]} label={`第${line.index}行`} onChange={value => setSKUConfirmations(values => ({ ...values, [line.id]: value }))} />
              <label className="packing-check"><input type="checkbox" checked={skuChecks[line.id] || false} onChange={event => setSKUChecks(values => ({ ...values, [line.id]: event.target.checked }))} />已核对客户编码、商品名称和完整规格</label>
              <button className="button dark" disabled={!skuChoices[line.id] || !skuChecks[line.id] || !reason.trim()} onClick={() => void run(async () => { const exception = skuNeedsReview(skuState(line), sourceBarcode(line)); await mutate("sku", { line_id: line.id, sku_id: skuChoices[line.id], specification_confirmed: true, save_mapping: saveMappings && !exception, sku_confirmation: skuConfirmations[line.id] }); setNotice(exception ? "已登记本单例外，未保存为自动复用映射。" : "本行 SKU 映射已确认；客户编码与原规格均保留。"); })}>确认这一行映射</button>
            </article>)}
            {intake.lines.length > 0 && <label className="packing-check"><input type="checkbox" checked={saveMappings} onChange={event => setSaveMappings(event.target.checked)} />保存此客户的编码与规格映射，供后续订单核对（本单例外不保存）</label>}
          </section>
          <aside className="packing-source-original"><h4>原资料对照</h4><div className="packing-original-files">{intake.sources.map(source => <button key={source.id} className={source.id === sourceID ? "active" : ""} onClick={() => selectSource(source)}><FileText size={15} /><span>{source.name}<small>{sourceKind[source.kind] || "资料"} · {source.role === "rule" ? "入仓要求" : source.role === "packaging" ? "包装与编码" : "发货内容"}</small></span></button>)}</div>{activeSource && <><div className="packing-toolbar"><button className="button" onClick={() => previewSource(activeSource)}>预览原文件</button><button className="button" onClick={() => void run(() => downloadSource(activeSource))}>下载原资料</button></div>{activeChunk && <div className="packing-selected-evidence"><b>当前字段引用 · {sourceLocation(activeChunk.locator)}</b><p>{activeChunk.text}</p></div>}<div className="packing-original-chunks">{activeSource.chunks.map(chunk => <details key={chunk.id} open={chunk.id === chunkID}><summary>{sourceLocation(chunk.locator)}</summary><p>{chunk.text}</p></details>)}</div>{imageCitation && !activeChunk && <div className="packing-selected-evidence"><b>当前字段引用 · {imageCitation.location}原图</b><p>{imageCitation.quote}</p><small>图像转录用于辅助填写，请对照这一页原图逐项人工核对。</small></div>}{activeSource.pages?.length > 0 && <div className="packing-original-pages">{activeSource.pages.map(page => <button key={page.image_id} className="button" onClick={() => previewPage(activeSource, page)}>查看原图第 {page.page} 页</button>)}</div>}</>}{mediaLoading && <p className="packing-notice" role="status"><Spinner />正在读取所选原资料…</p>}{media?.kind === "image" && <figure className="packing-source-image"><img src={media.url} alt={media.name} /><figcaption>{media.name}</figcaption></figure>}</aside>
        </div>
        <section className="packing-rules-review"><h4>仓库要求与标签尺寸</h4><p>以下核心字段必须核对，不能从箱唛中隐藏。补充要求按原资料选择。</p><div className="packing-rules-sources">{intake.rules.candidates.map((candidate, index) => <div key={candidate.id || index}><b>{candidate.field === "label_width_mm" ? "标签宽度候选" : candidate.field === "label_height_mm" ? "标签高度候选" : "必填要求候选"}：{candidate.field === "required_fields" && Array.isArray(candidate.value) ? candidate.value.map(value => ruleNames[String(value)] || "补充要求").join("、") : displayValue(candidate.value)}</b>{candidate.citations?.map((citation, citationIndex) => <button key={citationIndex} onClick={() => selectCitation(citation)}>{intake.sources.find(source => source.id === citation.source_id)?.name || "原资料"} · {citationLocation(intake.sources.find(source => source.id === citation.source_id), citation)} · {citation.quote}</button>)}</div>)}{intake.fields.filter(field => field.path.startsWith("rules.")).flatMap(field => (field.rejected_candidates || []).filter(rejection => field.active_rejections?.includes(rejection.id)).map(rejection => <div className="packing-rejected-candidate" key={rejection.id}><b>{field.title}候选需更正：{displayValue(rejection.value)}</b><p>{rejection.error}</p></div>))}</div><div className="packing-rule-options">{Object.entries(ruleNames).map(([key, name]) => { const required = ["supplier", "destination", "ship_date", "lines.external_code", "lines.specification", "lines.quantity", "lines.per_carton"].includes(key); return <label className="packing-check" key={key}><input type="checkbox" checked={required || extraRules.includes(key)} disabled={required} onChange={event => setExtraRules(values => event.target.checked ? [...values, key] : values.filter(item => item !== key))} />{name}{required ? "（必填）" : ""}</label>; })}</div><div className="packing-form-grid"><label>标签宽度 / 毫米<input type="number" min="100" max="200" value={ruleWidth} onChange={event => { setRuleWidth(Number(event.target.value)); setRuleReviewed(false); }} /></label><label>标签高度 / 毫米<input type="number" min="150" max="270" value={ruleHeight} onChange={event => { setRuleHeight(Number(event.target.value)); setRuleReviewed(false); }} /></label></div><label className="packing-check"><input type="checkbox" checked={ruleReviewed} onChange={event => setRuleReviewed(event.target.checked)} />已按实际仓库要求核对必填项及标签尺寸</label>{rulesNeedDecision && <label className="packing-check"><input type="checkbox" checked={conflictsReviewed} onChange={event => setConflictsReviewed(event.target.checked)} />已逐项处理冲突，核对理由说明了最终采用的依据</label>}<button className="button" disabled={!ruleReviewed || !reason.trim() || (rulesNeedDecision && !conflictsReviewed)} onClick={() => void run(async () => { await mutate("rules/confirm", { confirmed: true, required_fields: extraRules, label_width_mm: ruleWidth, label_height_mm: ruleHeight, resolve_conflicts: conflictsReviewed }); setNotice("本次入仓要求与尺寸已确认。"); })}>确认本次仓库要求</button><div className="packing-source-apply"><span>{intake.ready ? "字段、SKU 与要求均已人工核对，可继续核对分箱。" : "完成字段、SKU 和要求核对后，可应用到装箱表单。"}</span><button className="button dark" disabled={!intake.ready} onClick={() => void run(async () => { const value = await post<PackingSourceDraft>(`/packing/intakes/${intake.id}/draft`, { version: intake.version, request_id: requestID() }); if (current()) await onApply(value); })}>应用资料并核对分箱</button></div></section>
        <details className="packing-block"><summary>资料修改与确认记录</summary><ol className="packing-history">{intake.history.slice().reverse().map((entry, index) => <li key={index}><b>{entry.action}</b><small>第 {entry.version} 版 · {entry.operator} · {date(entry.at, true)}</small><p>{entry.reason}</p></li>)}</ol></details>
      </>}
    </fieldset>
    {media?.kind === "pdf" && <Suspense fallback={<div className="packing-notice"><Spinner />正在打开原始 PDF</div>}><PackingViewer document={media} onClose={clearMedia} /></Suspense>}
  </section>;
}
