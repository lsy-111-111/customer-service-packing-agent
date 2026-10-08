export type PackingSkuState = { sku_id: string; specification: string; product_barcode: string; packing_enabled: boolean; version: number };
export type PackingSkuConfirmation = { version: number; disabled_confirmed: boolean; barcode_conflict_confirmed: boolean; reason: string };

export function skuNeedsReview(state: PackingSkuState | undefined, usedBarcode: string) {
  return !!state && (!state.packing_enabled || !!(usedBarcode && state.product_barcode && usedBarcode !== state.product_barcode));
}

export default function PackingSkuReview({ state, usedBarcode, value, onChange, label }: {
  state?: PackingSkuState; usedBarcode: string; value?: PackingSkuConfirmation;
  onChange: (value: PackingSkuConfirmation) => void; label: string;
}) {
  if (!state || !skuNeedsReview(state, usedBarcode)) return null;
  const current = value?.version === state.version ? value : { version: state.version, disabled_confirmed: false, barcode_conflict_confirmed: false, reason: "" };
  const conflict = !!(usedBarcode && state.product_barcode && usedBarcode !== state.product_barcode);
  return <div className="packing-notice packing-sku-exception">
    <b>{label} · 需要本单人工核对</b>
    {!state.packing_enabled && <><p>这个 SKU 已停用装箱。确需处理本单时，请核对具体依据并登记例外。</p><label className="packing-check"><input type="checkbox" aria-label={`${label}停用例外确认`} checked={current.disabled_confirmed} onChange={event => onChange({ ...current, disabled_confirmed: event.target.checked })} />已核对并确认本单使用停用 SKU</label></>}
    {conflict && <><p>本单条码：{usedBarcode}；SKU 登记条码：{state.product_barcode}。请核对差异原因及本次打印值。</p><label className="packing-check"><input type="checkbox" aria-label={`${label}条码冲突确认`} checked={current.barcode_conflict_confirmed} onChange={event => onChange({ ...current, barcode_conflict_confirmed: event.target.checked })} />已核对条码冲突并确认本单打印值</label></>}
    <label>本单例外原因与核对依据<textarea aria-label={`${label}例外原因`} maxLength={500} value={current.reason} onChange={event => onChange({ ...current, reason: event.target.value })} placeholder="填写本单可采用该商品或条码的具体依据" /></label>
    <p className="packing-muted">本单例外不保存为自动复用映射；状态或条码再次变化后需重新核对。</p>
  </div>;
}
