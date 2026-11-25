import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./icons";
import { GraphCanvas } from "./GraphCanvas";
import { NodeDetails } from "./EvidencePanel";
import { FocusReader } from "./FocusReader";
import { Outline } from "./Outline";
import {
  childCounts,
  compileGraph,
  kindLabels,
  validateAll,
  visibleNodeIds,
} from "./graph";
import {
  downloadFile,
  exportPng,
  fileStem,
  standaloneSvg,
  toMarkdown,
  toPlainText,
} from "./export";
import { GlossaryView, Highlight, QualityView, paperTypes } from "./ReaderViews";
import { ContinuousTextView } from "./ContinuousTextView";
import { ReadingOverview } from "./ReadingOverview";
import { READING_MODES } from "./readerNavigation";
import { AnalysisText } from "./AnalysisText";
import { useNotebook } from "./useNotebook";
import { ReadingSelectionNote } from "./ReadingSelectionNote";
import { ReadingHighlights } from "./ReadingHighlights";
import { readingDocument, readingText } from "./reading";
import { readPaperSummary } from "./libraryMetadata";
import { SelectionExplain, type Picked } from "./SelectionExplain";
import { QuickChat, addToQuick } from "./QuickChat";
import { addAnnotation, useAnnotations } from "./annotations";
import type { AnnotationAppearance, PaperSummary } from "./api";
import { isTyping } from "./Shortcuts";
import { backgroundShortcutBlocked } from "./dialog";
import { readingMatches } from "./readingSearch";
import { go, type ReaderMode } from "./router";
import { initialFold } from "./fold";
import { useMedia, type Prefs } from "./prefs";
import type { DashboardReport, ReadingItem } from "./types";

const readingModes = READING_MODES;

export function Reader({
  report,
  paperId,
  mode,
  outlineSlot,
  termSidebarSlot,
  termsOpen = false,
  onOpenTerms,
  onCloseTerms,
  prefs,
  onPrefs,
  canExplain,
  canChat = false,
  canAnnotate = false,
  canTranslate = false,
  canReferenceTree = false,
  canNotes = false,
  canAutoNotes = false,
  canStream = false,
  papers = [],
  onToast,
  onSaveOffline,
  onLoadComplete,
}: {
  report: DashboardReport;
  paperId: string;
  mode: ReaderMode;
  outlineSlot: HTMLElement | null;
  termSidebarSlot?: HTMLElement | null;
  termsOpen?: boolean;
  onOpenTerms?: () => void;
  onCloseTerms?: () => void;
  prefs: Prefs;
  onPrefs: (patch: Partial<Prefs>) => void;
  canExplain: boolean;
  /** 问一问 beside the paper. */
  canChat?: boolean;
  /** 勾画 kept by the local service. */
  canAnnotate?: boolean;
  /** 对照 / 译文, translated on request by the local service. */
  canTranslate?: boolean;
  canReferenceTree?: boolean;
  canNotes?: boolean;
  canAutoNotes?: boolean;
  canStream?: boolean;
  /** The library, for referencing other papers from the quick chat. */
  papers?: PaperSummary[];
  onToast: (message: string) => void;
  /** Present when the page is served; saves a standalone copy of this reader. */
  onSaveOffline?: () => Promise<void>;
  /** Fetch embedded PDF/images for an explicit data export of an online report. */
  onLoadComplete?: () => Promise<DashboardReport>;
}) {
  const overviewOpen = mode === "overview";
  const [viewId, setViewId] = useState(report.graphs[0]?.id || "");
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ id: string; tick: number } | null>(null);
  const [collapsed, setCollapsed] = useState(() =>
    initialFold(report, report.graphs[0], prefs.mapDepth),
  );
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  /** Which result group shows every match, keyed to the query it was opened for. */
  const [allOf, setAllOf] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchIndex, setSearchIndex] = useState(-1);
  const [searchTarget, setSearchTarget] = useState<{ id: string; tick: number } | null>(
    null,
  );
  useEffect(() => {
    setSearchIndex(-1);
    setSearchTarget(null);
  }, [query]);
  const [exportsOpen, setExportsOpen] = useState(false);
  const [panelWidth, setPanelWidth] = useState(380);
  const [mobilePanel, setMobilePanel] = useState(false);
  /** Node open in the focused reading view, if any. */
  const [reading, setReading] = useState<string | null>(null);
  const [quickOpen, setQuickOpen] = useState(false);
  const annotations = useAnnotations(paperId, canAnnotate);
  const notebook = useNotebook({
    paperId,
    report,
    remote: canNotes,
    canAI: canNotes && canChat,
    canAutoNotes,
    stream: canStream,
    onToast,
    annotations: annotations.items || undefined,
    onOpen: () => setQuickOpen(false),
  });
  const mobile = useMedia("(max-width: 720px)");
  const svgRef = useRef<SVGSVGElement | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const dark =
    prefs.theme === "dark" || document.documentElement.dataset.theme === "dark";

  const nodes = report.analysis.nodes;
  const selectedNode = nodes.find((n) => n.id === selected);
  const graph = report.graphs.find((g) => g.id === viewId) || report.graphs[0];
  const modes = readingModes;
  const activeMode =
    mode === "results" ? "text" : modes.some(([id]) => id === mode) ? mode : "map";
  const setMode = useCallback(
    (next: ReaderMode) => {
      go({ page: "read", paperId, mode: next });
    },
    [paperId],
  );
  const openArgument = () => {
    const argument = report.graphs.find((view) => view.id === "argument");
    if (argument) {
      setViewId(argument.id);
      setCollapsed(initialFold(report, argument, prefs.mapDepth));
      setAnchorId(null);
      setFocus(null);
    }
    setMode("map");
  };

  const search = query.trim().toLocaleLowerCase();
  const nodeMatches = useMemo(
    () =>
      search
        ? nodes.filter((n) =>
            `${n.title} ${n.summary} ${n.explanation}`
              .toLocaleLowerCase()
              .includes(search),
          )
        : [],
    [nodes, search],
  );
  const textMatches = useMemo(() => readingMatches(report, query), [report, search]);
  const matchIds = useMemo(() => new Set(nodeMatches.map((n) => n.id)), [nodeMatches]);
  const visible = useMemo(
    () => (graph ? visibleNodeIds(report, graph, collapsed) : new Set<string>()),
    [report, graph, collapsed],
  );
  const children = useMemo(
    () => (graph ? childCounts(report, graph) : new Map<string, number>()),
    [report, graph],
  );
  const source = useMemo(
    () =>
      graph
        ? compileGraph(report, graph, collapsed)
        : 'flowchart LR\nempty["暂无可显示的导图"]',
    [report, graph, collapsed],
  );
  const warnings = useMemo(
    () => [
      ...new Set([
        ...report.quality.warnings,
        ...report.analysis.warnings,
        ...report.document.warnings,
      ]),
    ],
    [report],
  );
  const onReady = useCallback((svg: SVGSVGElement | null) => {
    svgRef.current = svg;
  }, []);

  const paperTitle =
    papers.find((p) => p.id === paperId)?.title || readPaperSummary(report).title;
  const askItem = (item: ReadingItem) => {
    const error = addToQuick(paperId, paperTitle, {
      kind: "reading_item",
      paper_id: paperId,
      paper_title: paperTitle,
      item_id: item.id,
      source_version: readingDocument(report).source_version,
      label:
        item.label ||
        {
          formula: "公式",
          table: "表格",
          figure: "图片",
          reference: "参考文献",
          algorithm: "算法",
          code: "代码",
          heading: "章节",
          paragraph: "原文段落",
        }[item.kind],
      text: readingText(item, new Map(report.document.blocks.map((b) => [b.id, b]))),
      page: item.page,
    });
    if (error) onToast(error);
    else {
      notebook.setCurrent(item.id);
      setQuickOpen(true);
    }
  };
  useEffect(() => {
    document.title = `${paperTitle} · PaperLens`;
    window.__paperlensValidate = () => validateAll(report);
    // Partial native editions can have no graphs. Their reader and validator
    // are still ready even though GraphCanvas never mounts.
    window.__paperlensReaderReady = true;
    const key = (e: KeyboardEvent) => {
      if (backgroundShortcutBlocked(e)) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchRef.current?.focus();
        setSearchOpen(true);
      }
      if (e.key === "Escape") {
        setSearchOpen(false);
        setExportsOpen(false);
        setMobilePanel(false);
        if (!isTyping(e) && !document.querySelector(".modal-backdrop")) setSelected(null);
      }
      if (
        e.key === "/" &&
        canChat &&
        !isTyping(e) &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !document.querySelector(".modal-backdrop")
      ) {
        e.preventDefault();
        setQuickOpen(true);
        // Already open: put the cursor back in its box.
        document.querySelector<HTMLTextAreaElement>(".quick-chat textarea")?.focus();
      }
      const n = Number(e.key);
      if (n >= 1 && n <= 5 && !isTyping(e) && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const next = readingModes[n - 1];
        if (next) setMode(next[0]);
      }
    };
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("keydown", key);
      window.__paperlensReaderReady = false;
      document.title = "PaperLens · 文献库工作台";
    };
  }, [report, setMode, canChat, paperTitle]);

  // The library's title is the one the user sees (and may have edited) elsewhere.
  const closeQuick = useCallback(() => setQuickOpen(false), []);
  /** Takes picked words into this paper's quick chat as a quoted passage. */
  const askAbout = (picked: Picked, annotationId?: string) => {
    const block = report.document.blocks.find((b) => b.id === picked.block_id);
    const error = addToQuick(paperId, paperTitle, {
      kind: "passage",
      paper_id: paperId,
      paper_title: paperTitle,
      block_id: picked.block_id,
      start: picked.start,
      end: picked.end,
      text: picked.text,
      page: block?.page,
      annotation_id: annotationId,
    });
    if (error) onToast(error);
    setQuickOpen(true);
    requestAnimationFrame(() =>
      document.querySelector<HTMLTextAreaElement>(".quick-chat textarea")?.focus(),
    );
  };
  const annotate = async (picked: Picked, appearance?: AnnotationAppearance) => {
    const created = await addAnnotation(paperId, picked, "", appearance);
    onToast("已勾画");
    return created;
  };

  const navigate = useCallback(
    (id: string) => {
      const next = nodes.find((n) => n.id === id);
      if (!next) return;
      const ancestors = new Set<string>();
      let parent = next.parent_id;
      while (parent && !ancestors.has(parent)) {
        ancestors.add(parent);
        parent = nodes.find((n) => n.id === parent)?.parent_id || null;
      }
      setAnchorId(null);
      setCollapsed((previous) =>
        [...previous].some((v) => ancestors.has(v))
          ? new Set([...previous].filter((v) => !ancestors.has(v)))
          : previous,
      );
      setViewId((current) => {
        const active = report.graphs.find((g) => g.id === current);
        return active?.node_ids.includes(id)
          ? current
          : report.graphs.find((g) => g.node_ids.includes(id))?.id || current;
      });
      setSelected(id);
      setFocus({ id, tick: Date.now() });
      setSearchOpen(false);
      setMobilePanel(true);
      if (activeMode !== "map") setMode("map");
    },
    [nodes, report, activeMode, setMode],
  );
  const selectNode = useCallback((id: string) => {
    setSelected((old) => (old === id ? old : id));
    setMobilePanel(true);
  }, []);
  const openReading = useCallback((id: string) => {
    setMobilePanel(false);
    setReading(id);
  }, []);
  /** Enter: the first press opens the details, the second reads it in focus. */
  const activateNode = useCallback(
    (id: string) => {
      if (selected === id) openReading(id);
      else selectNode(id);
    },
    [selected, selectNode, openReading],
  );
  const closeReading = useCallback(
    (id: string) => {
      setReading(null);
      navigate(id);
    },
    [navigate],
  );
  useEffect(() => setReading(null), [activeMode]);
  const toggleNode = useCallback((id: string) => {
    setAnchorId(id);
    setCollapsed((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  /** Arrow keys on a focused map node walk the tree instead of panning. */
  const stepFrom = useCallback(
    (id: string, key: string): string | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node) return null;
      let target: string | null = null;
      if (key === "ArrowLeft") {
        target = node.parent_id && visible.has(node.parent_id) ? node.parent_id : null;
      } else if (key === "ArrowRight") {
        if (collapsed.has(id)) {
          toggleNode(id);
          return id;
        }
        target = nodes.find((n) => n.parent_id === id && visible.has(n.id))?.id || null;
      } else {
        const siblings = nodes.filter(
          (n) => n.parent_id === node.parent_id && visible.has(n.id),
        );
        const index = siblings.findIndex((n) => n.id === id);
        target = siblings[index + (key === "ArrowUp" ? -1 : 1)]?.id || null;
      }
      if (target) navigate(target);
      return target;
    },
    [nodes, visible, collapsed, toggleNode, navigate],
  );
  const collapseAll = () => {
    setAnchorId(null);
    setCollapsed(
      new Set(
        nodes
          .filter((n) => n.parent_id && nodes.some((c) => c.parent_id === n.id))
          .map((n) => n.id),
      ),
    );
  };

  const fileName = fileStem(paperTitle);
  const exportReport = async (format: string) => {
    setExportsOpen(false);
    if (format === "html") return onSaveOffline?.();
    try {
      if (format === "json")
        downloadFile(
          JSON.stringify(onLoadComplete ? await onLoadComplete() : report, null, 2),
          `${fileName}.json`,
          "application/json",
        );
      else if (format === "md")
        downloadFile(toMarkdown(report), `${fileName}.md`, "text/markdown;charset=utf-8");
      else if (format === "txt")
        downloadFile(
          toPlainText(report),
          `${fileName}-全文.txt`,
          "text/plain;charset=utf-8",
        );
      else if (format === "mmd")
        downloadFile(
          graph ? compileGraph(report, graph, collapsed, { relations: true }) : source,
          `${fileName}-${viewId}.mmd`,
          "text/plain;charset=utf-8",
        );
      else if (!svgRef.current) throw new Error("请先打开导图并等待绘制完成。");
      else if (format === "svg")
        downloadFile(
          standaloneSvg(svgRef.current).text,
          `${fileName}-${viewId}.svg`,
          "image/svg+xml;charset=utf-8",
        );
      else if (format === "png")
        await exportPng(svgRef.current, `${fileName}-${viewId}.png`);
      onToast("导出文件已准备好");
    } catch (error) {
      onToast(`导出未完成：${String(error)}`);
    }
  };
  const showTextMatch = (index: number) => {
    const match = textMatches[index];
    if (!match) return;
    setMode("text");
    setSearchOpen(false);
    setSearchIndex(index);
    setSearchTarget((old) => ({ id: match.id, tick: (old?.tick || 0) + 1 }));
  };

  const outline =
    outlineSlot && activeMode === "map" && !overviewOpen
      ? createPortal(
          <Outline
            report={report}
            collapsed={collapsed}
            selected={selected}
            onToggle={toggleNode}
            onSelect={navigate}
          />,
          outlineSlot,
        )
      : null;

  return (
    <div
      className={`reader mode-${overviewOpen ? "overview" : activeMode}`}
      style={{ "--panel-width": `${panelWidth}px` } as React.CSSProperties}
    >
      {outline}
      <header className="reader-head">
        <div className="reader-title">
          <h1 title={paperTitle}>{paperTitle}</h1>
          <div className="meta-line">
            <span>{paperTypes[report.analysis.paper_type]}</span>
            <span>{report.document.pages.length} 页</span>
            <span>
              {report.evidence.length} 条证据 · {report.quality.grounded_nodes}/
              {report.quality.total_nodes} 论点有来源
            </span>
            {report.run.status === "partial" && (
              <button className="meta-warn" onClick={() => setMode("quality")}>
                <Icon name="warning" size={13} />
                部分完成
              </button>
            )}
          </div>
        </div>
        <div className="reader-actions">
          <div className="search">
            <label className="search-field">
              <Icon name="search" size={15} />
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setSearchOpen(true);
                }}
                onFocus={() => setSearchOpen(true)}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    !event.nativeEvent.isComposing &&
                    textMatches.length
                  ) {
                    event.preventDefault();
                    showTextMatch(searchIndex < 0 ? 0 : searchIndex);
                  }
                }}
                placeholder="搜索论点与原文"
                aria-label="搜索论点和全文"
              />
              {query ? (
                <button aria-label="清空搜索" onClick={() => setQuery("")}>
                  <Icon name="close" size={14} />
                </button>
              ) : (
                <kbd>Ctrl K</kbd>
              )}
            </label>
            {searchOpen && search && (
              <div className="search-results popover">
                <div className="popover-head">
                  <span>
                    {nodeMatches.length} 个论点 · {textMatches.length} 处原文
                  </span>
                  <button aria-label="关闭搜索结果" onClick={() => setSearchOpen(false)}>
                    <Icon name="close" size={14} />
                  </button>
                </div>
                {nodeMatches.length > 0 && (
                  <div className="result-group">
                    <div className="group-label">论点</div>
                    {nodeMatches
                      .slice(0, allOf === `nodes:${search}` ? undefined : 8)
                      .map((n) => (
                        <button
                          className="search-result"
                          key={n.id}
                          onClick={() => navigate(n.id)}
                        >
                          <span className={`kind-dot kind-${n.kind}`} />
                          <span>
                            <strong>
                              <Highlight text={n.title} query={query} />
                            </strong>
                            <small>
                              <Highlight text={n.summary.slice(0, 90)} query={query} />
                            </small>
                          </span>
                        </button>
                      ))}
                    <MoreResults
                      total={nodeMatches.length}
                      open={allOf === `nodes:${search}`}
                      onOpen={() => setAllOf(`nodes:${search}`)}
                    />
                  </div>
                )}
                {textMatches.length > 0 && (
                  <div className="result-group">
                    <div className="group-label">原文</div>
                    {textMatches
                      .slice(0, allOf === `text:${search}` ? undefined : 8)
                      .map((b, indexInMatches) => {
                        const index = b.text.toLocaleLowerCase().indexOf(search);
                        return (
                          <button
                            className="search-result"
                            key={b.id}
                            onClick={() => showTextMatch(indexInMatches)}
                          >
                            <span className="page-badge">P{b.page}</span>
                            <span>
                              <strong>{b.section || "原文段落"}</strong>
                              <small>
                                <Highlight
                                  text={`${index > 30 ? "…" : ""}${b.text.slice(Math.max(0, index - 30), index + search.length + 80)}`}
                                  query={query}
                                />
                              </small>
                            </span>
                          </button>
                        );
                      })}
                    <MoreResults
                      total={textMatches.length}
                      open={allOf === `text:${search}`}
                      onOpen={() => setAllOf(`text:${search}`)}
                    />
                  </div>
                )}
                {!nodeMatches.length && !textMatches.length && (
                  <p className="empty-line">没有找到匹配内容，试试更短的关键词。</p>
                )}
              </div>
            )}
          </div>
          {/* A view preference, kept apart from the two task buttons that follow. */}
          <button
            className="icon-button"
            aria-label={dark ? "切换浅色主题" : "切换深色主题"}
            title={dark ? "浅色主题" : "深色主题"}
            onClick={() => onPrefs({ theme: dark ? "light" : "dark" })}
          >
            <Icon name={dark ? "sun" : "moon"} size={17} />
          </button>
          {canChat && (
            <button
              className={`button quick-toggle ${quickOpen ? "is-on" : ""}`}
              aria-pressed={quickOpen}
              title="问一问：围绕这篇论文临时提问（/）"
              onClick={() => setQuickOpen(!quickOpen)}
            >
              <Icon name="chat" size={15} />
              <span>问一问</span>
            </button>
          )}
          <div className="export">
            <button
              className="button export-button"
              aria-label="导出"
              aria-expanded={exportsOpen}
              onClick={() => setExportsOpen(!exportsOpen)}
            >
              <Icon name="download" size={15} />
              <span>导出</span>
            </button>
            {exportsOpen && (
              <div className="export-menu popover">
                {[
                  ...(onSaveOffline ? [["html", "离线阅读", "HTML"]] : []),
                  ["md", "论文分析", "Markdown"],
                  ["json", "完整数据", "JSON"],
                  ["txt", "纯文本", "TXT"],
                  ["mmd", "导图代码", "Mermaid"],
                  ["svg", "矢量导图", "SVG"],
                  ["png", "导图图片", "PNG"],
                ].map(([id, title, type]) => (
                  <button key={id} onClick={() => void exportReport(id)}>
                    <span>{title}</span>
                    <small>{type}</small>
                  </button>
                ))}
                <p className="export-note">
                  SVG 与 PNG 保留当前导图的布局、可见关联线和主题配色；
                  导图代码包含当前节点间的全部关联线。
                </p>
              </div>
            )}
          </div>
        </div>
        <nav className="mode-nav" aria-label={mobile ? "手机阅读空间" : "阅读空间"}>
          {modes.map(([id, icon, title], index) => (
            <button
              key={id}
              aria-pressed={activeMode === id}
              title={`${title}（${index + 1}）`}
              aria-keyshortcuts={String(index + 1)}
              onClick={() => {
                setMode(id);
              }}
            >
              <Icon name={icon} size={15} />
              <span>{title}</span>
            </button>
          ))}
        </nav>
      </header>

      <div className={`reader-body ${quickOpen ? "has-quick" : ""}`}>
        <main className="reader-main">
          {overviewOpen && (
            <ReadingOverview
              report={report}
              onMap={openArgument}
              onText={() => setMode("text")}
              onNavigate={navigate}
            />
          )}
          {!overviewOpen && activeMode === "map" && graph && (
            <div className="map-stage">
              <div className="view-tabs" role="tablist" aria-label="导图视图">
                {report.graphs.map((view) => (
                  <button
                    role="tab"
                    aria-selected={viewId === view.id}
                    key={view.id}
                    title={view.description}
                    onClick={() => {
                      setAnchorId(null);
                      setViewId(view.id);
                      setFocus(null);
                      if (view.id !== viewId)
                        setCollapsed(initialFold(report, view, prefs.mapDepth));
                    }}
                  >
                    {view.title}
                  </button>
                ))}
                <span className="view-count">{visible.size} 个论点</span>
              </div>
              <GraphCanvas
                report={report}
                graph={graph}
                source={source}
                visible={visible}
                collapsed={collapsed}
                childCount={children}
                selected={selected}
                matches={matchIds}
                focus={focus}
                anchorId={anchorId}
                showRelations={prefs.showRelations}
                onShowRelations={(showRelations) => onPrefs({ showRelations })}
                onSelect={selectNode}
                onActivate={activateNode}
                onStep={stepFrom}
                onToggle={toggleNode}
                onCollapseAll={collapseAll}
                onExpandAll={() => {
                  setAnchorId(null);
                  setCollapsed(new Set());
                }}
                onReady={onReady}
              />
            </div>
          )}
          {!overviewOpen && activeMode === "text" && (
            <ContinuousTextView
              outlineSlot={outlineSlot}
              termSidebarSlot={termSidebarSlot}
              termsOpen={termsOpen}
              onOpenTerms={onOpenTerms}
              onCloseTerms={onCloseTerms}
              report={report}
              paperId={paperId}
              canTranslate={canTranslate && paperId !== "local"}
              canReferenceTree={canReferenceTree && paperId !== "local"}
              onAskPassage={canChat ? (picked) => askAbout(picked) : undefined}
              onAskItem={notebook.canAI ? askItem : undefined}
              onCreateNote={notebook.open}
              onCheckItem={
                notebook.canAI
                  ? (item) => void notebook.check(item).catch((e) => onToast(String(e)))
                  : undefined
              }
              noteGroups={notebook.margin}
              renderNotes={notebook.cards}
              noteCount={notebook.count}
              sidePanelOpen={quickOpen || notebook.panelOpen}
              onCurrentItem={notebook.setCurrent}
              query={query}
              searchIndex={searchIndex}
              onSearchIndexChange={setSearchIndex}
              searchTarget={searchTarget}
              onClearQuery={() => setQuery("")}
              onExport={() => void exportReport("txt")}
              selectionTool={
                <>
                  <SelectionExplain
                    paperId={paperId}
                    report={report}
                    canExplain={canExplain}
                    onAsk={canChat ? askAbout : undefined}
                    onAnnotate={canAnnotate ? annotate : undefined}
                    onNote={(picked, kind) => {
                      const item = notebook.forPick(picked);
                      if (item) notebook.open(item, kind, picked.text);
                    }}
                  />
                  <ReadingSelectionNote
                    report={report}
                    paperId={paperId}
                    onMark={notebook.mark}
                    rawHighlighter={canAnnotate}
                    onNote={notebook.open}
                    onAsk={notebook.canAI ? askItem : undefined}
                  />
                  <ReadingHighlights
                    report={report}
                    paperId={paperId}
                    notes={notebook.marks}
                    onEdit={notebook.edit}
                    onChange={notebook.changeMark}
                    onDelete={notebook.removeMark}
                  />
                </>
              }
              marks={annotations.items || undefined}
              onMark={(a) => notebook.showAnnotation(a.id)}
              toolbarExtra={notebook.toolbar}
            />
          )}
          {!overviewOpen && activeMode === "glossary" && (
            <GlossaryView report={report} paperId={paperId} />
          )}
          {!overviewOpen && activeMode === "quality" && (
            <QualityView
              report={report}
              warnings={warnings}
              paperId={paperId}
              onNavigate={navigate}
            />
          )}
        </main>

        {!overviewOpen && activeMode === "map" && (
          <>
            <div
              className="panel-resizer"
              role="separator"
              aria-label="调整详情面板宽度"
              aria-orientation="vertical"
              aria-valuenow={panelWidth}
              aria-valuemin={300}
              aria-valuemax={600}
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") setPanelWidth((w) => Math.min(600, w + 24));
                if (e.key === "ArrowRight") setPanelWidth((w) => Math.max(300, w - 24));
              }}
              onPointerDown={(e) => e.currentTarget.setPointerCapture(e.pointerId)}
              onPointerMove={(e) => {
                if (e.currentTarget.hasPointerCapture(e.pointerId))
                  setPanelWidth(
                    Math.min(600, Math.max(300, window.innerWidth - e.clientX)),
                  );
              }}
              onPointerUp={(e) => e.currentTarget.releasePointerCapture(e.pointerId)}
            />
            <aside
              className={`detail-panel ${mobilePanel && selectedNode ? "is-open" : ""}`}
              aria-label={selectedNode ? "论点详情" : "阅读指南"}
            >
              {selectedNode ? (
                <>
                  <div className="panel-bar">
                    <span>论点详情</span>
                    <button
                      className="text-button panel-focus"
                      title="按 Enter 逐条阅读"
                      onClick={() => openReading(selectedNode.id)}
                    >
                      <Icon name="expand" size={14} />
                      专注阅读
                    </button>
                    <button
                      className="icon-button small"
                      aria-label="返回阅读指南"
                      onClick={() => {
                        setSelected(null);
                        setMobilePanel(false);
                      }}
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                  <div className="panel-scroll">
                    <NodeDetails
                      node={selectedNode}
                      report={report}
                      onCollapse={() => toggleNode(selectedNode.id)}
                      isCollapsed={collapsed.has(selectedNode.id)}
                      hasChildren={nodes.some((n) => n.parent_id === selectedNode.id)}
                      onNavigate={navigate}
                    />
                  </div>
                </>
              ) : (
                <div className="panel-scroll">
                  <Guide
                    report={report}
                    onStart={navigate}
                    onRead={(id) => {
                      setSelected(id);
                      openReading(id);
                    }}
                  />
                </div>
              )}
            </aside>
          </>
        )}
        {quickOpen && (
          <QuickChat
            paperId={paperId}
            title={paperTitle}
            papers={papers}
            canHighlights={canAnnotate}
            onClose={closeQuick}
            onToast={onToast}
            onPin={notebook.pin}
          />
        )}
      </div>
      {notebook.overlays}
      {reading && graph && (
        <FocusReader
          report={report}
          graph={
            graph.node_ids.includes(reading)
              ? graph
              : report.graphs.find((g) => g.node_ids.includes(reading)) || graph
          }
          startId={reading}
          onClose={closeReading}
        />
      )}
    </div>
  );
}

function Guide({
  report,
  onStart,
  onRead,
}: {
  report: DashboardReport;
  onStart: (id: string) => void;
  onRead: (id: string) => void;
}) {
  const { analysis } = report;
  const first = analysis.nodes.find((n) => n.kind === "question") || analysis.nodes[0];
  const kinds = Object.entries(
    analysis.nodes.reduce<Record<string, number>>((acc, n) => {
      acc[n.kind] = (acc[n.kind] || 0) + 1;
      return acc;
    }, {}),
  );
  return (
    <div className="guide">
      <section>
        <div className="eyebrow">一句话</div>
        <p className="lede">
          <AnalysisText text={analysis.summary} />
        </p>
      </section>
      {analysis.research_question && (
        <section>
          <div className="eyebrow">研究问题</div>
          <p className="question">
            <AnalysisText text={analysis.research_question} />
          </p>
        </section>
      )}
      {analysis.reading_guide.length > 0 && (
        <section>
          <div className="eyebrow">阅读建议</div>
          <ol className="steps">
            {analysis.reading_guide.map((step, i) => (
              <li key={i}>
                <AnalysisText text={step} />
              </li>
            ))}
          </ol>
        </section>
      )}
      <section>
        <div className="eyebrow">论点构成</div>
        <div className="kind-summary">
          {kinds.map(([kind, count]) => (
            <span key={kind}>
              <i className={`kind-dot kind-${kind}`} />
              {kindLabels[kind as keyof typeof kindLabels]} {count}
            </span>
          ))}
        </div>
      </section>
      {first && (
        <button className="button primary block" onClick={() => onStart(first.id)}>
          研究问题
          <Icon name="arrow" size={15} />
        </button>
      )}
      {analysis.nodes[0] && (
        <button className="button block" onClick={() => onRead(analysis.nodes[0].id)}>
          <Icon name="book" size={15} />
          逐条阅读
        </button>
      )}
      <p className="footnote">点击要点，查看解释与原文。</p>
    </div>
  );
}

function MoreResults({
  total,
  open,
  onOpen,
}: {
  total: number;
  open: boolean;
  onOpen: () => void;
}) {
  if (open || total <= 8) return null;
  return (
    <button className="more-results" onClick={onOpen}>
      显示其余 {total - 8} 条
    </button>
  );
}
