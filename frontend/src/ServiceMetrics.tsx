import { useEffect, useState } from "react";
import { api, date } from "./api";
import { ErrorNote } from "./ui";

type Ratio = { numerator: number; denominator: number; rate: number | null };
type Metrics = {
  as_of: string;
  business_completion: Ratio & { rejected: number; followup_records: number };
  customer_resolution: Ratio & { unresolved_targets: number; unknown_targets: number };
  unresolved_followup: Ratio;
  rating: { score_sum: number; scored_cycles: number; mean: number | null; unscored_cycles: number; feedback_cycles: number };
};
function percentage(value: number | null) { return value === null ? "暂无样本" : `${(value * 100).toFixed(1)}%`; }

export function ServiceMetrics() {
  const [data, setData] = useState<Metrics | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let live = true;
    let pending = false;
    async function read() {
      if (pending) return;
      pending = true;
      try { const value = await api<Metrics>("/staff/service-metrics"); if (live) { setData(value); setError(""); } }
      catch (e) { if (live) setError((e as Error).message); }
      finally { pending = false; }
    }
    void read();
    const timer = setInterval(() => { if (document.visibilityState !== "hidden") void read(); }, 10000);
    return () => { live = false; clearInterval(timer); };
  }, [refresh]);
  return <details className="service-metrics">
    <summary>服务结果统计 <span>累计 · 分母分别计算</span></summary>
    <div className="service-metrics-body">
      {data ? <>
        <dl>
          <div><dt>业务完成率</dt><dd>{percentage(data.business_completion.rate)}<small>{data.business_completion.numerator} / {data.business_completion.denominator} 条业务记录</small></dd></div>
          <div><dt>顾客确认解决率</dt><dd>{percentage(data.customer_resolution.rate)}<small>{data.customer_resolution.numerator} / {data.customer_resolution.denominator} 条已完成记录；未反馈 {data.customer_resolution.unknown_targets} 条</small></dd></div>
          <div><dt>未解决跟进率</dt><dd>{percentage(data.unresolved_followup.rate)}<small>{data.unresolved_followup.numerator} / {data.unresolved_followup.denominator} 轮未解决反馈已建立跟进</small></dd></div>
          <div><dt>顾客评分</dt><dd>{data.rating.mean === null ? "暂无评分" : `${data.rating.mean.toFixed(2)} / 5`}<small>总分 {data.rating.score_sum} / {data.rating.scored_cycles} 轮有效评分；未评分 {data.rating.unscored_cycles} 轮</small></dd></div>
        </dl>
        <p className="muted small">草稿不计业务分母；已拒绝 {data.business_completion.rejected} 条仍计入分母。关联跟进 {data.business_completion.followup_records} 条分别计为业务记录。解决情况按每条已完成记录最近一次反馈统计；无反馈保留未知。评分不代表已解决。</p>
        <small className="muted">{date(data.as_of, true)} 更新</small>
      </> : <p className="muted small">正在读取服务统计…</p>}
      <ErrorNote error={error} />
      <button className="text-link" onClick={() => setRefresh(value => value + 1)}>刷新统计</button>
    </div>
  </details>;
}
