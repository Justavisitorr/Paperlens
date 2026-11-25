import { savedFullReadMode } from "./readerNavigation";
import { useEffect, useRef, useState } from "react";
import type { PaperSummary } from "./api";
import { Icon } from "./icons";
import { paperProgress, paperTitle, readingLabels } from "./libraryMetadata";
import { PaperCover } from "./PaperCover";
import { dataBytes } from "./pdfRuntime";
import { href } from "./router";

export type PaperChanges = {
  starred?: boolean;
  reading_status?: "unread" | "reading" | "read";
};

export function PaperDetails({
  paper,
  embedded,
  source,
  onClose,
  onRead,
  onUpdate,
  onRemove,
  onDownload,
}: {
  paper: PaperSummary;
  embedded: boolean;
  source: "native" | "fallback" | null;
  onClose: () => void;
  onRead?: (paper: PaperSummary) => void;
  onUpdate?: (paper: PaperSummary, changes: PaperChanges) => Promise<void>;
  onRemove: (paper: PaperSummary) => void;
  onDownload: (paper: PaperSummary) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const panel = useRef<HTMLElement>(null);
  const [pdfBlob, setPdfBlob] = useState("");
  const progress = paperProgress(paper);
  const status = paper.reading_status || "unread";
  const continuing = status !== "unread" || (progress != null && progress > 0);
  const update = async (changes: PaperChanges) => {
    if (!onUpdate) return;
    setSaving(true);
    setError("");
    try {
      await onUpdate(paper, changes);
    } catch {
      setError("未能保存更改，请重试。");
    } finally {
      setSaving(false);
    }
  };
  const safeLink = (url?: string) =>
    url && /^(https?:|data:application\/pdf;|\/api\/)/i.test(url) ? url : "";
  const pdf = safeLink(paper.pdf_url);
  const publication = safeLink(paper.source_url);
  useEffect(() => {
    panel.current?.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    setPdfBlob("");
    if (!pdf.startsWith("data:application/pdf;")) return;
    try {
      const url = URL.createObjectURL(
        new Blob([dataBytes(pdf).buffer as ArrayBuffer], { type: "application/pdf" }),
      );
      setPdfBlob(url);
      return () => URL.revokeObjectURL(url);
    } catch {
      return;
    }
  }, [pdf]);
  return (
    <aside className="library-detail" aria-label="论文详情" tabIndex={-1} ref={panel}>
      <header className="library-detail-head">
        <span>论文详情</span>
        <button className="icon-button" onClick={onClose} aria-label="关闭论文详情">
          <Icon name="close" size={18} />
        </button>
      </header>
      <div className="library-detail-content">
        <div className="library-detail-cover">
          <PaperCover paper={paper} large />
          <div className="library-detail-actions">
            <a
              className="button primary"
              href={href({ page: "read", paperId: paper.id, mode: savedFullReadMode() })}
              onClick={
                onRead
                  ? (event) => {
                      if (
                        event.metaKey ||
                        event.ctrlKey ||
                        event.shiftKey ||
                        event.altKey
                      )
                        return;
                      event.preventDefault();
                      onRead(paper);
                    }
                  : undefined
              }
            >
              <Icon name="book" size={16} />
              {continuing ? "继续阅读全文" : "阅读全文"}
            </a>
            {pdf && (
              <a
                className="button"
                href={pdfBlob || pdf}
                target="_blank"
                rel="noopener noreferrer"
              >
                <Icon name="file" size={15} />
                查看原页
              </a>
            )}
            {onUpdate && (
              <button
                className={`button library-star ${paper.starred ? "is-on" : ""}`}
                aria-pressed={Boolean(paper.starred)}
                disabled={saving}
                onClick={() => void update({ starred: !paper.starred })}
              >
                <Icon name="star" size={15} />
                {paper.starred ? "已星标" : "添加星标"}
              </button>
            )}
          </div>
        </div>
        <h2>{paperTitle(paper)}</h2>
        {paper.alternate_title && paper.alternate_title !== paperTitle(paper) && (
          <p className="library-alternate-title">{paper.alternate_title}</p>
        )}
        {paper.authors?.length ? (
          <p className="library-detail-authors">{paper.authors.join("、")}</p>
        ) : null}
        {(paper.year || paper.venue) && (
          <p className="library-publication">
            {[paper.year, paper.venue].filter(Boolean).join(" · ")}
          </p>
        )}
        {onUpdate ? (
          <div className="library-reading-states" aria-label="阅读状态">
            {(["unread", "reading", "read"] as const).map((value) => (
              <button
                key={value}
                aria-pressed={status === value}
                disabled={saving}
                onClick={() => void update({ reading_status: value })}
              >
                {readingLabels[value]}
              </button>
            ))}
          </div>
        ) : (
          <span className={`library-status ${status}`}>{readingLabels[status]}</span>
        )}
        {progress != null && progress > 0 && (
          <div className="library-progress-detail">
            <span>
              阅读进度
              {progress < 1 ? ` · 约 ${Math.round(progress * 100)}%` : " · 文末"}
            </span>
            <div className="library-progress">
              <i style={{ width: `${progress * 100}%` }} />
            </div>
          </div>
        )}
        {error && (
          <p className="library-save-error" role="alert">
            {error}
          </p>
        )}
        {paper.abstract && (
          <section className="library-abstract">
            <h3>摘要</h3>
            <p className={expanded || paper.abstract.length <= 360 ? "is-expanded" : ""}>
              {paper.abstract}
            </p>
            {paper.abstract.length > 360 && (
              <button
                className="text-button"
                aria-expanded={expanded}
                onClick={() => setExpanded(!expanded)}
              >
                {expanded ? "收起摘要" : "展开摘要"}
              </button>
            )}
          </section>
        )}
        <dl className="library-facts">
          {paper.doi && (
            <>
              <dt>DOI</dt>
              <dd>{paper.doi}</dd>
            </>
          )}
          {publication && (
            <>
              <dt>原文链接</dt>
              <dd>
                <a href={publication} target="_blank" rel="noopener noreferrer">
                  原文链接
                  <Icon name="link" size={13} />
                </a>
              </dd>
            </>
          )}
          {paper.pages ? (
            <>
              <dt>篇幅</dt>
              <dd>{paper.pages} 页</dd>
            </>
          ) : null}
          <dt>文件</dt>
          <dd>{paper.source_name}</dd>
        </dl>
        {!embedded && (
          <div className="library-detail-footer">
            {paper.report_url && (
              <button className="button" onClick={() => onDownload(paper)}>
                <Icon name="download" size={15} />
                离线下载
              </button>
            )}
            <button
              className="text-button library-remove"
              onClick={() => onRemove(paper)}
            >
              {source === "native" ? "删除论文" : "移出列表"}
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
