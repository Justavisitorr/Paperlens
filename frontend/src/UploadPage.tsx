import { useEffect, useMemo, useRef, useState } from "react";
import { Icon, type IconName } from "./icons";
import { href } from "./router";
import { ImportSource } from "./ImportSource";
import { FailureDetails } from "./FailureDetails";
import { ProcessingTierPicker } from "./ProcessingTierPicker";
export { MathReviewBudget } from "./ProcessingTierPicker";
import { combineEstimates } from "./estimate";
import { SPEED_LABELS } from "./processingSpeed";
import {
  estimateProcessing,
  getProcessingProfiles,
  type JobAction,
  type JobState,
  type ProcessingProfileId,
  type ProcessingEstimate,
  type ProcessingProfiles,
  type ProcessingSpeedMode,
  type Source,
  type PaperSummary,
} from "./api";

const ACTIVE = new Set(["queued", "running", "paused"]);
const DONE = new Set(["complete", "partial"]);
const statusText: Record<string, string> = {
  queued: "排队中",
  running: "解析中",
  paused: "已暂停",
  complete: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
};
const actionMeta: Record<JobAction, [IconName, string]> = {
  accelerate: ["bolt", "加速"],
  ultra: ["bolt", "极速"],
  pause: ["pause", "暂停"],
  resume: ["play", "继续"],
  cancel: ["close", "取消"],
  retry: ["refresh", "重试"],
};

export function UploadPage({
  active: pageActive = true,
  staged,
  papers = [],
  jobs,
  jobsSource,
  sessionJobs,
  busy,
  error,
  canUpload,
  onPick,
  onDropFiles,
  onUnstage,
  onStart,
  onAction,
  onImported,
  onClearHistory,
}: {
  active?: boolean;
  staged: File[];
  papers?: PaperSummary[];
  jobs: JobState[];
  jobsSource: Source | null;
  sessionJobs: Set<string>;
  busy: boolean;
  error: string;
  canUpload: boolean;
  onPick: () => void;
  onDropFiles: (files: FileList) => void;
  onUnstage: (index: number) => void;
  onStart: (profile: ProcessingProfileId, speedMode: ProcessingSpeedMode) => void;
  onAction: (job: JobState, action: JobAction) => void;
  onImported?: (id: string) => void;
  onClearHistory?: () => Promise<void>;
}) {
  const [entry, setEntry] = useState<"file" | "lookup">("file");
  const [importBusy, setImportBusy] = useState(false);
  const working = busy || importBusy;
  const [dragging, setDragging] = useState(false);
  const [profile, setProfile] = useState<ProcessingProfileId>("standard");
  const [speedMode, setSpeedMode] = useState<ProcessingSpeedMode>("normal");
  const [catalog, setCatalog] = useState<ProcessingProfiles | null>(null);
  const [profileError, setProfileError] = useState("");
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState("");
  const clearHistory = async () => {
    if (!onClearHistory || clearing) return;
    setClearing(true);
    setClearError("");
    try {
      await onClearHistory();
    } catch (e) {
      setClearError(`清理未完成：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setClearing(false);
    }
  };
  useEffect(() => {
    if (!canUpload || !pageActive) return;
    const controller = new AbortController();
    setProfileError("");
    getProcessingProfiles(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setCatalog(value);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setProfileError("暂时无法读取档位预算；仍可选择处理档位，按当前设置执行。");
      });
    return () => {
      controller.abort();
    };
  }, [canUpload, pageActive]);
  // Estimates follow what would be processed next: the staged files, or a
  // typical paper while looking one up. An older service without the
  // endpoint simply shows no estimate.
  const [estimateResult, setEstimateResult] = useState<{
    entry: typeof entry;
    staged: File[];
    value: ProcessingEstimate | null;
    pages: (number | null)[];
  } | null>(null);
  const [estimateRevision, setEstimateRevision] = useState(0);
  const sameDocument =
    estimateResult?.entry === entry &&
    estimateResult.staged.length === staged.length &&
    estimateResult.staged.every((file, index) => file === staged[index]);
  // New speed/settings requests retain this document's previous quote. A fresh
  // request identity marks it as being updated immediately, before effects run.
  const estimateRequest = useMemo(
    () => ({ entry, staged, speedMode, enabled: canUpload && pageActive }),
    [canUpload, pageActive, entry, staged, speedMode, estimateRevision],
  );
  const latestEstimateRequest = useRef(estimateRequest);
  latestEstimateRequest.current = estimateRequest;
  const [estimateStatus, setEstimateStatus] = useState<{
    request: typeof estimateRequest;
    error: string;
  } | null>(null);
  const estimate = canUpload && sameDocument ? estimateResult.value : null;
  const estimating =
    estimateRequest.enabled && estimateStatus?.request !== estimateRequest;
  const estimateError =
    estimateRequest.enabled && estimateStatus?.request === estimateRequest
      ? estimateStatus.error
      : "";
  useEffect(() => {
    if (!estimateRequest.enabled) return;
    const controller = new AbortController();
    const { entry, staged, speedMode } = estimateRequest;
    const estimateFiles = entry === "file" ? staged : [];
    const wanted = estimateFiles.length
      ? estimateFiles.map((f) => estimateProcessing(f, controller.signal, speedMode))
      : [estimateProcessing(undefined, controller.signal, speedMode)];
    Promise.all(wanted)
      .then((list) => {
        if (
          !controller.signal.aborted &&
          latestEstimateRequest.current === estimateRequest
        ) {
          setEstimateResult({
            entry,
            staged,
            value: combineEstimates(list),
            pages: estimateFiles.map((_, index) =>
              list[index].pages_assumed ? null : list[index].pages,
            ),
          });
          setEstimateStatus({ request: estimateRequest, error: "" });
        }
      })
      .catch(() => {
        if (
          !controller.signal.aborted &&
          latestEstimateRequest.current === estimateRequest
        )
          setEstimateStatus({
            request: estimateRequest,
            error: "本次估算未更新，可点击“重新估算”。",
          });
      });
    return () => {
      controller.abort();
    };
  }, [estimateRequest]);
  const active = jobs.filter((j) => ACTIVE.has(j.status));
  const history = jobs.filter((j) => !ACTIVE.has(j.status));
  return (
    <div className="page upload-page">
      <header className="page-head">
        <div>
          <h1>导入论文</h1>
          <p>上传文件，或查找论文后导入。</p>
        </div>
      </header>

      <div className="import-tabs" role="tablist" aria-label="论文导入方式">
        <button
          role="tab"
          id="import-tab-file"
          aria-selected={entry === "file"}
          aria-controls="import-panel-file"
          onClick={() => setEntry("file")}
        >
          <Icon name="upload" size={16} /> 上传文件
          {staged.length ? ` · ${staged.length}` : ""}
        </button>
        <button
          role="tab"
          id="import-tab-lookup"
          aria-selected={entry === "lookup"}
          aria-controls="import-panel-lookup"
          onClick={() => setEntry("lookup")}
        >
          <Icon name="search" size={16} /> 查找论文
        </button>
      </div>

      <div
        id="import-panel-file"
        role="tabpanel"
        aria-labelledby="import-tab-file"
        hidden={entry !== "file"}
      >
        {staged.length > 0 && (
          <section className="staged" aria-label="待处理文件">
            <ul>
              {staged.map((file, index) => (
                <li key={`${file.name}-${index}`}>
                  <Icon name="file" size={15} />
                  <span title={file.name}>{file.name}</span>
                  {sameDocument && estimateResult.pages[index] != null && (
                    <small>{estimateResult.pages[index]} 页</small>
                  )}
                  <small>{(file.size / 1024 / 1024).toFixed(2)} MB</small>
                  <button
                    className="icon-button small"
                    aria-label={`移除 ${file.name}`}
                    onClick={() => onUnstage(index)}
                    disabled={working}
                  >
                    <Icon name="close" size={14} />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
        <button
          type="button"
          className={`drop-zone${staged.length ? " drop-zone-compact" : ""} ${dragging ? "is-dragging" : ""}`}
          onClick={onPick}
          disabled={!canUpload || working}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (canUpload && !working) onDropFiles(e.dataTransfer.files);
          }}
        >
          <span className="drop-icon">
            <Icon name="upload" size={20} />
          </span>
          <strong>{staged.length ? "继续添加 PDF" : "拖入文件，或点击上传"}</strong>
          <small>{staged.length ? "也可拖入更多文件" : "支持中英文 PDF，可多选。"}</small>
        </button>
      </div>

      <div
        id="import-panel-lookup"
        role="tabpanel"
        aria-labelledby="import-tab-lookup"
        hidden={entry !== "lookup"}
      >
        <ImportSource
          papers={papers}
          jobs={jobs}
          profile={profile}
          speedMode={speedMode}
          processingOptions={
            <ProcessingTierPicker
              profile={profile}
              speedMode={speedMode}
              onSpeedModeChange={setSpeedMode}
              catalog={catalog}
              error={profileError}
              disabled={working}
              compact
              estimate={estimate}
              estimating={estimating}
              estimateError={estimateError}
              onRetryEstimate={() => setEstimateRevision((revision) => revision + 1)}
              queued={active.length}
              onChange={setProfile}
            />
          }
          disabled={busy || !canUpload || !onImported}
          onImported={(id) => onImported?.(id)}
          onBusyChange={setImportBusy}
          onLocalUpload={() => {
            setEntry("file");
            onPick();
          }}
        />
      </div>

      {!canUpload && (
        <p className="notice static">
          <Icon name="info" size={15} />
          这是离线打开的页面。处理新论文请先运行 <code>paperlens serve</code>
          ，再从服务地址打开。
        </p>
      )}

      {canUpload && entry === "file" && (
        <ProcessingTierPicker
          profile={profile}
          speedMode={speedMode}
          onSpeedModeChange={setSpeedMode}
          catalog={catalog}
          error={profileError}
          disabled={working}
          compact
          estimate={estimate}
          estimating={estimating}
          estimateError={estimateError}
          onRetryEstimate={() => setEstimateRevision((revision) => revision + 1)}
          files={staged.length}
          queued={active.length}
          onChange={setProfile}
        />
      )}

      {staged.length > 0 && entry === "file" && (
        <div className="staged-actions">
          <small>处理后自动加入文献库。</small>
          <button
            className="button primary"
            onClick={() => onStart(profile, speedMode)}
            disabled={working || !canUpload}
          >
            {working && <span className="spinner" />}
            开始处理
          </button>
        </div>
      )}

      {error && (
        <p className="error-line" role="alert">
          <Icon name="warning" size={15} />
          {error}
        </p>
      )}

      {(active.length > 0 || history.length > 0) && (
        <section className="queue" aria-live="polite">
          {active.length > 0 && (
            <>
              <h2 className="subhead">处理中</h2>
              {active.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  fresh={sessionJobs.has(job.id)}
                  onAction={onAction}
                />
              ))}
            </>
          )}
          {history.length > 0 && (
            <>
              <div className="job-history-heading">
                <h2 className="subhead">最近任务 · {history.length}</h2>
                {onClearHistory && (
                  <div className="job-history-actions">
                    <a
                      href={href({ page: "settings" })}
                      onClick={() => {
                        try {
                          sessionStorage.setItem("paperlens.settingsTab", "processing");
                        } catch {
                          /* Settings remain accessible without browser storage. */
                        }
                      }}
                    >
                      保留设置
                    </a>
                    <button
                      className="button small"
                      disabled={clearing}
                      title="仅清理已结束任务，文献库与笔记保留"
                      onClick={() => void clearHistory()}
                    >
                      {clearing && <span className="spinner" />}
                      {clearing ? "正在清理" : "清理记录"}
                    </button>
                  </div>
                )}
              </div>
              {clearError && (
                <p className="error-line" role="alert">
                  {clearError}
                </p>
              )}
              {history.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  fresh={sessionJobs.has(job.id)}
                  onAction={onAction}
                />
              ))}
            </>
          )}
          {jobsSource === "fallback" && (
            <p className="footnote">关闭页面后，任务仍会继续。</p>
          )}
        </section>
      )}
    </div>
  );
}

function JobRow({
  job,
  fresh,
  onAction,
}: {
  job: JobState;
  fresh: boolean;
  onAction: (job: JobState, action: JobAction) => void;
}) {
  const done = DONE.has(job.status);
  const progress = typeof job.progress === "number" ? job.progress : 0;
  const paperId = job.paper_id || job.id;
  const mode =
    job.speed_mode ||
    job.acceleration?.mode ||
    (job.acceleration?.enabled ? "fast" : "normal");
  return (
    <article className={`job status-${job.status} ${fresh && done ? "is-fresh" : ""}`}>
      <span className="job-icon">
        {ACTIVE.has(job.status) && job.status !== "paused" ? (
          <span className="spinner" />
        ) : (
          <Icon
            name={done ? "check" : job.status === "paused" ? "pause" : "warning"}
            size={15}
          />
        )}
      </span>
      <div className="job-body">
        <div className="job-line">
          <strong title={job.source_name}>{job.source_name}</strong>
          {job.profile_label && <span className="status-tag">{job.profile_label}</span>}
          {job.model && <span className="status-tag">{job.model}</span>}
          <span className={`status-tag s-${job.status}`}>
            {statusText[job.status] || job.status}
          </span>
        </div>
        <small>{job.error || job.message}</small>
        <FailureDetails failures={job.failures} />
        {ACTIVE.has(job.status) && (
          <div
            className="progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
          >
            <span style={{ width: `${Math.max(3, progress * 100)}%` }} />
          </div>
        )}
      </div>
      <div className="job-actions">
        {job.source_url && (
          <a className="button small" href={href({ page: "processing", jobId: job.id })}>
            阅读原文
          </a>
        )}
        {job.acceleration?.enabled && ACTIVE.has(job.status) && (
          <span className="status-tag s-running" title="在可并行的步骤生效，保留全部核查">
            {SPEED_LABELS[mode]} · {job.acceleration.concurrency} 路
          </span>
        )}
        {(["accelerate", "ultra"] as const)
          .filter((action) => job.actions?.includes(action))
          .map((action) => (
            <button
              key={action}
              className="button small"
              onClick={() => {
                if (
                  window.confirm(
                    `${action === "ultra" ? "切换到极速" : "加速"}此论文？\n\n按任务保存的并行设置运行，保留全部核查和总请求预算。速度取决于服务商和当前阶段。`,
                  )
                )
                  onAction(job, action);
              }}
              aria-label={`${action === "ultra" ? "极速" : "加速"} ${job.source_name}`}
            >
              <Icon name={action === "ultra" ? "bolts" : "bolt"} size={14} />
              {action === "ultra" ? "极速" : "加速"}
            </button>
          ))}
        {job.actions
          ?.filter((action) => !["accelerate", "ultra"].includes(action))
          .map((action) => (
            <button
              key={action}
              className={
                job.input_budget && ["retry", "resume"].includes(action)
                  ? "button small"
                  : "icon-button small"
              }
              title={
                job.input_budget && ["retry", "resume"].includes(action)
                  ? "确认本次输入预算后继续"
                  : actionMeta[action][1]
              }
              aria-label={`${job.input_budget && ["retry", "resume"].includes(action) ? "确认输入预算后继续" : actionMeta[action][1]} ${job.source_name}`}
              onClick={() => onAction(job, action)}
            >
              <Icon name={actionMeta[action][0]} size={14} />
              {job.input_budget &&
                ["retry", "resume"].includes(action) &&
                "确认预算并继续"}
            </button>
          ))}
        {done &&
          (fresh ? (
            <a
              className="button primary small"
              href={href({ page: "read", paperId, mode: "overview" })}
              onClick={() => {
                window.__paperlensReady = false;
              }}
            >
              继续阅读
              <Icon name="arrow" size={14} />
            </a>
          ) : (
            <a
              className="button small"
              href={href({ page: "read", paperId, mode: "overview" })}
            >
              阅读
            </a>
          ))}
      </div>
    </article>
  );
}
