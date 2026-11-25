import { useEffect, useState } from "react";
import { getJob, jobSourcePdf, type JobAction, type JobState } from "./api";
import { FailureDetails } from "./FailureDetails";
import { PdfSource, type PdfSourceReport } from "./PdfSource";
import { sourceDocument } from "./pdfRuntime";
import { readLocal, writeLocal } from "./readingExperience";
import { href } from "./router";
import "./styles/processing-reader.css";

const statusLabel: Record<JobState["status"], string> = {
  queued: "等待处理",
  running: "正在整理",
  paused: "已暂停",
  complete: "整理完成",
  partial: "部分完成",
  failed: "处理未完成",
  cancelled: "已取消处理",
};
const actionLabel = {
  pause: "暂停",
  resume: "继续处理",
  cancel: "取消处理",
  retry: "重试未完成步骤",
};

export function ProcessingStatus({
  job,
  onAction,
}: {
  job: JobState;
  onAction: (job: JobState, action: JobAction) => void;
}) {
  const ready = job.status === "complete" || job.status === "partial";
  const active = ["queued", "running", "paused"].includes(job.status);
  return (
    <div className="processing-reader-status">
      <div className="processing-reader-progress" aria-live="polite">
        <strong>{statusLabel[job.status]}</strong>
        <span>{job.error || job.message}</span>
        {active && (
          <progress max={1} value={job.progress || 0} aria-label="论文处理进度" />
        )}
        <small>
          {ready
            ? "整理结果已就绪。可以继续读原文，或打开整理结果。"
            : active
              ? "你可以先读原文；处理进度会在这里更新，离开页面也不会停止任务。"
              : "原文仍可阅读，已完成的处理结果保留。"}
        </small>
        <FailureDetails failures={job.failures} />
      </div>
      <div className="processing-reader-actions">
        {job.actions
          ?.filter((action): action is keyof typeof actionLabel => action in actionLabel)
          .map((action) => (
            <button
              key={action}
              className="button small"
              onClick={() => onAction(job, action)}
            >
              {job.input_budget && ["retry", "resume"].includes(action)
                ? "确认预算并继续"
                : actionLabel[action]}
            </button>
          ))}
        {ready && (
          <a
            className="button primary small"
            href={href({ page: "read", paperId: job.paper_id || job.id, mode: "text" })}
          >
            打开整理结果
          </a>
        )}
      </div>
    </div>
  );
}

/** The parent supplies its existing task polling; this page never starts a second poller. */
export function ProcessingReader({
  jobId,
  job: liveJob,
  server,
  onAction,
}: {
  jobId: string;
  job?: JobState;
  server: boolean;
  onAction: (job: JobState, action: JobAction) => void;
}) {
  const [loadedJob, setLoadedJob] = useState<JobState>();
  const [jobError, setJobError] = useState("");
  const [jobAttempt, setJobAttempt] = useState(0);
  const job = liveJob || loadedJob;
  // A refreshed deep link can precede the task list. Archived tasks also stay readable.
  useEffect(() => {
    if (liveJob || !server) return;
    const controller = new AbortController();
    setJobError("");
    getJob(jobId, controller.signal).then(setLoadedJob, (error) => {
      if (!controller.signal.aborted)
        setJobError(error instanceof Error ? error.message : String(error));
    });
    return () => controller.abort();
  }, [jobId, !!liveJob, server, jobAttempt]);

  return (
    <section className="processing-reader" aria-label="处理中阅读原文">
      <header className="processing-reader-heading">
        <div>
          <span className="page-eyebrow">原文阅读</span>
          <h1>{job?.source_name || "原始 PDF"}</h1>
        </div>
        <a className="button small" href={href({ page: "upload" })}>
          查看任务
        </a>
      </header>
      {!server ? (
        <p className="processing-reader-message">请在在线工作区打开原始 PDF。</p>
      ) : !job ? (
        <div className="processing-reader-message" role={jobError ? "alert" : "status"}>
          {jobError || "正在读取任务…"}
          {jobError && (
            <button className="button small" onClick={() => setJobAttempt((v) => v + 1)}>
              重试
            </button>
          )}
        </div>
      ) : (
        <>
          <ProcessingStatus job={job} onAction={onAction} />
          <UploadedPdf
            jobId={jobId}
            sourceUrl={job.source_url}
            name={job.source_name}
            sha256={job.source_sha256 || ""}
          />
        </>
      )}
    </section>
  );
}

function UploadedPdf({
  jobId,
  sourceUrl,
  name,
  sha256,
}: {
  jobId: string;
  sourceUrl?: string | null;
  name: string;
  sha256: string;
}) {
  const [report, setReport] = useState<PdfSourceReport>();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const pageKey = `paperlens:job-source-page:${jobId}`;
  const [page, setPage] = useState(() => {
    const stored = readLocal(pageKey);
    return typeof stored === "number" && Number.isSafeInteger(stored) && stored > 0
      ? stored
      : 1;
  });
  useEffect(() => {
    const controller = new AbortController();
    let source: ReturnType<typeof sourceDocument> | undefined;
    setReport(undefined);
    setError("");
    async function load() {
      try {
        if (!sourceUrl)
          throw new Error(
            "此任务的原始 PDF 不可用。请确认电脑端已更新，或重新上传原文件。",
          );
        const blob = await jobSourcePdf(jobId, controller.signal);
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          const abort = () => reader.abort();
          controller.signal.addEventListener("abort", abort, { once: true });
          reader.onloadend = () => controller.signal.removeEventListener("abort", abort);
          reader.onerror = () => reject(reader.error);
          reader.onabort = () => reject(new DOMException("Aborted", "AbortError"));
          reader.onload = () =>
            typeof reader.result === "string"
              ? resolve(reader.result)
              : reject(new Error("无法读取 PDF。"));
          reader.readAsDataURL(blob);
          if (controller.signal.aborted) reader.abort();
        });
        if (controller.signal.aborted) return;
        source = sourceDocument({ source_pdf: { data_url: dataUrl, sha256 } });
        const pdf = await source.document;
        if (controller.signal.aborted) return;
        setPage((previous) => Math.min(previous, pdf.numPages));
        setReport({
          source_pdf: { data_url: dataUrl, sha256 },
          previews: [],
          document: {
            source_name: name,
            sha256,
            parser: "raw-pdf",
            blocks: [],
            pages: Array.from({ length: pdf.numPages }, (_, index) => ({
              number: index + 1,
              label: "",
            })),
          },
        });
      } catch (reason) {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
    void load();
    return () => {
      controller.abort();
      source?.release();
    };
  }, [jobId, sourceUrl, name, sha256, attempt]);

  return (
    <div className="processing-reader-pdf">
      {report ? (
        <PdfSource
          report={report}
          page={page}
          onNavigate={(next) => {
            setPage(next);
            writeLocal(pageKey, next);
          }}
        />
      ) : (
        <div className="processing-reader-message" role={error ? "alert" : "status"}>
          {error || "正在打开原始 PDF，整理任务继续运行…"}
          {error && (
            <button className="button small" onClick={() => setAttempt((v) => v + 1)}>
              重试打开
            </button>
          )}
        </div>
      )}
    </div>
  );
}
