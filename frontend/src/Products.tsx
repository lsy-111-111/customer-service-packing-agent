import { useEffect, useRef, useState } from "react";
import {
  Search,
  ChevronRight,
  ArrowRight,
  Check,
  Minus,
  Plus,
  PackageCheck,
} from "lucide-react";
import type { Order, Product, Quote, ResourceState, SKU } from "./types";
import { APIError, date, money, post, requestID } from "./api";
import { Empty, ErrorNote, Modal, ResourceNotice, Spinner } from "./ui";
import { ProductLinks } from "./ProductLinks";
import { productPath } from "./catalogLinks";

export function Catalog({
  products,
  selected,
  onConsult,
  onDetails,
  loadState,
  onRetry,
  consultEnabled = true,
}: {
  products: Product[];
  selected: string | null;
  onConsult: (p: Product) => void;
  onDetails: (p: Product) => void;
  loadState?: ResourceState;
  onRetry?: () => void;
  consultEnabled?: boolean;
}) {
  const categories = ["全部", ...new Set(products.map(p => p.category))];
  const [category, setCategory] = useState("全部");
  const [search, setSearch] = useState("");
  const shown = products.filter(
    (p) =>
      (category === "全部" || p.category === category) &&
      [p.name, p.model, p.description, ...p.aliases]
        .join(" ")
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
  );
  return (
    <section className="catalog">
      <div className="catalog-heading">
        <h1>挑些喜欢的，不确定就问问。</h1>
        <p>数码、家电、服饰与家居</p>
      </div>
      <div className="catalog-toolbar">
        <div className="category-tabs" role="tablist" aria-label="商品分类">
          {categories.map((c) => (
            <button
              role="tab"
              aria-selected={c === category}
              key={c}
              className={c === category ? "active" : ""}
              onClick={() => setCategory(c)}
            >
              {c}
            </button>
          ))}
        </div>
        <label className="search-field">
          <Search size={19} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索商品或型号"
            aria-label="搜索商品或型号"
          />
        </label>
      </div>
      <ResourceNotice label="商品" state={loadState} onRetry={onRetry} />
      <div className="product-grid" aria-busy={loadState?.loading && !loadState.loaded}>
        {shown.map((p) => (
          <article
            className={`product-tile ${selected === p.id ? "selected" : ""}`}
            key={p.id}
          >
            <button
              className="product-image"
              onClick={() => onDetails(p)}
              aria-label={`查看${p.name}`}
            >
              <img
                src={p.image}
                alt={p.name}
                loading={shown.indexOf(p) < 4 ? "eager" : "lazy"}
              />
            </button>
            <div className="product-content">
              <h2>
                <button onClick={() => onDetails(p)}>
                  {p.name}
                  <span className="model-label">{p.model}</span>
                </button>
              </h2>
              <div className="price-row">
                <span className="price">
                  <small>¥</small>
                  {money(p.price_cents)}
                </span>
                {p.price_cents >= 19900 && (
                  <span className="offer-tag">优惠可查询</span>
                )}
              </div>
              <div className="product-actions">
                <button onClick={() => onConsult(p)} disabled={!consultEnabled}>
                  咨询这款
                  <ChevronRight size={18} />
                </button>
                <span />
                <button onClick={() => onDetails(p)}>
                  查看规格
                  <ChevronRight size={18} />
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
      {!shown.length && (!loadState || (loadState.loaded && !loadState.error)) && (
        <Empty title={products.length ? "没有找到这款商品" : "店铺暂时没有商品"}>{products.length ? "换个型号、品类或关键词试试。" : "稍后再来看看，也可以联系店铺客服。"}</Empty>
      )}
      <div className="catalog-foot">
        认真挑选，安心咨询。<span>{(!loadState || loadState.loaded) && `${products.length} 款日常好物 · `}演示店铺</span>
      </div>
    </section>
  );
}
export function ProductDetail({
  product,
  onClose,
  onConsult,
  onPurchased,
  onCompare,
  compareIDs,
  readOnly = false,
}: {
  product: Product;
  readOnly?: boolean;
  onClose: () => void;
  onConsult: (p: Product) => void;
  onPurchased: (o: Order) => void;
  onCompare: (p: Product) => void;
  compareIDs: string[];
}) {
  const [skuID, setSkuID] = useState(product.selected_sku_id || product.skus[0]?.id || "");
  const [quantity, setQuantity] = useState(1);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const seq = useRef(0);
  const request = useRef("");
  const sku = product.skus.find((s) => s.id === skuID);
  useEffect(() => {
    const n = ++seq.current;
    setQuote(null);
    setConfirm(false);
    setLoading(true);
    setError("");
    post<Quote>("/quotes", { sku_id: skuID, quantity })
      .then((q) => {
        if (seq.current === n) {
          setQuote(q);
          request.current = requestID();
        }
      })
      .catch((e) => {
        if (seq.current === n) setError(e.message);
      })
      .finally(() => {
        if (seq.current === n) setLoading(false);
      });
  }, [skuID, quantity, sku?.version]);
  async function buy() {
    if (!quote) return;
    setBusy(true);
    setError("");
    try {
      const order = await post<Order>("/orders", {
        quote_id: quote.quote_id,
        request_id: request.current,
      });
      onPurchased(order);
    } catch (e) {
      setError((e as Error).message);
      setConfirm(false);
      if (e instanceof APIError && e.status === 409) {
        try {
          const next = await post<Quote>("/quotes", {
            sku_id: skuID,
            quantity,
          });
          setQuote(next);
          request.current = requestID();
          setError(`${e.message}。已重新核对，请查看最新金额后确认。`);
        } catch {
          setQuote(null);
        }
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={product.name}
      subtitle={`${product.model} · ${product.category}`}
      onClose={onClose}
    >
      <div className="drawer-body">
        <img className="detail-photo" src={product.image} alt={product.name} />
        <p className="detail-description">{product.description}</p>
        <div className="detail-section">
          <h3>选择规格</h3>
          <div className="sku-options">
            {product.skus.map((s) => (
              <button
                key={s.id}
                className={s.id === skuID ? "active" : ""}
                onClick={() => { setSkuID(s.id); const path = productPath(product, s.id); if (path) history.replaceState(null, "", path); }}
              >
                {s.label}
                {s.stock === 0 && <small>暂时缺货</small>}
              </button>
            ))}
          </div>
          {sku && <StockLine sku={sku} />}
          <ProductLinks product={product} skuID={skuID} />
        </div>
        <div className="quantity-row">
          <label>数量</label>
          <div className="stepper">
            <button
              disabled={quantity <= 1}
              onClick={() => setQuantity((v) => Math.max(1, v - 1))}
              aria-label="减少数量"
            >
              <Minus size={16} />
            </button>
            <output>{quantity}</output>
            <button
              disabled={quantity >= 99}
              onClick={() => setQuantity((v) => v + 1)}
              aria-label="增加数量"
            >
              <Plus size={16} />
            </button>
          </div>
        </div>
        <div className="quote-panel">
          {loading ? (
            <div className="muted inline">
              <Spinner /> 正在核对库存与优惠
            </div>
          ) : quote ? (
            <>
              <div>
                <span>商品金额</span>
                <span>¥ {money(quote.subtotal_cents)}</span>
              </div>
              <div>
                <span>{quote.promotion_label || "优惠"}</span>
                <span>− ¥ {money(quote.discount_cents)}</span>
              </div>
              <div className="quote-total">
                <span>实付金额</span>
                <b>¥ {money(quote.total_cents)}</b>
              </div>
            </>
          ) : (
            <span className="muted">规格确认后计算实付金额</span>
          )}
        </div>
        <ErrorNote error={error} />
        {!readOnly &&
          (confirm ? (
            <div className="confirm-box">
              <h3>确认模拟下单</h3>
              <p>
                这是演示订单，不会产生真实扣款。下单后直接设为已签收，方便体验退换货。
              </p>
              <button
                className="button primary full"
                disabled={busy}
                onClick={() => void buy()}
              >
                {busy ? <Spinner /> : <PackageCheck size={18} />}确认下单 · ¥{" "}
                {money(quote?.total_cents || 0)}
              </button>
              <button
                className="button full"
                onClick={() => setConfirm(false)}
                disabled={busy}
              >
                返回修改
              </button>
            </div>
          ) : (
            <button
              className="button primary full"
              disabled={loading || !quote || !sku?.stock}
              onClick={() => setConfirm(true)}
            >
              {!sku?.stock ? "暂时缺货" : "模拟下单"}
              <ArrowRight size={18} />
            </button>
          ))}
        {!readOnly && (
          <div className="detail-links">
            <button className="text-link" onClick={() => onConsult(product)}>
              问问店铺客服
              <ChevronRight size={17} />
            </button>
            <button className="text-link" onClick={() => onCompare(product)}>
              {compareIDs.includes(product.id)
                ? "从对比中移除"
                : "加入商品对比"}
              <Plus size={16} />
            </button>
          </div>
        )}
        <div className="detail-section">
          <h3>商品规格</h3>
          <div className="spec-list">
            {Object.entries(product.specs).map(([k, v]) => (
              <div key={k}>
                <span>{k}</span>
                <span>{formatSpec(v)}</span>
              </div>
            ))}
          </div>
        </div>
        {product.size_chart && (
          <div className="detail-section">
            <h3>尺码参考</h3>
            <div className="spec-list">
              {Object.entries(product.size_chart).map(([k, v]) => (
                <div key={k}>
                  <span>{k}</span>
                  <span>{formatSpec(v)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="detail-section">
          <h3>包装清单</h3>
          <p>{product.package_contents.join(" · ")}</p>
        </div>
        <div className="detail-section">
          <h3>常见问题</h3>
          {product.faq.map((f) => (
            <details className="faq" key={f.q}>
              <summary>{f.q}</summary>
              <p>{f.a}</p>
            </details>
          ))}
        </div>
        <p className="source-note">
          虚拟商品 · 规格参考公开资料，定价与库存为演示设定。
          {product.source_url && (
            <a href={product.source_url} target="_blank" rel="noreferrer">
              查看资料来源
            </a>
          )}
        </p>
      </div>
    </Modal>
  );
}
function formatSpec(value: unknown): string {
  if (Array.isArray(value)) return value.map(formatSpec).join(" / ");
  if (value && typeof value === "object")
    return Object.entries(value)
      .map(([k, v]) => `${k} ${formatSpec(v)}`)
      .join(" · ");
  return String(value ?? "—");
}
export function StockLine({ sku }: { sku: SKU }) {
  return (
    <p className={`stock-line ${sku.stock ? "" : "out"}`}>
      <span className="status-dot" />
      {sku.stock > 0
        ? `有货，当前 ${sku.stock} 件`
        : sku.restock_at
          ? `预计 ${date(sku.restock_at)} 补货，以实际到货为准`
          : "暂时缺货，补货时间尚未确定"}
    </p>
  );
}
export function OrdersDrawer({
  orders,
  onClose,
  onApply,
  onConsult,
  loadState,
  onRetry,
}: {
  orders: Order[];
  onClose: () => void;
  onApply: (o: Order) => void;
  onConsult: (o: Order) => void;
  loadState?: ResourceState;
  onRetry?: () => void;
}) {
  return (
    <Modal
      title="我的订单"
      subtitle="演示订单，可直接体验咨询和售后"
      onClose={onClose}
    >
      <div className="drawer-body">
        <ResourceNotice label="订单" state={loadState} onRetry={onRetry} />
        {orders.length ? (
          orders.map((o) => (
            <article className="order-card" key={o.id}>
              <div className="row-between">
                <small>{o.id}</small>
                <span className="badge neutral">
                  {o.status === "delivered" ? "已签收" : "配送中"}
                </span>
              </div>
              <div className="order-product">
                <img src={o.image} alt={o.product_name} />
                <div>
                  <h3>{o.product_name}</h3>
                  <p>
                    {o.sku_label} · ×{o.quantity}
                  </p>
                  <b>实付 ¥ {money(o.total_cents)}</b>
                </div>
              </div>
              <p className="muted small">
                {o.status === "delivered"
                  ? `${date(o.delivered_at, true)} 签收`
                  : `${date(o.created_at, true)} 下单`}
              </p>
              <div className="row-actions">
                <button className="button" onClick={() => onConsult(o)}>
                  咨询订单
                </button>
                <button
                  className="button primary"
                  onClick={() => onApply(o)}
                  disabled={o.remaining_quantity <= 0}
                >
                  申请售后
                </button>
              </div>
            </article>
          ))
        ) : (!loadState || (loadState.loaded && !loadState.error)) ? (
          <Empty title="还没有订单">
            <span>先逛逛喜欢的商品，有问题随时找店铺客服。</span><br />
            <button className="button primary" onClick={onClose}>去逛商品<ArrowRight size={16} /></button>
          </Empty>
        ) : null}
      </div>
    </Modal>
  );
}
export function Comparison({
  products,
  onClose,
  onConsult,
}: {
  products: Product[];
  onClose: () => void;
  onConsult: () => void;
}) {
  return (
    <Modal
      title="好物对比"
      subtitle="看看哪一款更适合你"
      onClose={onClose}
      wide
    >
      <div
        className="drawer-body compare-grid"
        style={{
          gridTemplateColumns: `repeat(${products.length},minmax(160px,1fr))`,
        }}
      >
        {products.map((p) => (
          <div className="compare-item" key={p.id}>
            <img src={p.image} alt={p.name} />
            <h3>{p.name}</h3>
            <b className="price">¥{money(p.price_cents)}</b>
            <p>{p.description}</p>
            {Object.entries(p.specs).map(([key, v]) => (
              <div className="compare-spec" key={key}>
                <small>{key}</small>
                <span>{formatSpec(v)}</span>
              </div>
            ))}
            <p className="stock-line">
              <Check size={16} />
              {p.skus.filter((s) => s.stock > 0).length} 种规格有货
            </p>
          </div>
        ))}
      </div>
      <div className="drawer-footer">
        <button className="button primary full" onClick={onConsult}>
          请客服帮我比较
          <ArrowRight size={18} />
        </button>
      </div>
    </Modal>
  );
}
