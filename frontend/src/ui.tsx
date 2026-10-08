import { useEffect, useRef, useState, type ReactNode } from "react";
import { X, ArrowUpRight, LoaderCircle, ImagePlus, Trash2 } from "lucide-react";
import { api, getSession, imageBlob } from "./api";
import type { ResourceState } from "./types";
export function ResourceNotice({ label, state, onRetry }: { label: string; state?: ResourceState; onRetry?: () => void }) {
  if (!state || (state.loaded && !state.error)) return null;
  return <div className={`resource-notice ${state.error ? "resource-error" : ""}`} role={state.error ? "alert" : "status"} data-resource={label}>
    <span>{state.error ? <><b>{label}{state.loaded ? "更新失败" : "加载失败"}</b><small>{state.error}{state.loaded ? " 当前显示上次成功加载的资料。" : ""}</small></> : <><Spinner />正在加载{label}</>}</span>
    {state.error && onRetry && <button className="button" disabled={state.loading} onClick={onRetry} aria-label={`重试${label}`}>{state.loading ? <><Spinner />重试中</> : "重试"}</button>}
  </div>;
}
export function Spinner() {
  return <LoaderCircle size={17} className="spin" />;
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-mark">
        日常
        <br />
        好物
      </span>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function ErrorNote({ error }: { error: string }) {
  return error ? (
    <div className="error-note" role="alert">
      {error}
    </div>
  ) : null;
}
export function Modal({
  title,
  subtitle,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const el = ref.current;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
      if (e.key === "Tab" && el) {
        const list = el.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
        );
        const first = list[0],
          last = list[list.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    el?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`drawer ${wide ? "drawer-wide" : ""}`}
      >
        <div className="drawer-head">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <X />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
export function Photo({
  id,
  onClick,
}: {
  id: string;
  onClick?: (url: string) => void;
}) {
  const [src, setSrc] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let url = "";
    imageBlob(id)
      .then((v) => {
        url = v;
        if (active) setSrc(v);
        else URL.revokeObjectURL(v);
      })
      .catch(() => setFailed(true));
    return () => {
      active = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [id]);
  return src ? (
    <button
      className="photo-button"
      onClick={() =>
        onClick ? onClick(src) : window.open(src, "_blank", "noopener")
      }
    >
      <img src={src} alt="顾客上传的图片" />
    </button>
  ) : (
    <span className="photo-loading">
      {failed ? "图片暂不可用" : <Spinner />}
    </span>
  );
}
export function Uploader({
  ids,
  onChange,
  onError,
  disabled = false,
}: {
  ids: string[];
  onChange: (ids: string[]) => void;
  onError: (message: string) => void;
  disabled?: boolean;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const lifecycle = useRef({ mounted: false, generation: 0, controller: null as AbortController | null });
  useEffect(() => {
    const current = lifecycle.current;
    current.mounted = true;
    current.generation += 1;
    return () => {
      current.mounted = false;
      current.generation += 1;
      current.controller?.abort();
      current.controller = null;
    };
  }, []);
  async function upload(files: FileList | null) {
    const token = getSession()?.token;
    if (!files?.length || !token || !lifecycle.current.mounted || disabled || lifecycle.current.controller) return;
    if (files.length + ids.length > 3) {
      onError("每条消息最多上传 3 张图片");
      return;
    }
    const controller = new AbortController();
    const generation = ++lifecycle.current.generation;
    lifecycle.current.controller = controller;
    const current = () => lifecycle.current.mounted && lifecycle.current.generation === generation &&
      !controller.signal.aborted && getSession()?.token === token;
    setUploading(true);
    try {
      const added: string[] = [];
      for (const file of Array.from(files)) {
        if (!current()) return;
        if (!["image/jpeg", "image/png", "image/webp"].includes(file.type))
          throw new Error("请使用 JPEG、PNG 或 WebP 图片");
        if (file.size > 10 * 1024 * 1024) throw new Error("每张图片最多 10 MB");
        const form = new FormData();
        form.append("file", file);
        const response = await api<{ id: string }>("/attachments", {
          method: "POST",
          body: form,
          signal: controller.signal,
        });
        if (!current()) return;
        added.push(response.id);
      }
      if (!current()) return;
      onChange([...ids, ...added]);
      onError("");
    } catch (error) {
      if (current()) onError((error as Error).message);
    } finally {
      if (current()) {
        setUploading(false);
        if (fileRef.current) fileRef.current.value = "";
      }
      controller.abort();
      if (lifecycle.current.controller === controller) lifecycle.current.controller = null;
    }
  }
  return (
    <>
      <input
        ref={fileRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        multiple
        hidden
        onChange={(e) => void upload(e.target.files)}
      />
      <button
        type="button"
        className="icon-button upload-button"
        onClick={() => fileRef.current?.click()}
        disabled={disabled || uploading || ids.length >= 3}
        title="上传商品照片，最多3张"
        aria-label="上传图片"
      >
        {uploading ? <Spinner /> : <ImagePlus size={23} />}
      </button>
      {ids.length > 0 && (
        <div className="upload-previews">
          {ids.map((id) => (
            <div className="upload-thumb" key={id}>
              <Photo id={id} />
              <button
                type="button"
                onClick={() => onChange(ids.filter((v) => v !== id))}
                aria-label="移除图片"
              >
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
export function TextLink({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="text-link" onClick={onClick}>
      {children}
      <ArrowUpRight size={16} />
    </button>
  );
}
