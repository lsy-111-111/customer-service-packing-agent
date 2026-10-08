import { useEffect, useState } from "react";
import { Check, ChevronRight, Plus, Save, Trash2 } from "lucide-react";
import { api, date, money, patch, post, put } from "./api";
import type { AdminData, PartSKU, Policy, Product, Promotions, SKU } from "./types";
import { ErrorNote, Modal, Spinner } from "./ui";
import { AddVariant, CatalogImport, NewProduct, StandardAnswers, catalogPriceCents, catalogStock } from "./CatalogManagement";
const tabs = ["商品资料", "库存与补货", "表格导入", "标准答案", "优惠活动", "售后规则", "修改记录"];
export function AdminDrawer({
  onClose,
  onUpdated,
}: {
  onClose: () => void;
  onUpdated: () => void;
}) {
  const [data, setData] = useState<AdminData | null>(null);
  const [tab, setTab] = useState(tabs[0]);
  const [productID, setProductID] = useState("");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [creating, setCreating] = useState(false);
  async function load() {
    try {
      const d = await api<AdminData>("/admin/data");
      setData(d);
      setProductID((v) => v || d.products[0]?.id || "");
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  async function complete(message = "已保存，后续客服查询会使用新资料") {
    setSaved(message);
    await load();
    onUpdated();
  }
  const selected = data?.products.find((p) => p.id === productID);
  return (
    <Modal
      title="经营资料"
      subtitle="维护店铺信息，客服查询时会读取最新资料"
      onClose={onClose}
      wide
    >
      <div className="admin-tabs">
        {tabs.map((t) => (
          <button
            className={t === tab ? "active" : ""}
            onClick={() => {
              setTab(t);
              setSaved("");
            }}
            key={t}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="drawer-body">
        <ErrorNote error={error} />
        {saved && (
          <div className="success-note">
            <Check size={17} />
            {saved}
          </div>
        )}
        {!data ? (
          <div className="loading-panel">
            <Spinner />
            正在读取经营资料
          </div>
        ) : (
          <>
            {tab === "商品资料" && <div className="catalog-management-toolbar"><button className="button" onClick={() => setCreating(!creating)}><Plus size={16} />{creating ? "返回已有商品" : "新增商品"}</button></div>}
            {tab === "商品资料" && creating && <NewProduct onSaved={() => complete("已新增商品，店铺里可以查看并模拟下单")} onCancel={() => setCreating(false)} />}
            {["商品资料", "库存与补货"].includes(tab) && !creating && (
              <div className="admin-product-selector">
                <label>
                  选择商品
                  <select
                    aria-label="选择商品"
                    value={productID}
                    onChange={(e) => {
                      setProductID(e.target.value);
                      setSaved("");
                    }}
                  >
                    {data.products.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </label>
                {selected && <img src={selected.image} alt={selected.name} />}
              </div>
            )}
            {tab === "商品资料" && !creating && selected && (
              <ProductEditor
                key={`${selected.id}-${selected.catalog_version || 1}`}
                product={selected}
                onSave={complete}
              />
            )}{" "}
            {tab === "库存与补货" && selected && (
              <InventoryEditor
                key={selected.id}
                product={selected}
                onSave={complete}
              />
            )}{" "}
            {tab === "表格导入" && <CatalogImport onSaved={() => complete("商品表格已确认导入")} />}
            {tab === "标准答案" && <StandardAnswers products={data.products} onSaved={() => complete("标准答案已更新")} />}
            {tab === "优惠活动" && (
              <PromotionEditor
                key={data.promotions.version}
                value={data.promotions}
                products={data.products}
                onSave={complete}
              />
            )}{" "}
            {tab === "售后规则" && (
              <PolicyEditor
                key={data.policy.version}
                value={data.policy}
                history={data.policy_history}
                onSave={complete}
              />
            )}{" "}
            {tab === "修改记录" && (
              <div className="audit-list">
                {data.audit.length ? (
                  data.audit
                    .slice()
                    .reverse()
                    .map((a, i) => (
                      <div key={i}>
                        <span className="timeline-dot" />
                        <div>
                          <b>{String(a.action || a.type || "资料更新")}</b>
                          <p>
                            {String(
                              a.target_id ||
                                a.entity_id ||
                                a.note ||
                                a.summary ||
                                "经营资料已更新",
                            )}
                          </p>
                          <small>
                            {date(String(a.created_at || a.at || ""), true)} ·{" "}
                            {String(
                              a.actor_name ||
                                a.user_id ||
                                a.actor_id ||
                                "人工客服",
                            )}
                          </small>
                        </div>
                      </div>
                    ))
                ) : (
                  <p className="muted">还没有经营资料修改记录。</p>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
function ProductEditor({
  product,
  onSave,
}: {
  product: Product;
  onSave: () => Promise<void>;
}) {
  const [description, setDescription] = useState(product.description);
  const [specs, setSpecs] = useState(
    Object.entries(product.specs).map(([key, value]) => ({
      key,
      value: typeof value === "string" ? value : JSON.stringify(value),
    })),
  );
  const [base, setBase] = useState({ name: product.name, model: product.model, category: product.category });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true);
    setError("");
    try {
      if (!description.trim()) throw new Error("请填写商品描述");
      if (specs.some((s) => !s.key.trim() || !s.value.trim()))
        throw new Error("请完整填写规格名称和内容");
      if (new Set(specs.map((s) => s.key.trim())).size !== specs.length)
        throw new Error("规格名称不能重复");
      await patch(`/admin/catalog/products/${product.id}`, {
        ...base, expected_version: product.catalog_version || 1,
        description,
        specs: Object.fromEntries(specs.map((s) => [s.key.trim(), s.value])),
      });
      await onSave();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="form-grid">{([["name", "商品名称"], ["model", "型号"], ["category", "分类"]] as const).map(([key,label]) => <label className="form-field" key={key}>{label}<input value={base[key]} onChange={e => setBase({...base,[key]:e.target.value})} /></label>)}</div>
      <label className="form-field">
        商品描述
        <textarea
          rows={3}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <div className="editor-section">
        <div className="row-between">
          <h3>规格参数</h3>
          <button
            className="text-link"
            onClick={() => setSpecs([...specs, { key: "", value: "" }])}
          >
            <Plus size={15} />
            添加参数
          </button>
        </div>
        {specs.map((s, i) => (
          <div className="key-value-editor" key={i}>
            <input
              aria-label={`参数${i + 1}名称`}
              value={s.key}
              onChange={(e) =>
                setSpecs(
                  specs.map((v, n) =>
                    n === i ? { ...v, key: e.target.value } : v,
                  ),
                )
              }
            />
            <input
              aria-label={`参数${i + 1}内容`}
              value={s.value}
              onChange={(e) =>
                setSpecs(
                  specs.map((v, n) =>
                    n === i ? { ...v, value: e.target.value } : v,
                  ),
                )
              }
            />
            <button
              className="icon-button"
              onClick={() => setSpecs(specs.filter((_, n) => n !== i))}
              aria-label="删除参数"
            >
              <Trash2 size={16} />
            </button>
          </div>
        ))}
      </div>
      <p className="muted small">常见问题请在“标准答案”中提交审核，审核通过后生效。</p>
      <ErrorNote error={error} />
      <button
        className="button primary full"
        onClick={() => void save()}
        disabled={busy}
      >
        {busy ? <Spinner /> : <Save size={17} />}保存商品资料
      </button>
    </>
  );
}
function InventoryEditor({
  product,
  onSave,
}: {
  product: Product;
  onSave: () => Promise<void>;
}) {
  const [skuID, setSkuID] = useState(product.skus[0]?.id);
  const sku = product.skus.find((s) => s.id === skuID);
  return (
    <>
      <div className="inventory-list">
        {product.skus.map((s) => (
          <button
            className={s.id === skuID ? "active" : ""}
            key={s.id}
            onClick={() => setSkuID(s.id)}
          >
            <span>
              <b>{s.label}</b>
              <small>{[s.color,s.size].filter(Boolean).join(" / ")}</small>
            </span>
            <span>¥ {money(s.price_cents)}</span>
            <span className={!s.stock ? "danger-text" : ""}>{s.stock} 件</span>
            <ChevronRight size={16} />
          </button>
        ))}
      </div>
      {sku && (
        <SKUEditor key={`${sku.id}-${sku.version}`} sku={sku} onSave={onSave} />
      )}
      <AddVariant product={product} onSaved={onSave} />
      {!!product.parts?.length && <div className="editor-section"><h3>独立配件库存</h3><p className="muted small">配件补发只扣除这里的库存，不扣整件商品。</p>{product.parts.map((part) => <PartEditor key={`${part.id}-${part.version}`} part={part} onSave={onSave} />)}</div>}
    </>
  );
}
function PartEditor({ part, onSave }: { part: PartSKU; onSave: () => Promise<void> }) {
  const [stock, setStock] = useState(part.stock);
  const [restock, setRestock] = useState(part.restock_at?.slice(0, 10) || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true); setError("");
    try {
      await patch(`/admin/skus/${part.id}`, { stock, restock_at: restock ? `${restock}T04:00:00Z` : null, expected_version: part.version });
      await onSave();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="part-stock-editor"><h4>{part.name}</h4><div className="form-grid"><label className="form-field">配件库存<input type="number" min={0} step={1} value={stock} onChange={(e) => setStock(Number(e.target.value))} /></label><label className="form-field">预计补货日期<input type="date" value={restock} onChange={(e) => setRestock(e.target.value)} /></label></div><ErrorNote error={error} /><button className="button" disabled={busy || stock < 0 || !Number.isInteger(stock)} onClick={() => void save()}>{busy && <Spinner />}保存配件库存</button></div>;
}
function SKUEditor({ sku, onSave }: { sku: SKU; onSave: () => Promise<void> }) {
  const [stock, setStock] = useState(String(sku.stock));
  const [price, setPrice] = useState(String(sku.price_cents / 100));
  const [restock, setRestock] = useState(
    sku.restock_at ? sku.restock_at.slice(0, 10) : "",
  );
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true);
    setError("");
    try {
      await patch(`/admin/skus/${sku.id}`, {
        stock: catalogStock(stock),
        price_cents: catalogPriceCents(price, "单价"),
        restock_at: restock ? `${restock}T04:00:00Z` : null,
        expected_version: sku.version,
      });
      await onSave();
      setPreview(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function previewOrSave() {
    try {
      catalogStock(stock);
      catalogPriceCents(price, "单价");
      setError("");
      if (preview) void save();
      else setPreview(true);
    } catch (e) {
      setPreview(false);
      setError((e as Error).message);
    }
  }
  return (
    <div className="editor-section">
      <h3>{sku.label}</h3>
      <div className="form-grid">
        <label className="form-field">
          可售库存
          <input
            type="number"
            min={0}
            step={1}
            value={stock}
            onChange={(e) => {
              setStock(e.target.value);
              setPreview(false);
            }}
          />
        </label>
        <label className="form-field">
          单价（元）
          <input
            type="number"
            min={0.01}
            step={0.01}
            value={price}
            onChange={(e) => {
              setPrice(e.target.value);
              setPreview(false);
            }}
          />
        </label>
      </div>
      <label className="form-field">
        预计补货日期 <span className="muted">留空表示尚未确定</span>
        <input
          type="date"
          value={restock}
          onChange={(e) => {
            setRestock(e.target.value);
            setPreview(false);
          }}
        />
      </label>
      {preview && (
        <div className="diff-box">
          <h4>确认修改</h4>
          <p>
            库存：{sku.stock} → {stock} 件
          </p>
          <p>
            单价：¥{money(sku.price_cents)} → ¥{price}
          </p>
          <p>
            补货日期：{sku.restock_at ? date(sku.restock_at) : "未知"} →{" "}
            {restock || "未知"}
          </p>
          <small>历史订单金额保持原快照；未确认的报价需要重新计算。</small>
        </div>
      )}
      <ErrorNote error={error} />
      <button
        className="button primary full"
        disabled={busy}
        onClick={previewOrSave}
      >
        {busy ? <Spinner /> : <Save size={16} />}{" "}
        {preview ? "确认保存" : "预览修改"}
      </button>
    </div>
  );
}
function PromotionEditor({
  value,
  products,
  onSave,
}: {
  value: Promotions;
  products: Product[];
  onSave: () => Promise<void>;
}) {
  const [v, setV] = useState(value);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function change<K extends keyof Promotions>(key: K, value: Promotions[K]) {
    setV({ ...v, [key]: value });
    setPreview(false);
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      await put("/admin/promotions", { ...v, expected_version: value.version });
      await onSave();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="row-between editor-section">
        <h3>全店优惠</h3>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={v.enabled}
            onChange={(e) => change("enabled", e.target.checked)}
          />
          启用活动
        </label>
      </div>
      <div className="form-grid">
        <label className="form-field">
          满减门槛（元）
          <input
            type="number"
            min={0}
            value={v.threshold_cents / 100}
            onChange={(e) =>
              change(
                "threshold_cents",
                Math.round(Number(e.target.value) * 100),
              )
            }
          />
        </label>
        <label className="form-field">
          减免金额（元）
          <input
            type="number"
            min={0}
            value={v.reduction_cents / 100}
            onChange={(e) =>
              change(
                "reduction_cents",
                Math.round(Number(e.target.value) * 100),
              )
            }
          />
        </label>
      </div>
      <h3>单品活动价</h3>
      <div className="form-grid">
        <label className="form-field">
          活动商品
          <select
            aria-label="活动商品"
            value={v.special_product_id}
            onChange={(e) => change("special_product_id", e.target.value)}
          >
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="form-field">
          活动价（元）
          <input
            type="number"
            min={0.01}
            step={0.01}
            value={v.special_price_cents / 100}
            onChange={(e) =>
              change(
                "special_price_cents",
                Math.round(Number(e.target.value) * 100),
              )
            }
          />
        </label>
      </div>
      <div className="form-grid">
        <label className="form-field">
          开始日期
          <input
            type="date"
            value={v.starts_at?.slice(0, 10) || ""}
            onChange={(e) =>
              change("starts_at", `${e.target.value}T00:00:00+08:00`)
            }
          />
        </label>
        <label className="form-field">
          结束日期
          <input
            type="date"
            value={v.ends_at?.slice(0, 10) || ""}
            onChange={(e) =>
              change("ends_at", `${e.target.value}T23:59:59+08:00`)
            }
          />
        </label>
      </div>
      <p className="notice-box">
        满减与活动价互斥，由程序自动选择更优惠的一种。
      </p>
      {preview && (
        <div className="diff-box">
          <h4>发布预览 · 当前版本 {value.version}</h4>
          <p>
            活动状态：{value.enabled ? "启用" : "停用"} →{" "}
            {v.enabled ? "启用" : "停用"}
          </p>
          <p>
            满 {money(value.threshold_cents)} 减 {money(value.reduction_cents)}{" "}
            → 满 {money(v.threshold_cents)} 减 {money(v.reduction_cents)}
          </p>
          <p>
            特价：{value.special_product_id} ¥{money(value.special_price_cents)}{" "}
            → {v.special_product_id} ¥{money(v.special_price_cents)}
          </p>
          <p>
            {date(v.starts_at)} 至 {date(v.ends_at)}
          </p>
        </div>
      )}
      <ErrorNote error={error} />
      <button
        className="button primary full"
        disabled={busy}
        onClick={() => (preview ? void save() : setPreview(true))}
      >
        {busy ? <Spinner /> : <Save size={17} />}{" "}
        {preview ? "确认发布优惠" : "预览优惠修改"}
      </button>
    </>
  );
}
function PolicyEditor({
  value,
  history,
  onSave,
}: {
  value: Policy;
  history: Policy[];
  onSave: () => Promise<void>;
}) {
  const [v, setV] = useState(value);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function change<K extends keyof Policy>(key: K, value: Policy[K]) {
    setV({ ...v, [key]: value });
    setPreview(false);
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      await post("/admin/policies", {
        return_days: v.return_days,
        exchange_days: v.exchange_days,
        quality_days: v.quality_days ?? 30,
        reship_days: v.reship_days ?? 30,
        warranty_days: v.warranty_days ?? 365,
        no_reason_shipping: v.no_reason_shipping,
        quality_note: v.quality_note,
        extra_note: v.extra_note,
        expected_version: value.version,
      });
      await onSave();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="row-between">
        <h3>售后规则</h3>
        <span className="badge neutral">当前版本 {value.version}</span>
      </div>
      <div className="form-grid">
        <label className="form-field">
          无理由退货期限（天）
          <input
            type="number"
            min={7}
            max={365}
            value={v.return_days}
            onChange={(e) => change("return_days", Number(e.target.value))}
          />
        </label>
        <label className="form-field">
          非质量换货期限（天）
          <input
            type="number"
            min={1}
            max={365}
            value={v.exchange_days}
            onChange={(e) => change("exchange_days", Number(e.target.value))}
          />
        </label>
      </div>
      <div className="form-grid">
        <label className="form-field">质量退换承诺（天）<input type="number" min={1} max={3650} value={v.quality_days ?? 30} onChange={(e) => change("quality_days", Number(e.target.value))} /></label>
        <label className="form-field">补发便捷申报期（天）<input type="number" min={1} max={3650} value={v.reship_days ?? 30} onChange={(e) => change("reship_days", Number(e.target.value))} /></label>
        <label className="form-field">数码家电保修（天）<input type="number" min={365} max={3650} value={v.warranty_days ?? 365} onChange={(e) => change("warranty_days", Number(e.target.value))} /></label>
      </div>
      <label className="form-field">
        无理由退货运费
        <select
          aria-label="无理由退货运费"
          value={v.no_reason_shipping}
          onChange={(e) =>
            change("no_reason_shipping", e.target.value as "customer" | "store")
          }
        >
          <option value="customer">顾客承担</option>
          <option value="store">店铺承担</option>
        </select>
      </label>
      <label className="form-field">
        质量问题说明
        <textarea
          rows={3}
          value={v.quality_note}
          onChange={(e) => change("quality_note", e.target.value)}
        />
      </label>
      <label className="form-field">
        补充承诺
        <textarea
          rows={3}
          value={v.extra_note}
          onChange={(e) => change("extra_note", e.target.value)}
        />
      </label>
      <p className="notice-box">
        退货期至少 7
        天。合理拆封查验不自动拒退；补发申报期不免除已确认的欠发责任。历史订单保留原规则并适用更有利承诺，人工终审记录不覆盖。
      </p>
      {preview && (
        <div className="diff-box">
          <h4>规则发布预览</h4>
          <p>
            退货期限：{value.return_days} → {v.return_days} 天
          </p>
          <p>
            换货期限：{value.exchange_days} → {v.exchange_days} 天
          </p>
          <p>
            无理由运费：{value.no_reason_shipping === "store" ? "店铺" : "顾客"}{" "}
            → {v.no_reason_shipping === "store" ? "店铺" : "顾客"}
          </p>
          <p>质量退换：{value.quality_days ?? 30} → {v.quality_days ?? 30} 天 · 补发便捷期：{value.reship_days ?? 30} → {v.reship_days ?? 30} 天</p>
          <p>数码家电保修：{value.warranty_days ?? 365} → {v.warranty_days ?? 365} 天</p>
          <p>质量问题：{v.quality_note}</p>
          <p>补充承诺：{v.extra_note}</p>
        </div>
      )}
      <ErrorNote error={error} />
      <button
        className="button primary full"
        disabled={busy || v.return_days < 7}
        onClick={() => (preview ? void save() : setPreview(true))}
      >
        {busy ? <Spinner /> : <Save size={17} />}{" "}
        {preview ? "确认发布新版本" : "预览规则修改"}
      </button>
      <div className="editor-section">
        <h3>历史版本</h3>
        {history.map((p) => (
          <details className="faq" key={p.version}>
            <summary>
              版本 {p.version}{" "}
              <span className="muted small">{date(p.published_at, true)}</span>
            </summary>
            <p>
              {p.return_days} 天无理由退货 · {p.exchange_days} 天换货
            </p>
            <p>{p.quality_note}</p>
            <p>{p.extra_note}</p>
          </details>
        ))}
      </div>
    </>
  );
}
