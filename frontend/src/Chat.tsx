import { useEffect, useRef, useState } from "react";
import {
  ArrowUpRight,
  CheckCircle2,
  ChevronRight,
  Headphones,
  Send,
  X,
} from "lucide-react";
import type {
  Assessment,
  Card,
  Case,
  ConversationData,
  Message,
  Product,
  User,
} from "./types";
import { money, streamTurn, time } from "./api";
import { AssessmentPanel, CaseCard } from "./Cases";
import { Empty, ErrorNote, Photo, Spinner, Uploader } from "./ui";
import { ProductLink, ProductLinks } from "./ProductLinks";
import { safeProductLink } from "./catalogLinks";
export function Chat({
  data,
  user,
  products,
  onSend,
  onHandoff,
  onResume,
  onProduct,
  onCase,
  onOrders,
  onRetry,
  onClearContext,
  sending,
  visible = true,
  onClose,
  switchingMode = false,
}: {
  data: ConversationData | null;
  user: User;
  products: Product[];
  onSend: (content: string, attachments: string[]) => Promise<void>;
  onHandoff: () => void;
  onResume: () => void;
  onProduct: (p: Product) => void;
  onCase: (c: Case) => void;
  onOrders: () => void;
  onRetry: (turnID: string) => void;
  onClearContext: () => void;
  sending: boolean;
  visible?: boolean;
  onClose?: () => void;
  switchingMode?: boolean;
}) {
  const [stream, setStream] = useState({ id: "", phase: "", text: "" });
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const staff = user.role === "staff";
  const mode = data?.conversation.mode;
  const pending = data?.active_turn?.status === "pending";
  const disabled = sending || pending || (staff && mode !== "human");
  const selected = products.find(
    (p) => p.id === data?.conversation.selected_product_id,
  );
  const scrollKey = data?.messages.map((m) => `${m.id}-${m.status}`).join("|");
  const pendingID =
    data?.active_turn?.status === "pending" ? data.active_turn.id : "";
  useEffect(() => {
    if (!pendingID) return;
    const controller = new AbortController();
    setStream({ id: pendingID, phase: "", text: "" });
    void streamTurn(pendingID, controller.signal, (event) => {
      if (event.type === "status")
        setStream((v) => ({ ...v, phase: event.phase || "" }));
      if (event.type === "delta")
        setStream((v) => ({
          ...v,
          text: v.text + (event.text || event.content || ""),
        }));
      if (event.type === "done" || event.type === "error")
        window.dispatchEvent(new Event("daily-turn-finished"));
    }).catch(() => {
      /* conversation polling remains active on a stream disconnect */
    });
    return () => controller.abort();
  }, [pendingID]);
  useEffect(() => {
    if (visible) bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [scrollKey, pending, stream.text, visible]);
  useEffect(() => {
    if (visible && !staff) composer.current?.focus({ preventScroll: true });
  }, [visible, staff]);
  async function send(content = text) {
    if ((!content.trim() && !attachments.length) || disabled) return;
    setError("");
    try {
      await onSend(content.trim(), attachments);
      setText("");
      setAttachments([]);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <section className={`chat-panel ${staff ? "staff-chat" : ""}`}>
      <header className="chat-header">
        <div>
          <h2>
            {staff
              ? `与${data?.conversation.user_name || "顾客"}的对话`
              : "店铺客服"}
          </h2>
          <p>
            <span
              className={`status-dot ${mode === "waiting_human" ? "waiting" : ""}`}
            />
            {mode === "human"
              ? "人工客服已接入"
              : mode === "waiting_human"
                ? "已转交人工，等待接入"
                : staff
                  ? "当前由智能客服接待"
                  : "智能客服 · 可转人工"}
          </p>
        </div>
        <div className="chat-header-actions">
        {staff ? (
          <button
            className={`button ${mode === "human" ? "" : "outline-dark"}`}
            onClick={mode === "human" ? onResume : onHandoff}
            disabled={!data || switchingMode}
          >
            {mode === "human" ? "交回智能客服" : "接管对话"}
          </button>
        ) : (
          <button
            className="button"
            onClick={mode === "ai" ? onHandoff : onResume}
            disabled={!data || switchingMode}
          >
            <Headphones size={16} />
            {switchingMode ? "正在切换" : mode === "human"
              ? "切回智能客服"
              : mode === "waiting_human"
                ? "取消等待，回到智能客服"
                : "转人工"}
          </button>
        )}
        {onClose && <button className="icon-button chat-close" onClick={onClose} aria-label="收起客服"><X size={20} /></button>}
        </div>
      </header>
      {!staff && (
        <div className="quick-actions">
          <button onClick={() => setText("想选一件适合我的商品，帮我看看。")}>
            帮我选商品
          </button>
          <button
            onClick={() =>
              setText(
                selected
                  ? `${selected.name}现在有什么优惠？`
                  : "店里现在有什么优惠？",
              )
            }
          >
            查优惠
          </button>
          <button onClick={onOrders}>申请售后</button>
        </div>
      )}
      {selected && !staff && (
        <div className="context-strip">
          <img src={selected.image} alt="" />
          <button onClick={() => onProduct(selected)}>
            正在咨询：{selected.name}
            <ChevronRight size={14} />
          </button>
          <button
            className="icon-button"
            onClick={onClearContext}
            aria-label="清除当前商品"
          >
            <X size={14} />
          </button>
        </div>
      )}
      <div className="messages" aria-live="polite" aria-relevant="additions">
        {!data ? (
          <Empty title={staff ? "选择一段对话" : "正在连接店铺客服"}>
            {staff
              ? "从左侧选择顾客，查看对话和售后资料。"
              : "稍等片刻，我们准备好了就来。"}
          </Empty>
        ) : data.messages.length ? (
          data.messages.map((message) => (
            <ChatMessage
              key={message.id}
              message={message}
              user={user}
              customerName={data.conversation.user_name}
              products={products}
              preferenceVersion={data.preferences?.version}
              cases={data.cases}
              onProduct={onProduct}
              onCase={onCase}
              onOrders={onOrders}
              onRetry={onRetry}
            />
          ))
        ) : (
          <Empty title="今天想找点什么？">
            告诉我你的预算和用途，也可以直接发一张商品照片。
          </Empty>
        )}
        {pending && (
          <div className="message-row incoming">
            <span className="brand-avatar">
              日常
              <br />
              好物
            </span>
            <div className="message-main">
              <div className="progress-message">
                <Spinner />
                <span>
                  {(stream.id === pendingID && stream.phase) ||
                    data?.active_turn?.phase ||
                    "正在了解你的需求"}
                </span>
              </div>
              {stream.id === pendingID && stream.text && (
                <div className="message-bubble streaming-copy">
                  <RichText text={stream.text} />
                </div>
              )}
            </div>
          </div>
        )}
        {data?.active_turn?.status === "failed" && (
          <div className="turn-error">
            <p>{data.active_turn.error || "本次回复暂时中断，消息已保存。"}</p>
            <button
              className="text-link"
              onClick={() => onRetry(data.active_turn!.id)}
            >
              重新回复
              <ArrowUpRight size={14} />
            </button>
          </div>
        )}
        <div ref={bottom} />
      </div>
      <div className="composer-wrap">
        <ErrorNote error={error} />
        {staff && mode !== "human" && (
          <p className="composer-hint">接管后可直接回复顾客</p>
        )}
        <form
          className={`composer ${staff ? "staff-composer" : ""}`}
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          {!staff && (
            <Uploader
              ids={attachments}
              onChange={setAttachments}
              onError={setError}
              disabled={sending}
            />
          )}
          <textarea
            ref={composer}
            rows={staff ? 2 : 1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={
              staff
                ? mode === "human"
                  ? "回复顾客，Enter 发送"
                  : "接管后可直接回复顾客"
                : "说说你的需求，或上传商品照片"
            }
            aria-label="消息内容"
            disabled={staff && mode !== "human"}
            maxLength={6000}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <button
            className="button primary send-button"
            aria-label="发送"
            type="submit"
            disabled={disabled || (!text.trim() && !attachments.length)}
          >
            {sending ? (
              <Spinner />
            ) : (
              <>
                <span>发送</span>
                <Send size={17} />
              </>
            )}
          </button>
        </form>
        {staff ? (
          <p className="composer-footnote">本地演示 · 操作仅更新演示记录</p>
        ) : (
          <p className="composer-footnote">
            库存、优惠实时核对 · 售后由人工最终确认
          </p>
        )}
      </div>
    </section>
  );
}
function ChatMessage({
  message,
  user,
  customerName,
  products,
  preferenceVersion,
  cases,
  onProduct,
  onCase,
  onOrders,
  onRetry,
}: {
  message: Message;
  user: User;
  customerName: string;
  products: Product[];
  preferenceVersion?: number;
  cases: Case[];
  onProduct: (p: Product) => void;
  onCase: (c: Case) => void;
  onOrders: () => void;
  onRetry: (id: string) => void;
}) {
  const staff = user.role === "staff";
  const customer = message.role === "user";
  const outgoing = staff ? !customer : customer;
  if (message.role === "system")
    return <div className="system-message">{message.content}</div>;
  return (
    <div className={`message-row ${outgoing ? "outgoing" : "incoming"}`}>
      <span className={customer ? "avatar" : "brand-avatar"}>
        {customer ? (
          customerName
        ) : message.role === "staff" ? (
          "客服"
        ) : staff ? (
          "智"
        ) : (
          <>
            日常
            <br />
            好物
          </>
        )}
      </span>
      <div className="message-main">
        {staff && (
          <div className="message-label">
            {customer
              ? customerName
              : message.role === "staff"
                ? "人工客服"
                : "智能客服"}{" "}
            <time>{time(message.created_at)}</time>
          </div>
        )}
        <div
          className={`message-bubble ${message.cards?.length ? "with-cards" : ""}`}
        >
          {message.attachments?.length > 0 && (
            <div className="message-photos">
              {message.attachments.map((id) => (
                <Photo key={id} id={id} />
              ))}
            </div>
          )}
          {message.content && (
            <div className="message-copy">
              <RichText text={message.content} />
            </div>
          )}
          {message.cards?.map((card, i) => (
            <BusinessCard
              key={i}
              card={cardAtPreferenceVersion(card, preferenceVersion)}
              products={products}
              cases={cases}
              onProduct={onProduct}
              onCase={onCase}
              onOrders={onOrders}
            />
          ))}
        </div>
        {!staff && (
          <time className="message-time">{time(message.created_at)}</time>
        )}
        {message.status === "failed" && message.turn_id && (
          <button
            className="text-link retry-link"
            onClick={() => onRetry(message.turn_id!)}
          >
            回复中断，点击重试
          </button>
        )}
      </div>
    </div>
  );
}
function BusinessCard({
  card,
  products,
  cases,
  onProduct,
  onCase,
  onOrders,
}: {
  card: Card;
  products: Product[];
  cases: Case[];
  onProduct: (p: Product) => void;
  onCase: (c: Case) => void;
  onOrders: () => void;
}) {
  if (card.type === "assessment") return <AssessmentPanel assessment={card.data as unknown as Assessment} />;
  if (card.type === "product") {
    const p =
      products.find((v) => v.id === card.data.id) ||
      (card.data as unknown as Product);
    if (!p.id) return null;
    return (
      <div className="chat-product">
        <img src={p.image} alt={p.name} />
        <div>
          <h4>{p.name}</h4>
          {card.selection?.purchase_allowed !== false && <ProductLinks product={p} skuID={String(card.data.selected_sku_id || card.selection?.eligible_sku_ids[0] || p.skus?.[0]?.id || "") || undefined} />}
          <CardSelectionNotice card={card} />
          {card.selection?.purchase_allowed === false && <details className="card-comparison-details">
            <summary>查看对比资料</summary>
            {Object.entries(p.specs || {}).map(([key, value]) => <small key={key}>{key}：{String(value)}</small>)}
          </details>}
          <div className="row-between">
            <b>¥ {money(p.price_cents)}</b>
            {card.selection?.purchase_allowed !== false && (
              <button className="button primary" onClick={() => onProduct(cardProduct(p, card))}>
                {card.selection?.label ? "查看已核实规格" : "查看商品"}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }
  if (card.type === "case") {
    const c =
      cases.find((v) => v.id === card.data.id) ||
      (card.data as unknown as Case);
    return c.id ? <CaseCard item={c} onClick={() => onCase(c)} /> : null;
  }
  if (card.type === "quote") {
    return (
      <div className="quote-card">
        <div className="row-between">
          <span>{card.selection?.quote_verified === false ? "报价资料待核实" : "已核对优惠"}</span>
          <strong>¥ {money(Number(card.data.total_cents || 0))}</strong>
        </div>
        <small>
          {String(card.data.promotion_label || "实际优惠以确认下单时为准")}
        </small>
        <CardSelectionNotice card={card} />
        {card.selection?.purchase_allowed !== false && <button
          className="text-link"
          onClick={() => {
            const p = products.find((p) =>
              p.skus.some((s) => s.id === card.data.sku_id),
            );
            if (p) onProduct({ ...cardProduct(p, card), selected_sku_id: String(card.data.sku_id) });
          }}
        >
          {card.selection?.label ? "查看规格" : "查看规格并确认"}
          <ChevronRight size={15} />
        </button>}
      </div>
    );
  }
  if (card.type === "order") {
    return (
      <button className="order-chat-card" onClick={onOrders}>
        <PackageIcon />
        <span>
          <b>{String(card.data.product_name || "订单详情")}</b>
          <small>{String(card.data.id || "")} · 查看订单</small>
        </span>
        <ChevronRight size={17} />
      </button>
    );
  }
  if (card.type === "facts") {
    const raw =
      card.data.summary || card.data.text || card.data.label || card.data.facts;
    const lines = Array.isArray(raw)
      ? raw
          .map((v) => (typeof v === "string" ? v : JSON.stringify(v)))
          .join(" · ")
      : typeof raw === "string"
        ? raw
        : "已核对商品资料与店铺信息";
    return (
      <div className="verified-facts">
        <CheckCircle2 size={17} />
        <span>{lines}</span>
      </div>
    );
  }
  return null;
}
function cardProduct(product: Product, card: Card): Product {
  return card.selection ? { ...product, skus: product.skus.filter(s => card.selection!.eligible_sku_ids.includes(s.id)), selected_sku_id: card.selection.eligible_sku_ids[0] } : product;
}
function cardAtPreferenceVersion(card: Card, version?: number): Card {
  if (!['product', 'quote'].includes(card.type) || version === undefined || card.selection?.preference_version === version) return card;
  return { ...card, selection: { status: 'reference', label: '历史资料，未按当前条件重新核实',
    reasons: card.selection?.label ? [card.selection.label, ...card.selection.reasons] : [],
    purchase_allowed: false, eligible_sku_ids: [], preference_version: card.selection?.preference_version,
    quote_verified: card.selection?.quote_verified } };
}
function CardSelectionNotice({ card }: { card: Card }) {
  if (!card.selection?.label) return null;
  return <div className="card-selection-notice" role="note">
    <strong>{card.selection.label}</strong>
    {card.selection.reasons.map(reason => <small key={reason}>{reason}</small>)}
  </div>;
}
function PackageIcon() {
  return (
    <span className="case-mark">
      <CheckCircle2 size={21} />
    </span>
  );
}

function RichText({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|\[[^\]\n]+\]\([^\s)]+\)|https?:\/\/[^\s<>)，。；！？」]+|\/products\/[^\s<>)，。；！？」]+)/g);
  return <>{parts.map((part,i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2,-2)}</strong>;
    const markdown = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    const path = safeProductLink(markdown ? markdown[2] : part);
    if (path && (markdown || part.startsWith("/products/") || /^https?:/.test(part))) return <ProductLink key={i} path={path} label={markdown ? markdown[1] : "查看商品规格"} inline />;
    return markdown ? markdown[1] : part;
  })}</>;
}
