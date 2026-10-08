import { useEffect, useRef, useState } from 'react';
import { Plus, Trash2, Upload } from 'lucide-react';
import { api, date, getSession, post, requestID } from './api';
import type { Product } from './types';
import { ErrorNote, Spinner } from './ui';
import './CatalogManagement.css';

type Variant = {label: string; color: string; size: string; price: string; stock: string};
const blankVariant = (): Variant => ({label:'',color:'',size:'',price:'',stock:'0'});
export function catalogPriceCents(value: string, label = '售价'): number {
  const text = value.trim();
  if (!text) throw new Error(`请填写${label}`);
  const message = `${label}请填写大于零、最多两位小数且不超过100万元的金额`;
  if (!/^\d+(\.\d{1,2})?$/.test(text)) throw new Error(message);
  const [whole, fraction = ''] = text.split('.');
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  if (!Number.isSafeInteger(cents) || cents < 1 || cents > 100_000_000) throw new Error(message);
  return cents;
}
export function catalogStock(value: string): number {
  if (!value.trim()) throw new Error('请填写可售库存');
  const stock = Number(value);
  if (!Number.isSafeInteger(stock) || stock < 0 || stock > 1_000_000) throw new Error('可售库存请填写0至1000000的整数');
  return stock;
}
function validateVariant(value: Variant) {
  if (!value.label.trim()) throw new Error('请填写规格名称');
  catalogPriceCents(value.price);
  catalogStock(value.stock);
}
function VariantFields({value, onChange}: {value:Variant;onChange:(v:Variant)=>void}) {
  return <div className="form-grid">{([['label','规格名称'],['color','颜色（选填）'],['size','尺码或容量（选填）'],['price','售价（元）'],['stock','可售库存']] as const).map(([key,label])=><label className="form-field" key={key}>{label}<input required={['label','price','stock'].includes(key)} type={['price','stock'].includes(key)?'number':'text'} min={key==='price'?0.01:0} step={key==='price'?'0.01':key==='stock'?'1':undefined} value={value[key]} onChange={e=>onChange({...value,[key]:e.target.value})}/></label>)}</div>;
}
export function NewProduct({onSaved,onCancel}:{onSaved:()=>Promise<void>;onCancel:()=>void}) {
  const [base,setBase]=useState({name:'',model:'',category:'家居',description:''});
  const [skus,setSkus]=useState<Variant[]>([blankVariant()]);
  const [error,setError]=useState(''); const [busy,setBusy]=useState(false); const req=useRef(requestID());
  async function save(){setBusy(true);setError('');try{for(const [key,label] of [['name','商品名称'],['category','分类'],['description','商品说明']] as const){if(!base[key].trim())throw new Error(`请填写${label}`);}skus.forEach(validateVariant);await post('/admin/catalog/products',{...base,skus,request_id:req.current});await onSaved();onCancel();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <form noValidate onSubmit={e=>{e.preventDefault();void save();}}><h3>新增商品</h3><p className="muted">填写商品和售价库存，保存后可在店铺里直接查看。此处商品和订单均为演示数据。</p><div className="form-grid">{([['name','商品名称'],['model','型号（选填）'],['category','分类']] as const).map(([key,label])=><label className="form-field" key={key}>{label}<input required={key!=='model'} value={base[key]} onChange={e=>setBase({...base,[key]:e.target.value})}/></label>)}</div><label className="form-field">商品说明<textarea required rows={3} value={base.description} onChange={e=>setBase({...base,description:e.target.value})}/></label>{skus.map((v,i)=><section className="catalog-form-card" key={i}><div className="row-between"><h3>规格 {i+1}</h3>{skus.length>1&&<button type="button" className="icon-button" aria-label={`删除规格${i+1}`} onClick={()=>setSkus(skus.filter((_,n)=>n!==i))}><Trash2 size={16}/></button>}</div><VariantFields value={v} onChange={next=>setSkus(skus.map((s,n)=>n===i?next:s))}/></section>)}<button type="button" className="text-link" onClick={()=>setSkus([...skus,blankVariant()])}><Plus size={16}/>添加规格</button><ErrorNote error={error}/><div className="catalog-actions"><button className="button primary" disabled={busy}>{busy&&<Spinner/>}保存商品</button><button type="button" className="button" disabled={busy} onClick={onCancel}>取消</button></div></form>;
}
export function AddVariant({product,onSaved}:{product:Product;onSaved:()=>Promise<void>}) {
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');const [value,setValue]=useState(blankVariant);const req=useRef(requestID());
  async function save(){setBusy(true);setError('');try{validateVariant(value);await post(`/admin/catalog/products/${product.id}/skus`,{...value,request_id:req.current});await onSaved();setOpen(false);setValue(blankVariant());req.current=requestID();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section className="catalog-form-card">{!open?<button className="text-link" onClick={()=>setOpen(true)}><Plus size={16}/>新增规格</button>:<form noValidate onSubmit={e=>{e.preventDefault();void save();}}><h3>新增规格</h3><VariantFields value={value} onChange={setValue}/><ErrorNote error={error}/><div className="catalog-actions"><button className="button primary" disabled={busy}>{busy&&<Spinner/>}保存规格</button><button type="button" className="button" onClick={()=>setOpen(false)}>取消</button></div></form>}</section>;
}
type Preview={id:string;rows:{row_number:number;values:Record<string,string>;errors:string[]}[];valid_count:number;error_count:number;already_confirmed:boolean};
export function CatalogImport({onSaved}:{onSaved:()=>Promise<void>}) {
  const [preview,setPreview]=useState<Preview|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
  async function download(format:string,current=false){setError('');try{const response=await fetch(`/api/admin/catalog/template?format=${format}&current=${current}`,{headers:{Authorization:`Bearer ${getSession()?.token}`}});if(!response.ok){const body=await response.json();throw new Error(typeof body.detail==='string'?body.detail:'暂时无法下载表格');}const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=`${current?'现有商品资料':'商品导入模板'}.${format}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){setError((e as Error).message);}}
  async function upload(file:File){setBusy(true);setError('');setNotice('');setPreview(null);try{const data=new FormData();data.append('file',file);setPreview(await api<Preview>('/admin/catalog/imports/preview',{method:'POST',body:data}));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function confirm(){if(!preview)return;setBusy(true);setError('');try{const r=await post<{count:number;already_confirmed:boolean}>(`/admin/catalog/imports/${preview.id}/confirm`,{});setPreview({...preview,already_confirmed:true});setNotice(r.already_confirmed?'这份表格已经导入过，没有重复写入。':`已导入 ${r.count} 个规格，店铺资料已经更新。`);await onSaved();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section><h3>表格导入商品</h3><p className="muted">新增商品用“新增”；修改已有规格的价格与库存用“更新”，商品名称、型号、规格名称须与店铺一致。先预览，再确认；有错误时整张表都不会写入。每次最多 500 行。</p><div className="catalog-management-toolbar"><button className="button" onClick={()=>void download('xlsx')}>下载 Excel 模板</button><button className="button" onClick={()=>void download('csv')}>下载 CSV 模板</button><button className="button" onClick={()=>void download('xlsx',true)}>导出现有商品</button></div><label className="form-field">选择填写好的表格<input className="catalog-file-input" type="file" accept=".xlsx,.csv" disabled={busy} onChange={e=>{const file=e.target.files?.[0];if(file)void upload(file);e.target.value='';}}/></label>{busy&&<p><Spinner/>正在处理表格</p>}<ErrorNote error={error}/>{notice&&<p className="success-note" role="status">{notice}</p>}{preview&&<><p>{preview.already_confirmed?'这份表格已导入，不会重复写入。':`可导入 ${preview.valid_count} 行 · 需要修改 ${preview.error_count} 行`}</p><div className="catalog-import-table"><table><thead><tr><th>行号</th><th>操作</th><th>商品 / 规格</th><th>售价（元）</th><th>库存</th><th>核对结果</th></tr></thead><tbody>{preview.rows.map(r=><tr key={r.row_number} className={r.errors.length?'has-error':''}><td>{r.row_number}</td><td>{r.values['操作']}</td><td>{r.values['商品名称']} / {r.values['规格名称']}</td><td>{r.values['售价（元）']}</td><td>{r.values['库存']}</td><td>{r.errors.join('；')||'可以导入'}</td></tr>)}</tbody></table></div><button className="button primary" disabled={busy||!!preview.error_count||preview.already_confirmed} onClick={()=>void confirm()}><Upload size={16}/>确认导入 {preview.valid_count} 个规格</button>{!!preview.error_count&&<p className="muted">请在表格中修改提示的行，然后重新上传。</p>}</>}</section>;
}
type Draft={id:string;product_id:string;product_name:string;status:string;version:number;faq:Product['faq'];before_faq:Product['faq'];submitted_by:string;created_at:string;review_note:string};
export function StandardAnswers({products,onSaved}:{products:Product[];onSaved:()=>Promise<void>}) {
  const [pid,setPid]=useState(products[0]?.id||''),[drafts,setDrafts]=useState<Draft[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
  const p=products.find(v=>v.id===pid);const [faq,setFaq]=useState<Product['faq']>(p?.faq||[]);const [review,setReview]=useState<Draft|null>(null);const [note,setNote]=useState('');const req=useRef(requestID());
  async function load(){try{setDrafts(await api<Draft[]>('/admin/catalog/answers'));}catch(e){setError((e as Error).message);}}
  useEffect(()=>{void load();},[]);
  useEffect(()=>{setFaq(p?.faq||[]);req.current=requestID();},[pid,p?.catalog_version]);
  async function submit(){if(!p)return;setBusy(true);setError('');try{await post(`/admin/catalog/products/${pid}/answers`,{faq,expected_version:p.catalog_version||1,request_id:req.current});setNotice('已提交审核，审核通过后客服才会使用新答案。');req.current=requestID();await load();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function decide(decision:string){if(!review)return;setBusy(true);setError('');try{await post(`/admin/catalog/answers/${review.id}/review`,{decision,note,expected_version:review.version});setNotice(decision==='approve'?'已审核通过，新答案开始生效。':'已退回，现行答案保持不变。');setReview(null);setNote('');await Promise.all([load(),onSaved()]);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section><p className="muted">先编写并提交，再核对前后内容并审核。审核前，客服继续使用已生效的答案。</p><ErrorNote error={error}/>{notice&&<p className="success-note" role="status">{notice}</p>}<label className="form-field">选择商品<select aria-label="选择商品" value={pid} onChange={e=>{setPid(e.target.value);setNotice('');}}>{products.map(v=><option value={v.id} key={v.id}>{v.name}</option>)}</select></label>{faq.map((f,i)=><div className="catalog-form-card" key={i}><div className="row-between"><b>标准答案 {i+1}</b><button className="icon-button" aria-label={`删除答案${i+1}`} onClick={()=>setFaq(faq.filter((_,n)=>n!==i))}><Trash2 size={16}/></button></div><label className="form-field">客户会问什么<input value={f.q} onChange={e=>setFaq(faq.map((v,n)=>n===i?{...v,q:e.target.value}:v))}/></label><label className="form-field">店铺标准答案<textarea rows={3} value={f.a} onChange={e=>setFaq(faq.map((v,n)=>n===i?{...v,a:e.target.value}:v))}/></label></div>)}<div className="catalog-actions"><button className="button" onClick={()=>setFaq([...faq,{q:'',a:''}])}><Plus size={16}/>添加答案</button><button className="button primary" disabled={busy||!p} onClick={()=>void submit()}>{busy&&<Spinner/>}提交审核</button></div><div className="editor-section"><h3>答案审核记录</h3>{!drafts.length&&<p className="muted">暂无待审核答案。</p>}{drafts.map(d=><article className="catalog-form-card" key={d.id}><div className="row-between"><b>{d.product_name}</b><span className="catalog-status">{{pending:'待审核',approved:'已通过',rejected:'已退回'}[d.status]||'待核对'}</span></div><p className="muted small">{d.submitted_by} · {date(d.created_at,true)}</p>{d.review_note&&<p>{d.review_note}</p>}{d.status==='pending'&&<button className="button" onClick={()=>{setReview(d);setNote('');}}>核对并审核</button>}</article>)}</div>{review&&<section className="catalog-form-card"><h3>核对：{review.product_name}</h3><div className="catalog-review-columns">{[['当前答案',review.before_faq],['拟生效答案',review.faq]].map(([label,list])=><div key={String(label)}><h4>{String(label)}</h4>{(list as Product['faq']).map((f,i)=><div className="catalog-answer-row" key={i}><b>{f.q}</b><p>{f.a}</p></div>)}{!(list as Product['faq']).length&&<p>没有答案</p>}</div>)}</div><label className="form-field">审核说明（选填）<textarea value={note} onChange={e=>setNote(e.target.value)} rows={2}/></label><div className="catalog-actions"><button className="button primary" disabled={busy} onClick={()=>void decide('approve')}>确认通过并生效</button><button className="button" disabled={busy} onClick={()=>void decide('reject')}>退回修改</button><button className="text-link" onClick={()=>setReview(null)}>收起</button></div></section>}</section>;
}
