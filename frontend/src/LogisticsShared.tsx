import { requestID } from "./api";
export type LocalTarget = { type: string; id: string };
export type LocalOperator = { id: string; name: string; active: boolean };
export type LocalOperations = { version: number; assignee: LocalOperator | null; next_step: string; staff_due_at: string | null; platform_deadline: { due_at: string; source_note: string; source_label: string } | null; customer_wait: { waiting: boolean; reason: string }; history: Array<{ id: string; action_label: string; reason: string; at: string; actor: { name: string }; before: Record<string, unknown>; after: Record<string, unknown> }> };
export type LocalItem = LocalTarget & { title: string; user_name: string; user_id: string; order_id: string | null; conversation_id: string | null; source_kind?: string; status?: string; operations: LocalOperations };
export type LocalHistory = { id: string; reason: string; at: string; actor: { name: string }; before: Record<string, unknown> | null; after: Record<string, unknown> };
export function fullDate(value?: string | null) { return value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "未登记"; }
export function chinaInput(value?: string | null) {
  if (!value) return "";
  const dt = new Date(value); const parts = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(dt);
  return parts.replace(" ", "T");
}
export function chinaISO(value: string) { return value ? new Date(`${value}:00+08:00`).toISOString() : null; }
export function submission(userID: string, operation: string, data: unknown) {
  const signature = JSON.stringify([userID, operation, data]); const storageKey = "local-operations-submissions-v1";
  let rows: [string, string][] = [];
  try { const parsed: unknown = JSON.parse(sessionStorage.getItem(storageKey) || "[]"); if (Array.isArray(parsed)) rows = parsed.filter((v): v is [string, string] => Array.isArray(v) && v.length === 2 && v.every(x => typeof x === "string")); } catch { /* Local retry cache is optional. */ }
  const old = rows.find(v => v[0] === signature); if (old) return old[1];
  const id = requestID(); try { sessionStorage.setItem(storageKey, JSON.stringify([...rows.slice(-99), [signature, id]])); } catch { /* The live form retains its payload. */ }
  return id;
}
export function finishSubmission(userID: string, operation: string, data: unknown) {
  const signature = JSON.stringify([userID, operation, data]); const storageKey = "local-operations-submissions-v1";
  try {
    const parsed: unknown = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (Array.isArray(parsed)) sessionStorage.setItem(storageKey, JSON.stringify(parsed.filter(v => !Array.isArray(v) || v[0] !== signature)));
  } catch { /* Saving a receipt never depends on an optional retry cache. */ }
}
const valueLabels: Record<string, string> = { no_error: "无错误", error: "有错误", unknown: "无法判断", factual_error: "事实错误", policy_error: "政策错误", missed_intent: "漏诉求", repeated_question: "客服重复追问", poor_empathy: "共情不足", unclear_next_step: "下一步不清楚", wrong_promise: "错误承诺" };
const fieldLabels: Record<string, string> = { minutes: "实际分钟", work_date: "工作日期", description: "工作内容", active: "有效记录", outcome: "抽查结论", outcome_label: "结论", tags: "错误标签", reason: "原因", completed_at: "抽查完成时间", updated_at: "最近登记时间", version: "版本", target: "关联事项", source_message_id: "原顾客消息", staff_due_at: "员工跟进时间", platform_deadline: "平台要求时间", assignee: "负责人", next_step: "下一步", customer_wait: "等待顾客" };
export function HistoryList({ items }: { items: LocalHistory[] }) {
  return <details className="local-history"><summary>登记与更正历史 · {items.length} 次</summary>{items.map(h => <article key={h.id}><b>{h.actor.name} · {fullDate(h.at)}</b><p>{h.reason}</p><div>{Object.entries(fieldLabels).flatMap(([key, label]) => {
    const before = h.before?.[key], after = h.after[key]; if (JSON.stringify(before) === JSON.stringify(after) || after === undefined) return [];
    const display = (v: unknown): string => v === true ? "有效" : v === false ? "已撤销" : v === null || v === undefined ? "未登记" : Array.isArray(v) ? v.map(x => valueLabels[String(x)] || String(x)).join("、") : typeof v === "object" ? (v as { name?: string; title?: string }).name || (v as { title?: string }).title || "已保留详情" : valueLabels[String(v)] || String(v);
    return <p key={key}>{label}：{display(before)} → {display(after)}</p>;
  })}</div></article>)}</details>;
}
export function LocalMessage({ content, at, name }: { content: string; at: string; name?: string }) { return <div className="local-message"><small>{name || "原始消息"} · {fullDate(at)}</small><blockquote>{content}</blockquote></div>; }
