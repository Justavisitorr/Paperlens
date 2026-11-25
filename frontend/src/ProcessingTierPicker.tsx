import { useId } from "react";
import { AUTO_NOTE_CONTEXT_LABELS } from "./AutoNotes";
import { openSettings, settingsHref } from "./settingsNavigation";
import type { ProcessingEstimate, ProcessingProfileId, ProcessingProfiles } from "./api";
import { estimateCostText, formatDuration, profileEstimate } from "./estimate";
import { ProcessingEstimateCard, tierEstimateLine } from "./ProcessingEstimate";
import { Icon } from "./icons";
import { useMoney } from "./currency";
import { SPEED_LABELS, nextSpeedMode, type ProcessingSpeedMode } from "./processingSpeed";

export const PROFILE_LABELS: Record<ProcessingProfileId, string> = {
  economy: "经济",
  standard: "标准",
  detailed: "精细",
};

type Profile = ProcessingProfiles["profiles"][number];
const FALLBACK_PROFILES: (Omit<Profile, "limits"> & { limits?: Profile["limits"] })[] = [
  {
    id: "economy",
    label: "经济",
    description: "精简解释，保留全文阅读、核心结论与证据核查。",
  },
  { id: "standard", label: "标准", description: "完整梳理论文论证，按当前设置处理。" },
  { id: "detailed", label: "精细", description: "展开细节和解释，增加公式审校。" },
];

export function MathReviewBudget({ batches }: { batches?: number }) {
  if (batches === undefined) return null;
  return <small>{batches ? `公式审校 ${batches} 批，按整页核对。` : "公式审校已关闭。"}</small>;
}

function ProfileBudget({ limits }: { limits: Profile["limits"] }) {
  return <>
    <dl className="tier-limits">
      <div><dt>读取方式</dt><dd>AI 直读 · 全文</dd></div>
      <div><dt>代码</dt><dd>随原文保留换行与缩进</dd></div>
    </dl>
    <MathReviewBudget batches={limits.math_review_batches} />
  </>;
}

export function ProcessingTierPicker({
  profile,
  catalog,
  error,
  disabled,
  compact = false,
  estimate = null,
  estimating = false,
  estimateError = "",
  onRetryEstimate,
  files = 0,
  queued = 0,
  accelerated = false,
  onAcceleratedChange,
  speedMode,
  onSpeedModeChange,
  onChange,
}: {
  profile: ProcessingProfileId;
  catalog: ProcessingProfiles | null;
  error: string;
  disabled: boolean;
  compact?: boolean;
  /** Time and cost per tier for what is about to be processed. */
  estimate?: ProcessingEstimate | null;
  estimating?: boolean;
  estimateError?: string;
  onRetryEstimate?: () => void;
  files?: number;
  queued?: number;
  accelerated?: boolean;
  onAcceleratedChange?: (accelerated: boolean) => void;
  speedMode?: ProcessingSpeedMode;
  onSpeedModeChange?: (speedMode: ProcessingSpeedMode) => void;
  onChange: (profile: ProcessingProfileId) => void;
}) {
  const id = useId();
  const money = useMoney();
  const mode = speedMode ?? (accelerated ? "fast" : "normal");
  const next = nextSpeedMode(mode);
  const selected = catalog?.profiles.find((item) => item.id === profile);
  const profiles = catalog?.profiles || FALLBACK_PROFILES;
  const estimateState = estimating
    ? estimate
      ? "旧值·更新中"
      : "正在估算"
    : estimateError
      ? estimate
        ? "旧值·更新失败"
        : "估算失败"
      : estimate
        ? "当前估算"
        : "尚无估算";
  const notes = (
    <>
      {catalog?.auto_notes && (
        <p className="footnote">
          {catalog.auto_notes.enabled
            ? `自动笔记 ${catalog.auto_notes.max_paragraphs} 段 · ${AUTO_NOTE_CONTEXT_LABELS[catalog.auto_notes.context_scope]}`
            : "自动笔记已关闭"}{" "}
          <a
            className="button small settings-shortcut"
            href={settingsHref("autoNotes")}
            onClick={(event) => openSettings("autoNotes", event)}
          >
            配置自动笔记
          </a>
        </p>
      )}
      {selected && (
        <p className="footnote">
          {`调用上限 ${selected.limits.max_requests} 次，含重试。`} {catalog?.cost_note}
        </p>
      )}
    </>
  );
  return (
    <section
      className={`tier-picker${compact ? " tier-picker-compact" : ""}`}
      aria-label="处理档位"
    >
      <div className="tier-picker-head">
        <h2>处理档位</h2>
        <small className="tier-estimate-status" id={`${id}-estimate-status`}>
          {estimateState}
        </small>
        {!compact && (
          <small className="tier-setting-line">
            <span>
              {catalog?.rule || "各档均阅读全文并核查证据，解释深度与公式审校批次不同。"}
            </span>
            <a
              className="button small settings-shortcut"
              href={settingsHref("budget")}
              onClick={(event) => openSettings("budget", event)}
            >
              调整处理预算
            </a>
          </small>
        )}
      </div>
      {error && <p className="footnote">{error}</p>}
      <div
        className="tier-options"
        role="radiogroup"
        aria-label="处理档位"
        aria-describedby={`${id}-estimate-status`}
      >
        {profiles.map((item) => {
          const itemEstimate = profileEstimate(estimate, item.id);
          return (
            <div key={item.id} className="tier-option-card">
              <button
                type="button"
                role="radio"
                className="tier-option"
                aria-checked={profile === item.id}
                aria-labelledby={`${id}-${item.id}-name`}
                aria-describedby={`${id}-${item.id}-desc`}
                disabled={disabled}
                onClick={() => onChange(item.id)}
              >
                <span className="tier-option-title">
                  <strong id={`${id}-${item.id}-name`}>{item.label}</strong>
                </span>
                <small id={`${id}-${item.id}-desc`}>{item.description}</small>
                <small
                  className="tier-estimate"
                  aria-busy={estimating}
                  title={
                    itemEstimate
                      ? `${estimateState}：${tierEstimateLine(itemEstimate, money)}`
                      : undefined
                  }
                >
                  <span>
                    {itemEstimate?.typical?.seconds
                      ? formatDuration(itemEstimate.typical.seconds.typical)
                      : "—"}
                  </span>
                  <span>
                    {itemEstimate?.typical
                      ? estimateCostText(itemEstimate.typical.cost.typical, money)
                      : "—"}
                  </span>
                </small>
                {!compact && item.limits && <ProfileBudget limits={item.limits} />}
              </button>
              {(onSpeedModeChange || onAcceleratedChange) && (
                <button
                  type="button"
                  className="tier-accelerate"
                  data-speed-mode={mode}
                  aria-label={`${item.label}档加速`}
                  aria-pressed={mode !== "normal"}
                  aria-description={`当前${SPEED_LABELS[mode]}，点击切换${SPEED_LABELS[next]}。按回车或空格也可切换。`}
                  title={`当前${SPEED_LABELS[mode]}，点击切换${SPEED_LABELS[next]}；连续点两下可从普通切到极速。切换档位后仍沿用此设置。`}
                  disabled={disabled}
                  onClick={() => {
                    onChange(item.id);
                    if (onSpeedModeChange) onSpeedModeChange(next);
                    else onAcceleratedChange?.(mode === "normal");
                  }}
                >
                  <Icon name={mode === "ultra" ? "bolts" : "bolt"} size={16} />
                </button>
              )}
            </div>
          );
        })}
      </div>
      {compact && (
        <p className="tier-selected-description">
          {profiles.find((item) => item.id === profile)?.description}
          {selected?.limits.ai_direct ? (
            <span> · AI 直读 · 全文 · 代码随原文保留</span>
          ) : (
            selected?.limits.code_blocks !== undefined && (
              <span>
                {" · 代码排版"}
                {selected.limits.code_blocks ? "开启" : "关闭"}
              </span>
            )
          )}
        </p>
      )}
      {(onSpeedModeChange || mode !== "normal") && (
        <p className="footnote tier-acceleration-note">
          点击右上角闪电切换速度，连点两下进入极速。
        </p>
      )}
      {!selected?.limits.ai_direct && !!selected?.unlimited_limits?.length && (
        <p className="footnote" role="note">
          {selected.unlimited_limits.join("、")}
          不限，可能增加耗时和模型费用。开始前需确认；总请求与资源保护仍生效。
        </p>
      )}
      {!catalog && !error && <p className="muted">正在读取档位预算…</p>}
      <ProcessingEstimateCard
        estimate={estimate}
        item={profileEstimate(estimate, profile)}
        loading={estimating}
        error={estimateError}
        onRetry={onRetryEstimate}
        files={files}
        queued={queued}
      />
      {compact ? (
        <details className="tier-budget-details">
          <summary>处理详情</summary>
          <p className="footnote tier-setting-line">
            <span>
              {catalog?.rule || "各档均阅读全文并核查证据，解释深度与公式审校批次不同。"}
            </span>
            <a
              className="button small settings-shortcut"
              href={settingsHref("budget")}
              onClick={(event) => openSettings("budget", event)}
            >
              调整处理预算
            </a>
          </p>
          {selected && <ProfileBudget limits={selected.limits} />}
          {notes}
        </details>
      ) : (
        notes
      )}
    </section>
  );
}
