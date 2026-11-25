import { useEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import { href } from "./router";
import {
  libraryPapers,
  paperProgress,
  paperTitle,
  readingLabels,
  type LibrarySort,
  type LibraryView,
} from "./libraryMetadata";
import { PaperCover } from "./PaperCover";
import { PaperDetails, type PaperChanges } from "./PaperDetails";
import type { PaperSummary, Source } from "./api";

const views: [LibraryView, string][] = [
  ["all", "全部"],
  ["reading", "在读"],
  ["unread", "未读"],
  ["read", "已读"],
  ["starred", "星标"],
];

export function Library({
  papers,
  loading,
  error,
  source,
  embedded,
  activeJobs,
  onUpload,
  onRemove,
  onRemoveMany,
  onRetry,
  onDownload,
  onRead,
  onUpdate,
}: {
  papers: PaperSummary[];
  loading: boolean;
  error: string;
  source: Source | null;
  storagePath?: string;
  embedded: boolean;
  activeJobs: number;
  onUpload: () => void;
  onRemove: (paper: PaperSummary) => void;
  /** Resolves with the ids that could not be removed. */
  onRemoveMany: (papers: PaperSummary[]) => Promise<string[]>;
  onRetry: () => void;
  onDownload: (paper: PaperSummary) => void;
  onRead?: (paper: PaperSummary) => void;
  onUpdate?: (paper: PaperSummary, changes: PaperChanges) => Promise<void>;
}) {
  const [filter, setFilter] = useState("");
  const [view, setView] = useState<LibraryView>("all");
  const [sort, setSort] = useState<LibrarySort>("opened");
  const shown = libraryPapers(papers, view, filter, sort);
  const [selected, setSelected] = useState<string | null>(
    embedded && papers.length === 1 ? papers[0].id : null,
  );
  const selectedPaper = papers.find((paper) => paper.id === selected);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const last = useRef<string | null>(null);
  const canBatch = !embedded && papers.length > 0;
  const chosen = shown.filter((paper) => picked.has(paper.id));
  const all = shown.length > 0 && chosen.length === shown.length;
  const stop = () => {
    setSelecting(false);
    setPicked(new Set());
    setConfirming(false);
    last.current = null;
  };
  const toggle = (id: string, range: boolean) => {
    setConfirming(false);
    const from = shown.findIndex((paper) => paper.id === last.current);
    const to = shown.findIndex((paper) => paper.id === id);
    const ids =
      range && from >= 0
        ? shown.slice(Math.min(from, to), Math.max(from, to) + 1).map((paper) => paper.id)
        : [id];
    setPicked((old) => {
      const next = new Set(old);
      const on = !old.has(id);
      for (const value of ids) {
        if (on) next.add(value);
        else next.delete(value);
      }
      return next;
    });
    last.current = id;
  };
  const toggleAll = () => {
    setConfirming(false);
    setPicked(all ? new Set() : new Set(shown.map((paper) => paper.id)));
  };
  const removeChosen = async () => {
    setRemoving(true);
    try {
      const failed = await onRemoveMany(chosen);
      setConfirming(false);
      if (failed.length) setPicked(new Set(failed));
      else stop();
    } finally {
      setRemoving(false);
    }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = (event.target as HTMLElement).closest?.("input, textarea, select");
      if (event.key === "Escape" && !removing) {
        event.preventDefault();
        if (confirming) setConfirming(false);
        else if (selecting) stop();
        else setSelected(null);
      } else if (
        selecting &&
        (event.ctrlKey || event.metaKey) &&
        event.key.toLowerCase() === "a" &&
        !typing
      ) {
        event.preventDefault();
        setPicked(new Set(shown.map((paper) => paper.id)));
      }
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  });
  useEffect(() => {
    if (!papers.length) stop();
  }, [papers.length]);
  return (
    <div
      className={`page library-page ${selectedPaper && !selecting ? "has-detail" : ""}`}
    >
      <header className="page-head library-page-head">
        <div>
          <h1>文献库</h1>
          <p>{papers.length} 篇论文</p>
        </div>
        {!embedded && (
          <div className="page-actions">
            {canBatch && (
              <button
                className="button"
                aria-pressed={selecting}
                onClick={() => (selecting ? stop() : setSelecting(true))}
              >
                <Icon name={selecting ? "check" : "checklist"} size={15} />
                {selecting ? "完成" : "批量管理"}
              </button>
            )}
            <button className="button primary" onClick={onUpload}>
              <Icon name="upload" size={15} />
              导入论文
            </button>
          </div>
        )}
      </header>
      {activeJobs > 0 && (
        <a className="notice" href={href({ page: "upload" })}>
          <span className="spinner" />
          {activeJobs} 篇论文正在解析
          <Icon name="arrow" size={14} />
        </a>
      )}
      <nav className="library-tabs" aria-label="论文分类">
        {views.map(([value, label]) => (
          <button
            key={value}
            aria-pressed={view === value}
            onClick={() => {
              setView(value);
              setConfirming(false);
            }}
          >
            {label}
            <span>
              {
                papers.filter(
                  (paper) =>
                    value === "all" ||
                    (value === "starred"
                      ? paper.starred
                      : (paper.reading_status || "unread") === value),
                ).length
              }
            </span>
          </button>
        ))}
      </nav>
      <div className="library-workspace">
        <section className="library-list" aria-label="论文列表">
          {(!embedded || papers.length > 1) && (
            <div className="library-tools">
              <label className="search-field">
                <Icon name="search" size={15} />
                <input
                  value={filter}
                  onChange={(event) => {
                    setFilter(event.target.value);
                    setConfirming(false);
                  }}
                  placeholder="搜索标题、作者或摘要"
                  aria-label="搜索论文"
                />
              </label>
              <select
                value={sort}
                onChange={(event) => setSort(event.target.value as LibrarySort)}
                aria-label="论文排序"
              >
                <option value="opened">最近阅读</option>
                <option value="added">最近添加</option>
                <option value="year">出版年份</option>
                <option value="title">标题</option>
              </select>
            </div>
          )}
          {error ? (
            <div className="empty">
              <Icon name="warning" size={22} />
              <strong>文献库暂时无法读取</strong>
              <p>{error}</p>
              <button className="button" onClick={onRetry}>
                <Icon name="refresh" size={15} />
                重试
              </button>
            </div>
          ) : loading && !papers.length ? (
            <div className="paper-rows" aria-busy="true">
              {[0, 1, 2].map((index) => (
                <div className="paper-row skeleton" key={index} />
              ))}
            </div>
          ) : !papers.length ? (
            <button className="empty is-action" onClick={onUpload}>
              <Icon name="upload" size={24} />
              <strong>还没有论文</strong>
              <p>上传 PDF，开始阅读。</p>
            </button>
          ) : (
            <div className={`paper-rows ${selecting ? "is-selecting" : ""}`}>
              {shown.map((paper) => {
                const checked = picked.has(paper.id);
                const progress = paperProgress(paper);
                const status = paper.reading_status || "unread";
                const body = (
                  <>
                    {selecting && (
                      <span
                        className={`check ${checked ? "is-on" : ""}`}
                        aria-hidden="true"
                      >
                        <Icon name="check" size={13} />
                      </span>
                    )}
                    <PaperCover paper={paper} />
                    <span className="library-row-text">
                      <strong>
                        {paper.starred && <Icon name="star" size={13} />}
                        {paperTitle(paper)}
                      </strong>
                      <small>
                        {[
                          (paper.authors || []).slice(0, 3).join("、") +
                            ((paper.authors?.length || 0) > 3 ? " 等" : ""),
                          paper.year,
                          paper.venue,
                        ]
                          .filter(Boolean)
                          .join(" · ") || paper.source_name}
                      </small>
                      <span className="library-row-bottom">
                        <span className={`library-status ${status}`}>
                          {readingLabels[status]}
                        </span>
                        {paper.pages ? <span>{paper.pages} 页</span> : null}
                        {paper.status === "partial" && (
                          <span className="library-partial">部分完成</span>
                        )}
                      </span>
                      {progress != null && progress > 0 && (
                        <span
                          className="library-progress"
                          aria-label={`阅读进度约 ${Math.round(progress * 100)}%`}
                        >
                          <i style={{ width: `${progress * 100}%` }} />
                        </span>
                      )}
                    </span>
                  </>
                );
                return (
                  <article
                    className={`paper-row ${checked ? "is-selected" : ""} ${selected === paper.id && !selecting ? "is-open" : ""}`}
                    key={paper.id}
                  >
                    {selecting ? (
                      <button
                        className="paper-main"
                        role="checkbox"
                        aria-checked={checked}
                        onClick={(event) => toggle(paper.id, event.shiftKey)}
                      >
                        {body}
                      </button>
                    ) : (
                      <button
                        className="paper-main"
                        aria-expanded={selected === paper.id}
                        aria-label={`查看《${paperTitle(paper)}》详情`}
                        onClick={() => setSelected(paper.id)}
                      >
                        {body}
                      </button>
                    )}
                  </article>
                );
              })}
              {!shown.length && (
                <div className="library-no-results">
                  <Icon name="search" size={22} />
                  <p>{filter ? `没有匹配“${filter}”的论文` : "这个分类还没有论文"}</p>
                </div>
              )}
            </div>
          )}
        </section>
        {!selecting && selectedPaper && (
          <PaperDetails
            key={selectedPaper.id}
            paper={selectedPaper}
            embedded={embedded}
            source={source}
            onClose={() => setSelected(null)}
            onRead={onRead}
            onUpdate={onUpdate}
            onRemove={onRemove}
            onDownload={onDownload}
          />
        )}
      </div>
      {selecting && (
        <div className="batch-bar" role="toolbar" aria-label="批量操作">
          {confirming ? (
            <>
              <span className="batch-question">
                <Icon name="warning" size={15} />
                {source === "native"
                  ? `删除 ${chosen.length} 篇论文及其解析文件？无法撤销，原 PDF 保留。`
                  : `移出列表 ${chosen.length} 篇论文？本地文件仍保留。`}
              </span>
              <button
                className="button small"
                disabled={removing}
                onClick={() => setConfirming(false)}
              >
                取消
              </button>
              <button
                className="button small danger"
                autoFocus
                disabled={removing}
                onClick={() => void removeChosen()}
              >
                {removing ? (
                  <span className="spinner" />
                ) : (
                  <Icon name="trash" size={14} />
                )}
                {removing ? "正在删除…" : source === "native" ? "确认删除" : "确认移除"}
              </button>
            </>
          ) : (
            <>
              <button
                className="batch-all"
                role="checkbox"
                aria-checked={all ? true : chosen.length ? "mixed" : false}
                onClick={toggleAll}
              >
                <span
                  className={`check ${all ? "is-on" : chosen.length ? "is-mixed" : ""}`}
                  aria-hidden="true"
                >
                  <Icon name={all ? "check" : "minus"} size={13} />
                </span>
                全选
              </button>
              <span className="batch-count">
                {chosen.length ? (
                  <>
                    已选 <strong>{chosen.length}</strong> / {shown.length} 篇
                  </>
                ) : (
                  <span className="muted">点选论文，按住 Shift 可连选</span>
                )}
              </span>
              <button className="button small" onClick={stop}>
                取消
              </button>
              <button
                className="button small danger"
                disabled={!chosen.length}
                onClick={() => setConfirming(true)}
              >
                <Icon name="trash" size={14} />
                {source === "native" ? "删除所选" : "移除所选"}
                {chosen.length > 0 && ` ${chosen.length}`}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
