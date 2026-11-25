import { confirmProcessingBudget } from "./processingConfirmation";
import { useRef, useState, type ReactNode } from "react";
import {
  resolveImport,
  startImport,
  type ImportCandidate,
  type ProcessingProfileId,
  type ProcessingSpeedMode,
  type PaperSummary,
  type JobState,
} from "./api";
import { go } from "./router";
import {
  existingImport,
  importIdentity,
  readImportReceipts,
  rememberImport,
} from "./importIdentity";
import { Icon } from "./icons";
import { PROFILE_LABELS } from "./ProcessingTierPicker";
import "./styles/22-import.css";

export function ImportSource({
  profile,
  accelerated = false,
  speedMode,
  processingOptions,
  disabled,
  onImported,
  onLocalUpload,
  onBusyChange,
  papers = [],
  jobs = [],
}: {
  profile: ProcessingProfileId;
  accelerated?: boolean;
  speedMode?: ProcessingSpeedMode;
  processingOptions?: ReactNode;
  disabled: boolean;
  onImported: (id: string) => void;
  onLocalUpload: () => void;
  onBusyChange?: (busy: boolean) => void;
  papers?: PaperSummary[];
  jobs?: JobState[];
}) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<ImportCandidate[] | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [searching, setSearching] = useState(false);
  const [importing, setImporting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [importError, setImportError] = useState("");
  const [receipts, setReceipts] = useState(readImportReceipts);
  const submitting = useRef(false);
  const revision = useRef(0);
  const selected = candidates?.find((candidate) => candidate.id === selectedId);
  const lookup = async () => {
    if (!query.trim() || searching || importing || disabled) return;
    const request = ++revision.current;
    setSearching(true);
    setCandidates(null);
    setSelectedId("");
    setMessage("");
    setError("");
    setImportError("");
    try {
      const result = await resolveImport(query.trim());
      if (request !== revision.current) return;
      setCandidates(result.candidates);
      setMessage(result.message || "");
    } catch (cause) {
      if (request === revision.current)
        setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (request === revision.current) setSearching(false);
    }
  };
  const submit = async (reprocess = false) => {
    if (!selected?.pdf_url || submitting.current || importing || disabled) return;
    const identity = importIdentity(selected);
    if (!reprocess && (receipts[identity] || existingImport(selected, papers))) return;
    if (
      reprocess &&
      !confirm("按当前档位重新处理这篇论文？这会创建新任务，可能产生新的模型调用。")
    )
      return;
    submitting.current = true;
    setImporting(true);
    onBusyChange?.(true);
    setImportError("");
    try {
      const unlimitedConfirmed = await confirmProcessingBudget(profile);
      if (unlimitedConfirmed === null) return;
      const result = await startImport(
        selected.id,
        profile,
        unlimitedConfirmed,
        speedMode ?? accelerated,
      );
      setReceipts(rememberImport(identity, result.id, receipts));
      onImported(result.id);
    } catch (cause) {
      setImportError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setImporting(false);
      submitting.current = false;
      onBusyChange?.(false);
    }
  };
  return (
    <section className="import-source" aria-label="查找论文">
      <form
        className="import-query"
        onSubmit={(event) => {
          event.preventDefault();
          void lookup();
        }}
      >
        <label htmlFor="import-query">论文线索</label>
        <div className="import-query-controls">
          <input
            id="import-query"
            type="search"
            value={query}
            disabled={disabled || importing}
            placeholder="粘贴论文链接，或输入题名"
            aria-describedby="import-query-help"
            onChange={(event) => {
              ++revision.current;
              setQuery(event.target.value);
              setSearching(false);
              setCandidates(null);
              setSelectedId("");
              setMessage("");
              setError("");
              setImportError("");
            }}
          />
          <button
            className="button primary"
            disabled={disabled || searching || importing || !query.trim()}
          >
            {searching ? <span className="spinner" /> : <Icon name="search" size={15} />}
            {searching ? "查找中" : "查找论文"}
          </button>
        </div>
        <small id="import-query-help">选中论文后，可直接开始处理。</small>
      </form>
      {error && (
        <p className="error-line" role="alert">
          {error}
        </p>
      )}
      {message && (
        <p className="import-message" role="status">
          {message}
        </p>
      )}
      {candidates && (
        <div className="import-results" aria-live="polite">
          {candidates.length ? (
            <>
              <div className="import-results-heading">
                <h2>查找结果</h2>
                <small>{candidates.length} 个结果</small>
              </div>
              <div className="import-candidates" role="group" aria-label="选择论文">
                {candidates.map((candidate) => {
                  const paper = existingImport(candidate, papers);
                  const jobId = receipts[importIdentity(candidate)];
                  const job = jobs.find((item) => item.id === jobId);
                  const active =
                    !!job && ["queued", "running", "paused"].includes(job.status);
                  const existing = paper
                    ? {
                        label: "已有阅读结果",
                        action: "打开已有结果",
                        onOpen: () =>
                          go({ page: "read", paperId: paper.id, mode: "overview" }),
                        canReprocess: !active,
                      }
                    : jobId
                      ? {
                          label: active
                            ? "这篇论文已在处理队列中。"
                            : "这篇论文已有提交记录。",
                          action: "查看已有任务",
                          onOpen: () => {
                            onImported(jobId);
                            document
                              .querySelector(".queue")
                              ?.scrollIntoView({ block: "start", behavior: "smooth" });
                          },
                          canReprocess: !active,
                        }
                      : undefined;
                  return (
                    <ImportCandidateCard
                      key={candidate.id}
                      candidate={candidate}
                      selected={selectedId === candidate.id}
                      disabled={disabled || importing}
                      onSelect={() => {
                        setSelectedId(candidate.id);
                        setImportError("");
                      }}
                    >
                      <ImportCandidateActions
                        candidate={candidate}
                        profile={profile}
                        processingOptions={processingOptions}
                        disabled={disabled}
                        importing={importing}
                        imported={false}
                        existing={existing}
                        onReprocess={() => void submit(true)}
                        error={importError}
                        onSubmit={() => void submit()}
                        onLocalUpload={onLocalUpload}
                      />
                    </ImportCandidateCard>
                  );
                })}
              </div>
            </>
          ) : (
            <div className="import-empty">
              <strong>暂无结果</strong>
              <p>换个关键词，或直接上传文件。</p>
              <button className="button" onClick={onLocalUpload} disabled={disabled}>
                上传文件
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export function ImportCandidateCard({
  candidate,
  selected,
  disabled,
  onSelect,
  children,
}: {
  candidate: ImportCandidate;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
  children?: ReactNode;
}) {
  const source =
    candidate.url || (candidate.doi ? `https://doi.org/${candidate.doi}` : "");
  const link = /^https?:\/\//i.test(source) ? source : "";
  const metadata = [candidate.year, candidate.venue].filter(Boolean).join(" · ");
  return (
    <article className={`import-candidate ${selected ? "is-selected" : ""}`}>
      <label className="import-candidate-choice">
        <input
          type="radio"
          name="import-candidate"
          value={candidate.id}
          checked={selected}
          disabled={disabled}
          onChange={onSelect}
        />
        <span>
          <strong>{candidate.title}</strong>
          <small>
            {candidate.authors?.length ? candidate.authors.join("、") : "来源未提供作者"}
          </small>
          {metadata && <small>{metadata}</small>}
        </span>
      </label>
      {candidate.abstract ? (
        <details className="import-abstract">
          <summary>查看摘要</summary>
          <p>{candidate.abstract}</p>
        </details>
      ) : (
        <p className="import-missing-abstract">来源未提供摘要</p>
      )}
      <div className="import-candidate-source">
        {link && (
          <a href={link} target="_blank" rel="noreferrer">
            原文链接 <Icon name="external" size={12} />
          </a>
        )}
        {candidate.doi && <small>DOI: {candidate.doi}</small>}
        <span className={`status-tag ${candidate.pdf_url ? "s-complete" : ""}`}>
          {candidate.pdf_url ? "可下载" : "需上传"}
        </span>
      </div>
      {selected && children && <div className="import-candidate-actions">{children}</div>}
    </article>
  );
}

export function ImportCandidateActions({
  candidate,
  profile,
  processingOptions,
  disabled,
  importing,
  imported,
  error,
  onSubmit,
  onLocalUpload,
  existing,
  onReprocess,
}: {
  candidate: ImportCandidate;
  profile: ProcessingProfileId;
  processingOptions?: ReactNode;
  disabled: boolean;
  importing: boolean;
  imported: boolean;
  error: string;
  onSubmit: () => void;
  onLocalUpload: () => void;
  existing?: { label: string; action: string; onOpen: () => void; canReprocess: boolean };
  onReprocess?: () => void;
}) {
  if (existing)
    return (
      <div className="import-existing">
        <p role="status">{existing.label}</p>
        {existing.canReprocess && processingOptions}
        <div className="import-confirm">
          <button className="button" onClick={existing.onOpen}>
            {existing.action}
          </button>
          {existing.canReprocess && onReprocess && (
            <button
              className="text-button"
              disabled={disabled || importing}
              onClick={onReprocess}
            >
              重新处理
            </button>
          )}
        </div>
        {error && (
          <p className="error-line" role="alert">
            {error}
          </p>
        )}
      </div>
    );
  if (imported)
    return (
      <p className="import-message" role="status">
        已加入队列，可在下方查看进度。
      </p>
    );
  if (!candidate.pdf_url)
    return (
      <div className="import-unavailable">
        <p>请从原文链接下载后上传。</p>
        <button
          className="button"
          disabled={disabled || importing}
          onClick={onLocalUpload}
        >
          上传文件
        </button>
      </div>
    );
  return (
    <>
      {processingOptions}
      {error && (
        <p className="error-line" role="alert">
          {error}
        </p>
      )}
      <div className="import-confirm">
        <span>{PROFILE_LABELS[profile]}档</span>
        <button
          className="button primary"
          disabled={disabled || importing}
          onClick={onSubmit}
        >
          {importing && <span className="spinner" />}
          {importing ? "获取中" : "开始处理"}
        </button>
      </div>
    </>
  );
}
