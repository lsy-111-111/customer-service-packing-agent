import type { Product } from './types';

// Only catalogue-issued paths are customer purchase destinations.
export function productPath(product: Product, skuID?: string): string | null {
  const path = skuID ? product.skus.find(s => s.id === skuID)?.purchase_path : product.purchase_path;
  if (!path) return null;
  try {
    const url = new URL(path, window.location.origin);
    if (url.origin !== window.location.origin || url.pathname !== `/products/${encodeURIComponent(product.id)}`) return null;
    if (skuID && url.searchParams.get('sku') !== skuID) return null;
    return url.pathname + url.search;
  } catch { return null; }
}
export function safeProductLink(value: string): string | null {
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || !/^\/products\/[^/]+$/.test(url.pathname)) return null;
    if ([...url.searchParams.keys()].some(k => k !== 'sku')) return null;
    return url.pathname + url.search;
  } catch { return null; }
}
export function visitProduct(path: string) {
  history.pushState(null, '', path);
  window.dispatchEvent(new Event('shop:product-navigation'));
}
export async function copyProductLink(path: string) {
  await navigator.clipboard.writeText(new URL(path, window.location.origin).href);
}
