import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, Plus, Search } from "lucide-react";
import { APIError, post } from "./api";
import type { User } from "./types";
import { ErrorNote, Modal, Spinner } from "./ui";

export function CustomerPicker({ personas, loading, listLoading, onLogin, onCreated, onRefresh, onError }: {
  personas: User[];
  loading: boolean;
  listLoading: boolean;
  onLogin: (customer: User) => Promise<void>;
  onCreated: (customer: User) => void;
  onRefresh: () => void;
  onError: (message: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [created, setCreated] = useState<User | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const nameInput = useRef<HTMLInputElement>(null);
  const trimmedName = name.trim();
  const nameLength = Array.from(trimmedName).length;
  const customers = personas.filter(p => p.role === "customer" && p.name.toLowerCase().includes(search.trim().toLowerCase()));
  const disabled = loading || busy;

  useEffect(() => {
    if (!formOpen) return;
    const frame = requestAnimationFrame(() => nameInput.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [formOpen]);

  function openForm() {
    setName("");
    setError("");
    setCreated(null);
    setFormOpen(true);
  }

  async function enter(customer: User) {
    if (busyRef.current || loading) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await onLogin(customer);
    } catch (failure) {
      onError((failure as Error).message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busyRef.current || loading) return;
    if (!created && (nameLength < 1 || nameLength > 20)) {
      setError("顾客名字需要 1–20 个字符");
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setError("");
    let customer = created;
    try {
      if (!customer) {
        customer = await post<User>("/customers", { name: trimmedName });
        setCreated(customer);
        onCreated(customer);
      }
      await onLogin(customer);
    } catch (failure) {
      const message = (failure as Error).message;
      setError(customer
        ? `顾客已创建，暂时无法进入店铺。${message}`
        : failure instanceof APIError && failure.status === 0
          ? `${message} 可返回列表搜索这个名字，确认是否已添加。`
          : message);
      if (!customer) onRefresh();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  return <>
    <div className="customer-tools">
      <label className="customer-search">
        <Search size={17} aria-hidden="true" />
        <input type="search" aria-label="搜索顾客" placeholder="搜索顾客" value={search} onChange={e => setSearch(e.target.value)} />
      </label>
      <button className="button customer-add" onClick={openForm} disabled={disabled}>
        <Plus size={17} aria-hidden="true" />新增顾客
      </button>
    </div>
    <div className="persona-list customer-list" aria-label="顾客列表" aria-busy={listLoading}>
      {customers.map(p => <button className="persona" key={p.id} onClick={() => void enter(p)} disabled={disabled}>
        <span className="avatar" aria-hidden="true">{p.id.startsWith("customer_") ? p.avatar : p.name}</span>
        <span>
          <b>{p.name}{p.id === "customer-xu" && <span className="new-customer-label">新顾客</span>}</b>
          <small>{p.description}</small>
        </span>
        <ArrowRight size={20} aria-hidden="true" />
      </button>)}
      {!customers.length && <p className="customer-list-empty" role="status">
        {listLoading ? "正在读取顾客列表…" : search.trim() ? "没有找到这位顾客，可以换个名字搜索或新增顾客。" : "暂无可显示的顾客，你可以新增一位。"}
      </p>}
    </div>
    {formOpen && <Modal title="新增顾客" subtitle="取一个喜欢的名字，从第一次逛店开始。" onClose={() => { if (!busyRef.current) setFormOpen(false); }}>
      <form className="customer-create-form" onSubmit={event => void submit(event)} aria-busy={busy}>
        <div className="drawer-body">
          <div className="form-field">
            <label htmlFor="customer-name">顾客名字</label>
            <input ref={nameInput} id="customer-name" name="customer-name" autoComplete="off" placeholder="输入你取的名字" value={name} readOnly={!!created} disabled={busy} required aria-describedby="customer-name-help" onChange={e => { setName(e.target.value); setError(""); }} />
            <small id="customer-name-help" className={nameLength > 20 ? "customer-name-invalid" : "muted"}>
              {nameLength > 20 ? "名字最多 20 个字符" : "支持中文、英文和数字，同名顾客不可重复添加"} · {nameLength}/20
            </small>
          </div>
          <p className="customer-create-note">新顾客没有购买或聊天记录。之后的订单、聊天和售后会单独保存，下次回来仍然可以选择。</p>
          {created && <p className="customer-created-note" role="status">「{created.name}」已添加，重试会直接进入店铺。</p>}
          <ErrorNote error={error} />
        </div>
        <div className="customer-create-actions">
          <button type="button" className="button" disabled={busy} onClick={() => setFormOpen(false)}>{created ? "返回列表" : "取消"}</button>
          <button type="submit" className="button primary" disabled={disabled || (!created && (!nameLength || nameLength > 20))}>
            {busy && <Spinner />}{busy ? "正在进入…" : created ? "重试进入" : "创建并进入"}
          </button>
        </div>
      </form>
    </Modal>}
  </>;
}
