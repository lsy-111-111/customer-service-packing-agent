import { useRef, useState } from "react";
import type { PreferenceKey, Preferences, Product } from "./types";
import { money, patch, requestID } from "./api";
import { ErrorNote, Spinner } from "./ui";

const labels: Record<PreferenceKey, string> = { budget_max_cents: "预算上限", colors: "颜色", excluded_colors: "排除颜色", sizes: "个人尺码", excluded_sizes: "不穿的尺码", usage: "使用场景", required_features: "需要的特点", excluded_features: "不考虑的特点", excluded_products: "不考虑的商品" };
function valueText(key: PreferenceKey, value: unknown) {
  if (key === "budget_max_cents" && typeof value === "number") return `¥${money(value)}`;
  return Array.isArray(value) ? value.join("、") : String(value ?? "");
}
export function PreferencePanel({ conversationID, preferences, products = [], onUpdated }: { conversationID: string; preferences?: Preferences; products?: Product[]; onUpdated: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requests = useRef<Record<string, string>>({});
  const active = preferences?.items.filter(i => i.active) || [];
  const conditional = preferences?.conditional_requests?.filter(i => i.active) || [];
  const count = active.length + conditional.length;
  async function save(key?: PreferenceKey, scope?: string, remove = false, productID?: string | null, conditionalID?: string) {
    if (!preferences) return;
    let parsed: unknown = null;
    if (!remove && key) {
      if (key === "budget_max_cents") {
        if (!/^\d+(\.\d{1,2})?$/.test(value.trim())) { setError("预算请填写非负金额，最多两位小数"); return; }
        parsed = Math.round(Number(value) * 100);
        if (!Number.isSafeInteger(parsed) || Number(parsed) > 100000000) { setError("预算金额超出支持范围，请填写不超过100万元的金额"); return; }
      } else parsed = key === "usage" ? value.trim() : value.split(/[、,，\n]/).map(v => v.trim()).filter(Boolean);
    }
    const update = conditionalID
      ? { conditional_changes: [key ? { id: conditionalID, key, value: parsed } : { id: conditionalID, clear: true }] }
      : key ? { changes: [{ key, scope, ...(productID ? { product_id: productID } : {}), value: parsed }] } : { clear_all: true };
    const body = { expected_version: preferences.version, ...update };
    const identity = JSON.stringify(body);
    const rid = requests.current[identity] || (requests.current[identity] = requestID());
    setBusy(true); setError("");
    try { await patch(`/conversations/${conversationID}/preferences`, { ...body, request_id: rid }); setEditing(null); onUpdated(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <details className="preference-panel">
    <summary>本次选购偏好 <span>{count ? `${count} 项 · 可更正` : "尚未记录"}</span></summary>
    <div className="preference-content">
      <p className="muted small">这些偏好用于本次选购。可以直接在对话中改口，也可以在这里更正或移除。</p>
      {active.map((item) => {
        const id = `${item.scope}:${item.product_id || "all"}:${item.key}`;
        const product = products.find(p => p.id === item.product_id);
        const scopeLabel = item.product_id ? `${item.scope} · ${product?.model || item.product_id}` : item.scope === "会话" ? "本次选购" : `全部${item.scope}`;
        return <div className="preference-row" key={id}>
          <div><b>{labels[item.key]}</b><small>{scopeLabel}</small></div>
          {editing === id ? <><input aria-label={`更正${labels[item.key]}`} value={value} onChange={e => setValue(e.target.value)} /><button className="text-link" disabled={busy} onClick={() => void save(item.key, item.scope, false, item.product_id)}>保存</button><button className="text-link" onClick={() => setEditing(null)}>取消</button></> : <><span>{valueText(item.key, item.value)}</span><button className="text-link" disabled={busy} onClick={() => { setEditing(id); setValue(item.key === "budget_max_cents" ? String(Number(item.value) / 100) : valueText(item.key, item.value)); }}>更正</button><button className="text-link" disabled={busy} onClick={() => void save(item.key, item.scope, true, item.product_id)}>移除</button></>}
        </div>;
      })}
      {conditional.map(request => <section className="conditional-preference" key={request.id} aria-label={`条件选购：${request.condition.subject_text}`}>
        <div className="preference-row">
          <div><b>条件选购要求</b><small>{request.scope === "会话" ? "本次选购" : request.scope}</small></div>
          <span>若本店没有{request.condition.subject_text}，再找替代商品。</span>
          <button className="text-link" disabled={busy} onClick={() => void save(undefined, undefined, true, undefined, request.id)}>移除这项条件要求</button>
        </div>
        <p className="muted small">仅在上述条件下使用，不代表已选定或下单。</p>
        {Object.entries(request.constraints).filter(([key, itemValue]) => key in labels && itemValue != null).map(([rawKey, itemValue]) => {
          const key = rawKey as PreferenceKey;
          const id = `conditional:${request.id}:${key}`;
          return <div className="preference-row" key={id}>
            <div><b>{labels[key]}</b><small>仅此替代要求</small></div>
            {editing === id ? <><input aria-label={`更正条件${labels[key]}`} value={value} onChange={e => setValue(e.target.value)} /><button className="text-link" disabled={busy} onClick={() => void save(key, undefined, false, undefined, request.id)}>保存</button><button className="text-link" onClick={() => setEditing(null)}>取消</button></> : <><span>{valueText(key, itemValue)}</span><button className="text-link" aria-label={`更正条件${labels[key]}`} disabled={busy} onClick={() => { setEditing(id); setValue(key === "budget_max_cents" ? String(Number(itemValue) / 100) : valueText(key, itemValue)); }}>更正</button><button className="text-link" aria-label={`移除条件${labels[key]}`} disabled={busy} onClick={() => void save(key, undefined, true, undefined, request.id)}>移除</button></>}
          </div>;
        })}
        <details>
          <summary className="muted small">查看要求来源</summary>
          <p className="small">条件来源：{request.condition.source_text}</p>
          {request.constraint_sources
            ? Object.entries(request.constraint_sources).filter(([key]) => key in labels && request.constraints[key as PreferenceKey] != null).map(([key, source]) => source ? <p className="small" key={key}>{labels[key as PreferenceKey]}来源：{source.source_text}</p> : null)
            : <p className="small">{request.source_text}</p>}
        </details>
      </section>)}
      {count > 0 && <button className="text-link" disabled={busy} onClick={() => void save()}>{busy && <Spinner />}不再使用以上偏好</button>}
      <ErrorNote error={error} />
    </div>
  </details>;
}
