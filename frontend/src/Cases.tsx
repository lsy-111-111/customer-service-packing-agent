import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronRight,
  Clock3,
  PackageCheck,
  RotateCcw,
  Truck,
} from "lucide-react";
import type { Assessment, AssessmentTask, Case, CaseStatus, CaseType, Order, Product, ResourceState, ServiceRequest } from "./types";
import { api, date, getSession, money, patch, post, requestID } from "./api";
import { Empty, ErrorNote, Modal, Photo, ResourceNotice, Spinner, Uploader } from "./ui";
import { CustomerSupport, FeedbackPanel, QualityTags } from "./Support";
export const statusLabels: Record<CaseStatus, string> = {
  draft: "待确认",
  submitted: "待审核",
  needs_info: "待补材料",
  approved: "已同意待处理",
  rejected: "已拒绝",
  completed: "处理完成",
};
export const typeLabels: Record<CaseType, string> = {
  return_refund: "退货退款",
  exchange: "换货",
  reship: "补发",
};
const shippingLabels = {
  customer: "顾客承担",
  store: "店铺承担",
  pending: "待人工核实",
};
export function CaseBadge({ value }: { value: CaseStatus }) {
  return (
    <span
      className={`badge ${value === "completed" ? "success" : value === "rejected" ? "danger" : value === "needs_info" ? "warning" : value === "draft" ? "neutral" : "olive"}`}
    >
      {statusLabels[value]}
    </span>
  );
}
export function CaseCard({
  item,
  onClick,
}: {
  item: Case;
  onClick: () => void;
}) {
  return (
    <button className="case-card" onClick={onClick}>
      <span className="case-mark">
        <CheckCircle2 size={23} />
      </span>
      <span>
        <b>
          {typeLabels[item.type]}申请
          {item.status === "draft"
            ? "待确认"
            : item.status === "submitted"
              ? "已提交"
              : ""}
        </b>
        <small>
          {item.stale_since_message_id ? "顾客已更正，申请待核对" : statusLabels[item.status]} ·{" "}
          {item.order?.product_name || item.order_id}
        </small>
      </span>
      <ChevronRight size={18} />
    </button>
  );
}
export function CasesDrawer({
  items,
  serviceRequests = [],
  orders = [],
  conversationID = "",
  onUpdated = () => {},
  onClose,
  onSelect,
  loadState,
  requestState,
  orderState,
  onRetry,
  onRetryRequests,
  onRetryOrders,
}: {
  items: Case[];
  serviceRequests?: ServiceRequest[];
  orders?: Order[];
  conversationID?: string;
  onUpdated?: () => void;
  onClose: () => void;
  onSelect: (c: Case) => void;
  loadState?: ResourceState;
  requestState?: ResourceState;
  orderState?: ResourceState;
  onRetry?: () => void;
  onRetryRequests?: () => void;
  onRetryOrders?: () => void;
}) {
  return (
    <Modal
      title="我的售后"
      subtitle="每一步处理进度，都可以在这里查看"
      onClose={onClose}
    >
      <div className="drawer-body">
        <ResourceNotice label="售后申请" state={loadState} onRetry={onRetry} />
        {items.length ? (
          items.map((c) => (
            <article className="order-card" key={c.id}>
              <div className="row-between">
                <small>{c.id}</small>
                <CaseBadge value={c.status} />
              </div>
              <div className="order-product">
                <img src={c.order.image} alt={c.order.product_name} />
                <div>
                  <h3>{c.order.product_name}</h3>
                  <p>
                    {typeLabels[c.type]} · {c.quantity} 件
                  </p>
                  <small>{c.reason}</small>
                </div>
              </div>
              <div className="row-between">
                <span className="muted small">
                  {date(c.updated_at, true)} 更新
                </span>
                <button className="text-link" onClick={() => onSelect(c)}>
                  {c.status === "draft"
                    ? "确认申请"
                    : c.status === "needs_info"
                      ? "补充材料"
                      : "查看进度"}
                  <ChevronRight size={17} />
                </button>
              </div>
            </article>
          ))
        ) : (!loadState || (loadState.loaded && !loadState.error)) ? (
          <Empty title="暂时没有售后申请">
            在订单中选择“申请售后”，也可以直接告诉客服。
          </Empty>
        ) : null}
        <ResourceNotice label="人工受理" state={requestState} onRetry={onRetryRequests} />
        <ResourceNotice label="订单" state={orderState} onRetry={onRetryOrders} />
        {conversationID && (!requestState || requestState.loaded) && <CustomerSupport conversationID={conversationID} requests={serviceRequests} orders={orders} onUpdated={onUpdated} />}
      </div>
    </Modal>
  );
}
export function CaseModal({
  item,
  order,
  conversationID,
  products,
  onClose,
  onSaved,
}: {
  item?: Case;
  order: Order;
  conversationID: string;
  products: Product[];
  onClose: () => void;
  onSaved: (c: Case) => void;
}) {
  const [current, setCurrent] = useState<Case | undefined>(item);
  const [editing, setEditing] = useState(!item);
  const [type, setType] = useState<CaseType>(item?.type || "return_refund");
  const [quantity, setQuantity] = useState(item?.quantity || 1);
  const [reason, setReason] = useState(item?.reason || "");
  const [condition, setCondition] = useState(item?.condition_note || "");
  const [target, setTarget] = useState(item?.target_sku_id || order.sku_id);
  const [reshipKind, setReshipKind] = useState<"whole" | "part">(item?.reship_kind || "whole");
  const [partID, setPartID] = useState(item?.part_sku_id || "");
  const [assessmentTask, setAssessmentTask] = useState<AssessmentTask | null>(() => {
    try { return item ? JSON.parse(sessionStorage.getItem(`daily-assessment-${getSession()?.user.id}-${item.id}`) || "null") : null; } catch { return null; }
  });
  const assessmentRequest = useRef("");
  const [attachments, setAttachments] = useState<string[]>(
    item?.attachment_ids || [],
  );
  const [supplement, setSupplement] = useState("");
  const [supplementImages, setSupplementImages] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const createID = useRef(requestID());
  const actions = useRef<Record<string, string>>({});
  const product = products.find((p) => p.id === order.product_id);
  const packageParts = (order.package_snapshot || []).filter((part) => part.kind === "part" && part.part_sku_id);
  const selectedPart = product?.parts?.find((part) => part.id === partID);
  const quantityLimit = type === "reship" && reshipKind === "part"
    ? Math.max(1, (packageParts.find((part) => part.part_sku_id === partID)?.quantity || 1) * order.quantity)
    : Math.max(order.remaining_quantity, 1);
  const originalPrice =
    product?.skus.find((s) => s.id === order.sku_id)?.price_cents ??
    order.unit_price_cents;
  const allowedSKUs =
    product?.skus.filter((s) => s.price_cents === originalPrice) || [];
  useEffect(() => {
    if (item && (!current || item.version > current.version || item.assessment?.evaluated_at !== current.assessment?.evaluated_at)) {
      setCurrent(item);
      if (editing)
        setError(
          "申请已被更新，保存时会校验最新版本。你的输入仍保留，请核对新的方案。",
        );
    }
  }, [item?.version, item?.assessment?.evaluated_at]);
  useEffect(() => {
    if (assessmentTask?.case_id && (assessmentTask.id || assessmentTask.status === "failed")) sessionStorage.setItem(`daily-assessment-${getSession()?.user.id}-${assessmentTask.case_id}`, JSON.stringify(assessmentTask));
  }, [assessmentTask]);
  useEffect(() => {
    if (!assessmentTask?.id || assessmentTask.status !== "pending") return;
    let live = true;
    let inFlight = false;
    const tick = async () => {
      if (inFlight || !live) return;
      inFlight = true;
      try {
        const next = await api<AssessmentTask>(`/assessments/${assessmentTask.id}`);
        if (!live) return;
        if (next.status === "completed" || next.status === "superseded") {
          const fresh = (await api<Case[]>("/cases")).find((c) => c.id === next.case_id);
          if (live && fresh) { setCurrent(fresh); onSaved(fresh); }
        }
        if (live) setAssessmentTask(next);
      } catch (e) {
        if (live) setError((e as Error).message);
      } finally { inFlight = false; }
    };
    void tick();
    const timer = setInterval(() => void tick(), 1200);
    return () => { live = false; clearInterval(timer); };
  }, [assessmentTask?.id, assessmentTask?.status]);
  async function assess(c: Case, freshRequest = false) {
    if (freshRequest || !assessmentRequest.current) assessmentRequest.current = requestID();
    setAssessmentTask({ id: "", status: "pending", assessment: null, error: null, case_id: c.id, created_at: new Date().toISOString() });
    try {
      const task = await post<AssessmentTask>("/assessments", {
        conversation_id: c.conversation_id, order_id: c.order_id,
        type: c.type, quantity: c.quantity, reason: c.reason,
        condition_note: c.condition_note, target_sku_id: c.target_sku_id,
        reship_kind: c.reship_kind || "whole", part_sku_id: c.part_sku_id,
        attachment_ids: c.attachment_ids, case_id: c.id, case_version: c.version,
        request_id: assessmentRequest.current,
      });
      setAssessmentTask(task);
      if (task.status === "completed") {
        const fresh = (await api<Case[]>("/cases")).find((item) => item.id === c.id);
        if (fresh) { setCurrent(fresh); onSaved(fresh); }
      }
    } catch (e) {
      setAssessmentTask({ id: "", status: "failed", assessment: null, error: (e as Error).message, case_id: c.id, created_at: new Date().toISOString() });
    }
  }
  function id(action: string) {
    const k = `${current?.id || "new"}-${current?.version || 0}-${action}`;
    return actions.current[k] || (actions.current[k] = requestID());
  }
  async function saveDraft() {
    if (!reason.trim()) {
      setError("请告诉我们遇到的问题或申请原因");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const fields = {
        conversation_id: conversationID,
        order_id: order.id,
        type,
        quantity,
        reason,
        condition_note: condition,
        target_sku_id: type === "return_refund" || (type === "reship" && reshipKind === "part") ? null : target,
        reship_kind: type === "reship" ? reshipKind : "whole",
        part_sku_id: type === "reship" && reshipKind === "part" ? partID : null,
        attachment_ids: attachments,
      };
      const c = current
        ? await patch<Case>(`/cases/${current.id}`, {
            ...fields,
            expected_version: current.version,
          })
        : await post<Case>("/cases", {
            ...fields,
            request_id: createID.current,
          });
      setCurrent(c);
      setEditing(false);
      onSaved(c);
      await assess(c, true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function submit() {
    if (!current) return;
    setBusy(true);
    setError("");
    try {
      const c = await post<Case>(`/cases/${current.id}/submit`, {
        expected_version: current.version,
        request_id: id("submit"),
      });
      setCurrent(c);
      onSaved(c);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function sendSupplement() {
    if (!current) return;
    if (!supplement.trim() && !supplementImages.length) {
      setError("请填写补充说明或上传图片");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const c = await post<Case>(`/cases/${current.id}/supplements`, {
        content: supplement,
        attachment_ids: supplementImages,
        expected_version: current.version,
        request_id: id("supplements"),
      });
      setCurrent(c);
      setSupplement("");
      setSupplementImages([]);
      onSaved(c);
      await assess(c, true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={editing ? "申请售后" : "售后详情"}
      subtitle="我们先了解情况，最终处理由售后同事确认"
      onClose={() => { if (!busy) onClose(); }}
    >
      <div className="drawer-body">
        <div className="order-product compact">
          <img src={order.image} alt={order.product_name} />
          <div>
            <h3>{order.product_name}</h3>
            <p>
              {order.sku_label} · 已购 {order.quantity} 件
            </p>
            <small>{order.id}</small>
          </div>
        </div>
        {editing ? (
          <>
            <div className="form-field">
              <label>你希望怎样处理</label>
              <div className="segmented case-types">
                {(Object.keys(typeLabels) as CaseType[]).map((t) => (
                  <button
                    className={type === t ? "active" : ""}
                    key={t}
                    onClick={() => { setType(t); setQuantity(1); }}
                  >
                    {typeLabels[t]}
                  </button>
                ))}
              </div>
            </div>
            {type === "reship" && (
              <div className="form-field">
                <label>补发对象</label>
                <div className="segmented">
                  <button className={reshipKind === "whole" ? "active" : ""} onClick={() => { setReshipKind("whole"); setQuantity(1); }}>整件商品</button>
                  <button className={reshipKind === "part" ? "active" : ""} disabled={!packageParts.length} onClick={() => { setReshipKind("part"); setPartID(partID || packageParts[0]?.part_sku_id || ""); setQuantity(1); }}>随单配件</button>
                </div>
                {!packageParts.length && <small className="muted">购买时包装清单没有可单独补发的配件，可咨询客服核实。</small>}
              </div>
            )}
            <div className="form-grid">
              <label className="form-field">
                {type === "reship" && reshipKind === "part" ? "配件数量" : "申请数量"}
                <input
                  type="number"
                  min={1}
                  max={quantityLimit}
                  value={quantity}
                  onChange={(e) =>
                    setQuantity(
                      Math.min(
                        Math.max(1, Number(e.target.value) || 1),
                        quantityLimit,
                      ),
                    )
                  }
                />
              </label>
              {type === "reship" && reshipKind === "part" ? (
                <label className="form-field">
                  补发配件
                  <select aria-label="补发配件" value={partID} onChange={(e) => { setPartID(e.target.value); setQuantity(1); }}>
                    {packageParts.map((part) => <option key={part.id} value={part.part_sku_id}>{part.name}</option>)}
                  </select>
                  <small className="muted">独立配件库存：{selectedPart?.stock ?? "待核实"}；不占用整件退货数量。</small>
                </label>
              ) : type !== "return_refund" && (
                <label className="form-field">
                  {type === "exchange" ? "目标规格" : "补发规格"}
                  <select
                    aria-label={type === "exchange" ? "目标规格" : "补发规格"}
                    value={target}
                    onChange={(e) => setTarget(e.target.value)}
                  >
                    {allowedSKUs.map((s) => (
                      <option value={s.id} key={s.id}>
                        {s.label}
                        {s.stock === 0 ? "（暂缺）" : ""}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <label className="form-field">
              申请原因
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="例如：杯口有缺口；尺码偏小，希望换大一码"
                rows={3}
                maxLength={2000}
              />
            </label>
            <label className="form-field">
              商品与包装情况
              <textarea
                value={condition}
                onChange={(e) => setCondition(e.target.value)}
                placeholder="补充使用、拆封或包装情况（选填）"
                rows={2}
              />
            </label>
            <div className="form-field">
              <label>
                相关图片 <span className="muted">选填，最多 3 张</span>
              </label>
              <div className="attachment-field">
                <Uploader
                  ids={attachments}
                  onChange={setAttachments}
                  onError={setError}
                  disabled={busy}
                />
              </div>
            </div>
            <p className="muted small">
              下一步会展示申请摘要、规则依据和费用承担方，确认后才提交人工审核。
            </p>
            <ErrorNote error={error} />
            <button
              className="button primary full"
              disabled={busy}
              onClick={() => void saveDraft()}
            >
              {busy ? <Spinner /> : null}查看申请摘要
              <ChevronRight size={17} />
            </button>
          </>
        ) : current ? (
          <>
            <div className="case-status-heading">
              <CaseBadge value={current.status} />
              <span className="muted small">{current.id}</span>
            </div>
            {assessmentTask?.status === "pending" ? (
              <div className="assessment-progress" role="status"><Spinner /><div><b>正在核对售后建议</b><p>核对订单、申请事实与规则，完成后展示明确建议。</p></div></div>
            ) : assessmentTask?.status === "failed" ? (
              <div className="assessment-progress failed" role="alert"><div><b>建议暂未生成，申请草稿已保留</b><p>{assessmentTask.error}</p><button className="text-link" onClick={() => void assess(current, true)}>重新核对</button></div></div>
            ) : null}
            <CaseSummary item={current} hideAssessment={Boolean(current.stale_since_message_id) || assessmentTask?.status === "pending" || assessmentTask?.status === "failed"} />
            {current.stale_since_message_id && <div className="notice-box" role="alert">顾客已补充或更正诉求，原申请摘要待更新。请核对新事实后重新确认。</div>}
            {current.status === "draft" && (
              <>
                <div className="notice-box">
                  请确认下方申请内容。提交后由人工审核，AI
                  建议不代表已同意退款或换货。
                </div>
                <ErrorNote error={error} />
                <button
                  className="button primary full"
                  disabled={busy || Boolean(current.stale_since_message_id) || assessmentTask?.status === "pending" || assessmentTask?.status === "failed"}
                  onClick={() => void submit()}
                >
                  {busy ? <Spinner /> : <CheckCircle2 size={18} />}{current.assessment?.decision === "reject" ? "提交人工复核" : "确认提交申请"}
                </button>
                <button
                  className="button full"
                  disabled={busy}
                  onClick={() => setEditing(true)}
                >
                  修改申请
                </button>
              </>
            )}
            {current.status === "needs_info" && (
              <>
                <label className="form-field">
                  补充说明
                  <textarea
                    rows={4}
                    value={supplement}
                    onChange={(e) => setSupplement(e.target.value)}
                    placeholder="根据售后同事的说明，补充商品情况或照片"
                  />
                </label>
                <div className="attachment-field">
                  <Uploader
                    ids={supplementImages}
                    onChange={setSupplementImages}
                    onError={setError}
                    disabled={busy}
                  />
                </div>
                <ErrorNote error={error} />
                <button
                  className="button primary full"
                  onClick={() => void sendSupplement()}
                  disabled={busy}
                >
                  {busy ? <Spinner /> : null}提交补充材料
                </button>
              </>
            )}
            {!["draft", "needs_info"].includes(current.status) && (
              <div className="notice-box">
                <Clock3 size={18} />
                <span>
                  {current.status === "approved"
                    ? "申请已同意，等待人工处理；当前尚未完成退款或换货。"
                    : current.status === "completed"
                      ? "售后同事已记录演示处理完成，本次操作不会产生真实退款或物流。"
                      : current.status === "rejected"
                        ? "售后同事已提供处理说明。如有补充情况，可以继续联系店铺客服。"
                        : "申请已经提交，售后同事会核实材料并给出处理结果。"}
                </span>
              </div>
            )}
            <CaseHistory item={current} />
            {current.status === "completed" && <FeedbackPanel targetType="case" targetID={current.id} onUpdated={() => onSaved(current)} />}
          </>
        ) : null}
      </div>
    </Modal>
  );
}
export function CaseSummary({
  item,
  compact = false,
  hideAssessment = false,
}: {
  item: Case;
  compact?: boolean;
  hideAssessment?: boolean;
}) {
  return (
    <>
      <dl className="case-facts">
        <div>
          <dt>顾客诉求</dt>
          <dd>
            {typeLabels[item.type]}{item.type === "reship" ? (item.reship_kind === "part" ? "配件" : "整件") : ""} {item.quantity} 件
          </dd>
        </div>
        <div>
          <dt>申请原因</dt>
          <dd>{item.reason}</dd>
        </div>
        {!compact && item.condition_note && (
          <div>
            <dt>商品情况</dt>
            <dd>{item.condition_note}</dd>
          </div>
        )}
        {item.type === "return_refund" ? (
          <div>
            <dt>预计退款</dt>
            <dd>
              ¥ {money(item.refund_cents)} <small>以人工审核为准</small>
            </dd>
          </div>
        ) : (
          <div>
            <dt>{item.reship_kind === "part" ? "补发配件" : "目标规格"}</dt>
            <dd>{item.reship_kind === "part" ? (item.order.package_snapshot?.find((part) => part.part_sku_id === item.part_sku_id)?.name || item.part_sku_id || "待确认") : item.target_sku?.label || item.target_sku_id || "待确认"}</dd>
          </div>
        )}
        <div>
          <dt>运费承担</dt>
          <dd>{shippingLabels[item.shipping_payer]}</dd>
        </div>
        {!compact && (
          <div>
            <dt>首次申请</dt>
            <dd>{date(item.first_notice_at, true)}</dd>
          </div>
        )}
      </dl>
      {compact && (
        <details className="case-more">
          <summary>商品情况与申请时间</summary>
          <p>{item.condition_note || "未补充商品情况"}</p>
          <p>首次申请：{date(item.first_notice_at, true)}</p>
        </details>
      )}
      {item.attachment_ids?.length > 0 && (
        <div className="case-photos">
          {item.attachment_ids.map((id) => (
            <Photo id={id} key={id} />
          ))}
        </div>
      )}
      {!hideAssessment && <AssessmentSummary item={item} />}
      {item.review_note && (
        <div className="review-note">
          <h4>售后同事的说明</h4>
          <p>{item.review_note}</p>
        </div>
      )}
      {item.completion_note && (
        <div className="review-note">
          <h4>处理结果</h4>
          <p>{item.completion_note}</p>
        </div>
      )}
    </>
  );
}
function AssessmentSummary({ item }: { item: Case }) {
  const assessment = item.assessment;
  if (!assessment) return <div className="suggestion-box"><h4>{["approved", "rejected", "completed"].includes(item.status) ? "历史处理说明" : "待核对建议"}</h4><p>{item.suggestion || "正在整理申请事实，最终审核由人工完成。"}</p><small>历史申请保留原处理说明。</small></div>;
  return <AssessmentPanel assessment={assessment} policyBasis={item.policy_basis} />;
}
export function AssessmentPanel({ assessment, policyBasis = [] }: { assessment: Assessment; policyBasis?: string[] }) {
  const labels = { approve: "建议同意", reject: "建议拒绝", needs_info: "待补充信息" };
  return <section className={`suggestion-box assessment-${assessment.decision}`} aria-label="售后建议">
    <div className="assessment-heading"><span className={`badge ${assessment.decision === "reject" ? "danger" : assessment.decision === "approve" ? "success" : "warning"}`}>{labels[assessment.decision]}</span><small>规则建议 · 非人工审批结果</small></div>
    <h4>{assessment.headline || labels[assessment.decision]}</h4>
    <ul className="assessment-reasons">{assessment.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
    {assessment.questions.length > 0 && <div className="assessment-next"><b>请补充</b><ul>{assessment.questions.map((question, i) => <li key={i}>{question}</li>)}</ul></div>}
    {assessment.fulfillment?.status === "out_of_stock" && <p className="notice-box">资格建议不受缺货影响。当前库存不足，补货时间或替代方案由人工确认。</p>}
    {assessment.next_steps.length > 0 && <div className="assessment-next"><b>下一步</b><ul>{assessment.next_steps.map((step, i) => <li key={i}>{step}</li>)}</ul></div>}
    <details className="policy-basis"><summary>查看规则与已核对事实</summary>
      <p>规则版本 {assessment.policy_version} · 订单原版本 {assessment.order_policy_version}</p>
      <ul>{(policyBasis.length ? policyBasis : assessment.policy_basis?.length ? assessment.policy_basis : assessment.rule_ids).map((rule, i) => <li key={i}>{rule}</li>)}</ul>
      <dl className="assessment-facts">{Object.entries(assessment.facts_used || {}).map(([key, value]) => <div key={key}><dt>{factLabels[key] || key}</dt><dd>{formatFact(value, key)}</dd></div>)}</dl>
    </details>
    <small>这是针对本次诉求的建议，最终审核与处理由人工完成。对拒绝建议仍可申请人工复核。</small>
  </section>;
}
const factLabels: Record<string, string> = { cause: "问题原因", condition: "商品情况", responsibility: "责任", issue_type: "问题类型", issue: "问题类型", intact: "是否完好", opened: "是否拆封", used: "是否使用", verified: "是否核实", quantity: "数量", elapsed_days: "申请距签收天数", first_notice_at: "首次提出时间", delivered_at: "签收时间", source: "来源", value: "事实", quote: "原始说明", category: "商品类别", order_status: "订单状态", defect_on_arrival: "到货时已异常", accessories_complete: "配件是否齐全", normal_use: "是否正常使用", repair_failed: "维修后仍故障", mismatch_resolved: "已澄清商品与诉求", missing_quantity: "缺少数量", received_quantity: "实收数量", part_name: "配件名称" };
const factValues: Record<string, string> = { customer: "顾客陈述", customer_statement: "顾客陈述", system: "系统记录", system_record: "系统记录", staff: "人工核实", staff_verified: "人工核实", unknown: "待核实", quality: "质量问题", no_reason: "无理由", seller: "卖家责任", customer_damage: "人为损坏", missing: "少发", wrong_item: "错发", damaged: "破损", damage: "破损", intact: "完好", inspection_only: "仅合理拆封查验", used: "已使用", model_extraction: "从顾客陈述整理", user: "顾客陈述" };
function formatFact(value: unknown, field?: string): string {
  if (value === null || value === undefined) return "待核实";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object" && !Array.isArray(value)) {
    const fact = value as Record<string, unknown>;
    if ("value" in fact) return `${formatFact(fact.value, field)}${fact.source ? `（${formatFact(fact.source, "source")}）` : ""}${fact.quote ? `：${fact.quote}` : ""}`;
    return Object.entries(fact).map(([key, val]) => `${factLabels[key] || key}：${formatFact(val, key)}`).join("；");
  }
  if (Array.isArray(value)) return value.map((entry) => formatFact(entry, field)).join("、");
  if (field === "responsibility") return ({ seller: "卖家责任", customer: "顾客责任", unknown: "待核实" } as Record<string, string>)[String(value)] || String(value);
  return factValues[String(value)] || String(value);
}
function CaseHistory({ item }: { item: Case }) {
  return item.history?.length ? (
    <div className="case-history">
      <h3>处理记录</h3>
      {item.history.map((h, i) => (
        <div key={i}>
          <span className="timeline-dot" />
          <p>
            {String(
              h.note || h.content || h.action || h.status || "申请已更新",
            )}
            <small>{date(String(h.created_at || h.at || ""), true)}</small>
          </p>
        </div>
      ))}
    </div>
  ) : null;
}
export function StaffInspector({
  item,
  items,
  products,
  onSelect,
  onUpdated,
}: {
  item: Case | undefined;
  items: Case[];
  products: Product[];
  onSelect: (c: Case) => void;
  onUpdated: () => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [type, setType] = useState<CaseType>(item?.type || "return_refund");
  const [quantity, setQuantity] = useState(item?.quantity || 1);
  const [target, setTarget] = useState(item?.target_sku_id || "");
  const [reshipKind, setReshipKind] = useState<"whole" | "part">(item?.reship_kind || "whole");
  const [partID, setPartID] = useState(item?.part_sku_id || "");
  const [returnHandling, setReturnHandling] = useState(item?.return_handling || "");
  const [returnNote, setReturnNote] = useState(item?.return_note || "");
  const [payer, setPayer] = useState(item?.shipping_payer || "pending");
  const ids = useRef<Record<string, string>>({});
  if (!item)
    return (
      <aside className="staff-inspector">
        <h2>售后申请</h2>
        <Empty title="当前没有售后申请">
          在对话中了解顾客需求，申请提交后会在这里显示。
        </Empty>
      </aside>
    );
  const sku = products
    .flatMap((p) => p.skus)
    .find((s) => s.id === item.target_sku_id);
  const sourceProduct = products.find((p) => p.id === item.order.product_id);
  const packageParts = (item.order.package_snapshot || []).filter((part) => part.kind === "part" && part.part_sku_id);
  const partSKU = sourceProduct?.parts?.find((part) => part.id === item.part_sku_id);
  const sourcePrice =
    sourceProduct?.skus.find((s) => s.id === item.order.sku_id)?.price_cents ??
    item.order.unit_price_cents;
  const options =
    sourceProduct?.skus.filter((s) => s.price_cents === sourcePrice) || [];
  function id(action: string) {
    const k = `${item!.id}-${item!.version}-${action}`;
    return ids.current[k] || (ids.current[k] = requestID());
  }
  async function action(
    decision: "approve" | "reject" | "needs_info" | "complete",
  ) {
    if (!item) return;
    if (!note.trim()) {
      setError("请填写顾客可见的处理说明");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (decision === "complete")
        await post(`/cases/${item.id}/complete`, {
          note,
          expected_version: item.version,
          request_id: id(decision),
          ...(returnHandling ? { return_handling: returnHandling, return_note: returnNote } : {}),
        });
      else
        await post(`/cases/${item.id}/review`, {
          decision,
          note,
          expected_version: item.version,
          request_id: id(decision),
          ...(returnHandling ? { return_handling: returnHandling, return_note: returnNote } : {}),
        });
      setNote("");
      onUpdated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function changeProposal() {
    if (!item) return;
    setBusy(true);
    setError("");
    try {
      await patch(`/cases/${item.id}`, {
        conversation_id: item.conversation_id,
        order_id: item.order_id,
        type,
        quantity,
        target_sku_id: type === "return_refund" || (type === "reship" && reshipKind === "part") ? null : target,
        reship_kind: type === "reship" ? reshipKind : "whole",
        part_sku_id: type === "reship" && reshipKind === "part" ? partID : null,
        reason: item.reason,
        condition_note: item.condition_note,
        attachment_ids: item.attachment_ids,
        shipping_payer: payer,
        expected_version: item.version,
      });
      setEditing(false);
      onUpdated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <aside className="staff-inspector">
      <div className="row-between">
        <h2>售后申请</h2>
        <CaseBadge value={item.status} />
      </div>
      {items.length > 1 && (
        <select
          aria-label="选择售后申请"
          className="case-selector"
          value={item.id}
          onChange={(e) => {
            const c = items.find((v) => v.id === e.target.value);
            if (c) {
              onSelect(c);
              setNote("");
              setEditing(false);
            }
          }}
        >
          {items.map((c) => (
            <option value={c.id} key={c.id}>
              {c.order.product_name} · {typeLabels[c.type]} ·{" "}
              {statusLabels[c.status]}
            </option>
          ))}
        </select>
      )}
      <div className="inspector-inner">
        <p className="order-reference" title={item.order_id}>
          订单{" "}
          {item.order_id.startsWith("order_")
            ? `#${item.order_id.slice(-8).toUpperCase()}`
            : item.order_id}
        </p>
        <div className="order-product">
          <img src={item.order.image} alt={item.order.product_name} />
          <div>
            <h3>
              {item.order.product_name} · {item.order.sku_label}
            </h3>
            <p>实付 ¥{money(item.order.total_cents)}</p>
            <p>
              {item.order.delivered_at
                ? `${date(item.order.delivered_at)} 签收`
                : "暂未签收"}
            </p>
          </div>
        </div>
        <CaseSummary item={item} compact />
        {item.type !== "return_refund" && (
          <div className="stock-summary">
            <b>{item.reship_kind === "part" ? "当前配件库存" : "当前可用库存"}</b>
            <span>{(item.reship_kind === "part" ? partSKU?.stock : sku?.stock) ?? "—"}</span>
          </div>
        )}
        {["submitted", "approved", "needs_info", "draft"].includes(
          item.status,
        ) && (
          <button
            className="text-link proposal-edit"
            onClick={() => {
              setType(item.type);
              setQuantity(item.quantity);
              setTarget(item.target_sku_id || item.order.sku_id);
              setReshipKind(item.reship_kind || "whole");
              setPartID(item.part_sku_id || packageParts[0]?.part_sku_id || "");
              setPayer(item.shipping_payer);
              setEditing(!editing);
            }}
          >
            <RotateCcw size={14} />
            调整方案并请顾客重新确认
          </button>
        )}
        {editing && (
          <div className="proposal-form">
            <label className="form-field">
              处理类型
              <select
                aria-label="处理类型"
                value={type}
                onChange={(e) => { setType(e.target.value as CaseType); setQuantity(1); }}
              >
                {Object.entries(typeLabels).map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
            {type === "reship" && <label className="form-field">补发对象<select value={reshipKind} onChange={(e) => { setReshipKind(e.target.value as "whole" | "part"); setQuantity(1); }}><option value="whole">整件商品</option><option value="part" disabled={!packageParts.length}>随单配件</option></select></label>}
            <label className="form-field">
              数量
              <input
                type="number"
                min={1}
                max={type === "reship" && reshipKind === "part" ? (packageParts.find((part) => part.part_sku_id === partID)?.quantity || 1) * item.order.quantity : item.order.quantity}
                value={quantity}
                onChange={(e) => setQuantity(Number(e.target.value))}
              />
            </label>
            {type === "reship" && reshipKind === "part" ? <label className="form-field">补发配件<select value={partID} onChange={(e) => { setPartID(e.target.value); setQuantity(1); }}>{packageParts.map((part) => <option key={part.id} value={part.part_sku_id}>{part.name}</option>)}</select></label> : type !== "return_refund" && (
              <label className="form-field">
                目标规格
                <select
                  aria-label="目标规格"
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                >
                  {options.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="form-field">
              运费承担
              <select
                aria-label="运费承担"
                value={payer}
                onChange={(e) => setPayer(e.target.value as typeof payer)}
              >
                {Object.entries(shippingLabels).map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
            <p className="muted small">
              保存后回到待确认状态，顾客重新确认后才能审核。
            </p>
            <button
              className="button primary full"
              disabled={busy}
              onClick={() => void changeProposal()}
            >
              保存并请求重新确认
            </button>
          </div>
        )}
        <CaseHistory item={item} />
        <QualityTags targetType="case" targetID={item.id} />
      </div>
      <div className="inspector-action-area">
        {item.status === "submitted" || item.status === "approved" ? (
          <>
            <label className="form-field staff-note">
              填写处理说明
              <textarea
                rows={4}
                maxLength={2000}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="请输入顾客可见的处理说明"
              />
              <small className="muted">{note.length}/2000</small>
            </label>
            {item.type === "reship" && item.reship_kind !== "part" && <div className="return-handling"><label className="form-field">原商品回收安排<select aria-label="原商品回收安排" value={returnHandling} onChange={(e) => setReturnHandling(e.target.value as typeof returnHandling)}><option value="">少发无需回收；破损、质量问题请选择</option><option value="returned">已退回</option><option value="scheduled">已安排退回</option><option value="waived">人工同意免退</option></select></label>{returnHandling && <label className="form-field">回收或免退说明<textarea rows={2} value={returnNote} onChange={(e) => setReturnNote(e.target.value)} placeholder="说明退回安排或免退依据，顾客可见" /></label>}</div>}
            <ErrorNote error={error} />
            {item.stale_since_message_id && <div className="notice-box" role="alert">顾客已更正或撤回旧申请，请先核对。旧方案不能直接同意或记录完成。</div>}
            {item.status === "submitted" ? (
              <div className="review-actions">
                <button
                  className="button primary"
                  disabled={busy || Boolean(item.stale_since_message_id)}
                  onClick={() => void action("approve")}
                >
                  {busy ? <Spinner /> : null}同意申请
                </button>
                <button
                  className="button"
                  disabled={busy}
                  onClick={() => void action("needs_info")}
                >
                  补充材料
                </button>
                <button
                  className="button danger-text"
                  disabled={busy}
                  onClick={() => void action("reject")}
                >
                  拒绝
                </button>
              </div>
            ) : (
              <button
                className="button primary full"
                disabled={busy || Boolean(item.stale_since_message_id)}
                onClick={() => void action("complete")}
              >
                {busy ? (
                  <Spinner />
                ) : item.type === "return_refund" ? (
                  <PackageCheck size={17} />
                ) : (
                  <Truck size={17} />
                )}
                记录演示处理完成
              </button>
            )}
            <p className="muted small">
              {item.status === "approved"
                ? "请实际核实完成情况后记录。此处仅更新演示记录。"
                : "同意后进入待处理，完成结果由人工记录。"}
            </p>
          </>
        ) : (
          <>
            <ErrorNote error={error} />
            <p className="notice-box">
              {item.status === "draft"
                ? "等待顾客确认申请。"
                : item.status === "needs_info"
                  ? "等待顾客补充材料后重新审核。"
                  : item.status === "completed"
                    ? "此申请已记录演示处理完成。"
                    : "此申请已拒绝，说明已对顾客展示。"}
            </p>
          </>
        )}
      </div>
    </aside>
  );
}
