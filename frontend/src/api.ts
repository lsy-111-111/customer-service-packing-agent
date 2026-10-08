import type { Session } from "./types";
const KEY = "daily-good-session-v1";
export class APIError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "APIError";
  }
}
export function getSession(): Session | null {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || "null");
  } catch {
    return null;
  }
}
export function setSession(session: Session | null) {
  if (session) sessionStorage.setItem(KEY, JSON.stringify(session));
  else sessionStorage.removeItem(KEY);
}
export function requestID() {
  return crypto.randomUUID();
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  const session = getSession();
  if (session) headers.set("Authorization", `Bearer ${session.token}`);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...options, headers }).catch(
    () => {
      throw new APIError(
        "暂时无法连接店铺，请稍后重试；已提交的记录会保留。",
        0,
      );
    },
  );
  if (!response.ok) {
    let detail = "暂时无法完成，请稍后重试";
    try {
      const data = await response.json();
      detail =
        typeof data.detail === "string" ? data.detail : "请检查填写内容后重试";
    } catch {
      /* response has no JSON */
    }
    throw new APIError(detail, response.status);
  }
  return response.json();
}
export const post = <T>(path: string, data: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(data) });
export const patch = <T>(path: string, data: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(data) });
export const put = <T>(path: string, data: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(data) });
export async function imageBlob(id: string) {
  const token = getSession()?.token;
  const r = await fetch(`/api/attachments/${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!r.ok) throw new Error("图片暂不可用");
  return URL.createObjectURL(await r.blob());
}
export const money = (cents: number) =>
  new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(
    (cents || 0) / 100,
  );
export const date = (value: string, full = false) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        ...(full ? { hour: "2-digit", minute: "2-digit" } : {}),
      })
    : "—";
export const time = (value: string) =>
  value
    ? new Date(value).toLocaleTimeString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";

export type TurnEvent = {
  type: string;
  phase?: string;
  text?: string;
  content?: string;
  answer?: string;
  error?: string;
};
export async function streamTurn(
  id: string,
  signal: AbortSignal,
  onEvent: (event: TurnEvent) => void,
) {
  const response = await fetch(`/api/turns/${encodeURIComponent(id)}/events`, {
    headers: {
      Authorization: `Bearer ${getSession()?.token}`,
      Accept: "text/event-stream",
    },
    signal,
  });
  if (!response.ok || !response.body) throw new Error("实时连接暂不可用");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        for (const line of block.split("\n"))
          if (line.startsWith("data:")) {
            try {
              onEvent(JSON.parse(line.slice(5).trim()));
            } catch {
              /* ignore incomplete event payload */
            }
          }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
