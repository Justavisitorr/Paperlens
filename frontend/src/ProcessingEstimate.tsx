import type { ProcessingEstimate, ProfileEstimate } from "./api";
import { estimateCostText, formatDuration, formatDurationRange } from "./estimate";
import { Icon } from "./icons";
import { CURRENCIES, exchangeNote, useMoney, type MoneyDisplay } from "./currency";

/** Point estimates for time and explicitly denominated cost under each tier. */
export function tierEstimateLine(
  item: ProfileEstimate | undefined,
  money?: MoneyDisplay,
): string | null {
  const seconds = item?.typical?.seconds;
  return seconds
    ? `${formatDuration(seconds.typical)} · ${estimateCostText(item?.typical?.cost.typical, money)}`
    : null;
}

export function ProcessingEstimateCard({
  estimate,
  item,
  loading,
  error = "",
  onRetry,
  files,
  queued,
}: {
  estimate: ProcessingEstimate | null;
  item: ProfileEstimate | undefined;
  loading: boolean;
  error?: string;
  onRetry?: () => void;
  files: number;
  queued: number;
}) {
  const money = useMoney();
  const seconds = item?.typical?.seconds;
  const available = !!(estimate && item && seconds);
  const scope = estimate
    ? files > 1
      ? `${files} 篇共 ${estimate.pages} 页`
      : `${estimate.pages} 页`
    : files > 0
      ? `${files} 篇文档`
      : "尚未选择文档";
  const range = seconds ? formatDurationRange(seconds) : "—";
  const cost = item?.typical?.cost.typical;
  const priced = cost?.status === "range";
  const hasPrice = priced || cost?.status === "partial";
  const status = loading
    ? available
      ? "更新中 · 上次估算"
      : "正在估算时间与费用…"
    : error
      ? available
        ? "更新失败 · 上次估算"
        : "暂时无法估算时间与费用"
      : available
        ? "按当前设置估算"
        : "暂时无法估算时间与费用";
  return (
    <section className="estimate-card" aria-label="时间与费用估算" aria-busy={loading}>
      <header>
        <h3>
          <Icon name="info" size={15} />
          预计时间与费用
        </h3>
        <small>
          {estimate?.pages_assumed ? `按 ${scope} 估算` : scope}
          {item && ` · ${item.label}档`}
        </small>
      </header>
      <p className="estimate-status" role="status" aria-live="polite">
        {status}
      </p>
      <dl className="estimate-summary">
        <div>
          <dt>处理时间</dt>
          <dd className="estimate-time">
            <strong>{seconds ? formatDuration(seconds.typical) : "—"}</strong>
          </dd>
        </div>
        <div>
          <dt>预计费用（{CURRENCIES[money.currency]}）</dt>
          <dd className="estimate-time">
            <strong>{available ? estimateCostText(cost, money) : "—"}</strong>
          </dd>
        </div>
      </dl>
      <p
        className="footnote estimate-budget-warning"
        role={item?.budget_limited ? "note" : undefined}
      >
        {item?.budget_limited ? "当前设置可能不足以完成处理。" : ""}
      </p>
      <div className="estimate-actions">
        <details className="estimate-details">
          <summary>估算说明</summary>
          <div className="estimate-details-content">
            <p>时间参考范围：{range}</p>
            {item?.time_basis === "heuristic" && <p>时间尚未校准，仅供参考。</p>}
            {error && <p>{error}</p>}
            {hasPrice && exchangeNote(money) && <p>{exchangeNote(money)}</p>}
            {cost?.status === "partial" && (
              <p>部分模型尚无可核验单价，以上不含这些费用。</p>
            )}
            {available && !hasPrice && <p>当前模型服务尚无可核验单价。</p>}
            {hasPrice && (
              <p>
                {estimate?.price_checked_at
                  ? `模型参考价格核对日期：${estimate.price_checked_at.slice(0, 10)}。`
                  : ""}
                {estimate?.price_stale ? "参考价格已过期，" : ""}实际按服务商账单结算。
              </p>
            )}
          </div>
        </details>
        {onRetry && (
          <button
            type="button"
            className="button small"
            onClick={onRetry}
            disabled={loading}
          >
            重新估算
          </button>
        )}
      </div>
      {queued > 0 && <p className="footnote">当前另有 {queued} 个任务，等待时间另计。</p>}
    </section>
  );
}
