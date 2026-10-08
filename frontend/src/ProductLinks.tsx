import { useState } from 'react';
import { Copy } from 'lucide-react';
import type { Product } from './types';
import { copyProductLink, productPath, visitProduct } from './catalogLinks';
import './CatalogManagement.css';
export function ProductLink({path,label,inline=false}:{path:string;label:string;inline?:boolean}) {
  const [feedback,setFeedback]=useState('');
  async function copy(){try{await copyProductLink(path);setFeedback('已复制');}catch{setFeedback('复制暂不可用，可右键复制商品链接');}}
  return <span className={inline?'':'product-link-actions'}><a className={inline?'chat-inline-product-link':undefined} href={path} onClick={e=>{if(e.button===0&&!e.metaKey&&!e.ctrlKey&&!e.shiftKey&&!e.altKey){e.preventDefault();visitProduct(path);}}}>{label}</a><button className={inline?'copy-inline-link':'text-link'} aria-label={`复制${label}链接`} title={feedback||'复制商品链接'} onClick={()=>void copy()}><Copy size={13}/>{!inline&&(feedback||'复制链接')}</button>{inline&&feedback&&<small role="status">{feedback}</small>}</span>;
}
export function ProductLinks({product,skuID}:{product:Product;skuID?:string}) {
  const path=productPath(product,skuID);
  return path?<ProductLink path={path} label="打开商品页面"/>:null;
}
