import { confirmProcessingBudget } from "./processingConfirmation";
import { confirmInputBudget } from "./inputBudget";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Icon } from "./icons";
import { CurrencyProvider } from "./currency";
import { Sidebar, type ServiceState } from "./Sidebar";
import { Library } from "./Library";
import { readPaperSummary } from "./libraryMetadata";
import { readLocal, writeLocal } from "./readingExperience";
import { UploadPage } from "./UploadPage";
import { ProcessingReader } from "./ProcessingReader";
import { Settings } from "./Settings";
import { TermsPage } from "./TermsPage";
import { Reader } from "./Reader";
import { ChatPage } from "./ChatPage";
import { configureChat } from "./chat";
import { go, href, parseRoute, useHash, type ReaderMode, type Route } from "./router";
import { useMedia, usePrefs, type Prefs } from "./prefs";
import { Shortcuts, isTyping } from "./Shortcuts";
import { backgroundShortcutBlocked } from "./dialog";
import {
  getHealth,
  clearJobHistory,
  hasFeature,
  jobAction,
  listJobs,
  listLibrary,
  loadReport,
  loadCompleteReport,
  offlineHtml,
  removePaper,
  serverMode,
  submitJob,
  updatePaper,
  type Health,
  type JobAction,
  type JobState,
  type PaperSummary,
  type ProcessingProfileId,
  type ProcessingSpeedMode,
  type Source,
} from "./api";
import { downloadFile, fileStem } from "./export";
import { mergeUploadFiles, pdfFingerprint } from "./uploadIdentity";
import type { DashboardReport } from "./types";

const ACTIVE = new Set(["queued", "running", "paused"]);
const LOCAL_ID = "local";

function summarize(report: DashboardReport): PaperSummary {
  const summary = readPaperSummary(report, LOCAL_ID);
  const stored = readLocal(
    `paperlens:paper-state:${report.document.sha256}`,
  ) as Partial<PaperSummary> | null;
  return {
    ...summary,
    starred: stored?.starred ?? false,
    reading_status: stored?.reading_status || "unread",
    last_opened: stored?.last_opened || "",
  };
}

/**
 * The workspace shell: sidebar + one page. It owns everything that outlives
 * a single paper (library, jobs, service health, preferences); the Reader
 * owns everything about the paper being read.
 */
export function App({
  embedded,
  bootError,
}: {
  embedded: DashboardReport | null;
  bootError?: string;
}) {
  const [prefs, setPrefs] = usePrefs();
  const hash = useHash();
  const fallbackRoute: Route = { page: "library" };
  const route = parseRoute(hash) || fallbackRoute;
  const settingsVisited = useRef(false);
  if (route.page === "settings") settingsVisited.current = true;
  const uploadVisited = useRef(false);
  if (route.page === "upload") uploadVisited.current = true;
  useEffect(() => {
    if (!parseRoute(hash)) go(fallbackRoute, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash]);

  const server = serverMode && !embedded;
  const [health, setHealth] = useState<Health | null>(null);
  const [service, setService] = useState<ServiceState>(
    server ? { kind: "connecting" } : { kind: "offline-file" },
  );
  const [library, setLibrary] = useState<{
    papers: PaperSummary[];
    source: Source | null;
    storagePath?: string;
    loading: boolean;
    error: string;
  }>({
    papers: embedded ? [summarize(embedded)] : [],
    source: null,
    loading: server,
    error: bootError || "",
  });
  const [jobs, setJobs] = useState<JobState[]>([]);
  const budgetNotices = useRef(new Set<string>());
  const [jobsSource, setJobsSource] = useState<Source | null>(null);
  const [sessionJobs, setSessionJobs] = useState<Set<string>>(new Set());
  const [staged, setStaged] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const submitting = useRef(false);
  const submittedFiles = useRef(new Map<string, string>());
  const [uploadError, setUploadError] = useState("");
  const [toast, setToast] = useState("");
  const [drawer, setDrawer] = useState(false);
  const [termsOpen, setTermsOpen] = useState(false);
  const [termSidebarSlot, setTermSidebarSlot] = useState<HTMLDivElement | null>(null);
  const closeTerms = useCallback(() => setTermsOpen(false), []);
  const openTerms = useCallback(() => {
    setDrawer(false);
    setTermsOpen(true);
  }, []);
  const [shortcuts, setShortcuts] = useState(false);
  const mobile = useMedia("(max-width: 720px)");
  // Below this width the reader (map + detail panel) needs the room, so the
  // sidebar folds to an icon rail and opens as an overlay instead.
  const narrow = useMedia("(max-width: 1180px)");
  const docked = !narrow && !prefs.sidebarCollapsed;
  const [outlineSlot, setOutlineSlot] = useState<HTMLDivElement | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const showToast = useCallback((message: string) => setToast(message), []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 3200);
    return () => clearTimeout(timer);
  }, [toast]);

  /* ---------- service health ---------- */
  useEffect(() => {
    if (!server) return;
    let alive = true;
    const tick = () =>
      getHealth().then(
        (h) => {
          if (!alive) return;
          setHealth(h);
          setService({ kind: "online", queued: h.queued_jobs, running: h.running_jobs });
        },
        () => alive && setService({ kind: "unreachable" }),
      );
    tick();
    const timer = setInterval(tick, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [server]);

  /* ---------- library ---------- */
  const refreshLibrary = useCallback(async () => {
    if (!server) return;
    setLibrary((old) => ({ ...old, loading: true, error: "" }));
    try {
      const body = await listLibrary();
      setLibrary({
        papers: body.papers,
        source: body.source,
        storagePath: body.storage_path,
        loading: false,
        error: "",
      });
    } catch (e) {
      setLibrary((old) => ({
        ...old,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      }));
    }
  }, [server]);
  useEffect(() => {
    refreshLibrary();
  }, [refreshLibrary]);

  /* ---------- jobs: fast polling while something is running ---------- */
  const activeJobs = jobs.filter((j) => ACTIVE.has(j.status)).length;
  const lastActive = useRef<Set<string>>(new Set());
  const refreshJobs = useCallback(async () => {
    if (!server) return;
    try {
      const body = await listJobs();
      setJobs(body.jobs);
      setJobsSource(body.source);
      const nowActive = new Set(
        body.jobs.filter((j) => ACTIVE.has(j.status)).map((j) => j.id),
      );
      const finished = [...lastActive.current].some((id) => !nowActive.has(id));
      lastActive.current = nowActive;
      if (finished) refreshLibrary();
    } catch {
      /* the service badge already reports connectivity */
    }
  }, [server, refreshLibrary]);
  useEffect(() => {
    if (!server) return;
    refreshJobs();
    const timer = setInterval(refreshJobs, activeJobs ? 1500 : 8000);
    return () => clearInterval(timer);
  }, [server, activeJobs, refreshJobs]);

  /* ---------- upload ---------- */
  const pick = () => fileInput.current?.click();
  const stage = (files: FileList | File[]) => {
    if (submitting.current) {
      showToast("正在提交，请稍后添加文件。");
      return;
    }
    const pdfs = [...files].filter(
      (f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name),
    );
    if (pdfs.length < files.length) setUploadError("只能处理 PDF 文件，其余文件已忽略。");
    else setUploadError("");
    const merged = mergeUploadFiles(staged, pdfs);
    if (merged.duplicates)
      showToast(`已忽略 ${merged.duplicates} 个重复文件，待处理列表中已保留。`);
    if (pdfs.length) setStaged(merged.files);
    go({ page: "upload" });
  };
  const start = async (
    profile: ProcessingProfileId = "standard",
    speedMode: ProcessingSpeedMode = "normal",
  ) => {
    if (submitting.current) return;
    submitting.current = true;
    setUploading(true);
    setUploadError("");
    let unlimitedConfirmed: boolean | null;
    try {
      unlimitedConfirmed = await confirmProcessingBudget(profile);
    } catch (e) {
      setUploadError(`未开始处理：${e instanceof Error ? e.message : String(e)}`);
      setUploading(false);
      submitting.current = false;
      return;
    }
    if (unlimitedConfirmed === null) {
      setUploading(false);
      submitting.current = false;
      return;
    }
    const failed: File[] = [];
    const currentJobs = server
      ? await listJobs()
          .then((value) => value.jobs)
          .catch(() => jobs)
      : jobs;
    const submittedNow = new Set<string>();
    for (const file of staged) {
      try {
        const identity = await pdfFingerprint(file);
        const existingJob =
          currentJobs.find(
            (job) => ACTIVE.has(job.status) && job.source_sha256 === identity,
          )?.id || submittedFiles.current.get(identity);
        if (existingJob) {
          const job = currentJobs.find((item) => item.id === existingJob);
          if (submittedNow.has(existingJob) || (job && ACTIVE.has(job.status))) {
            setSessionJobs((old) => new Set(old).add(existingJob));
            showToast(`${file.name} 已在处理队列中，请查看下方进度。`);
            continue;
          }
          if (
            !confirm(
              `《${file.name}》已有处理记录。是否按当前档位重新处理？可能产生新的模型调用。`,
            )
          )
            continue;
        } else if (library.papers.some((paper) => paper.document_sha256 === identity)) {
          if (
            !confirm(
              `《${file.name}》已有阅读结果，可在文献库打开。是否按当前档位重新处理？可能产生新的模型调用。`,
            )
          )
            continue;
        }
        const { id } = await submitJob(file, profile, unlimitedConfirmed, speedMode);
        submittedFiles.current.set(identity, id);
        submittedNow.add(id);
        setSessionJobs((old) => new Set(old).add(id));
        lastActive.current.add(id);
      } catch (e) {
        failed.push(file);
        setUploadError(`${file.name}：${e instanceof Error ? e.message : String(e)}`);
      }
    }
    setStaged(failed);
    setUploading(false);
    submitting.current = false;
    refreshJobs();
    if (
      staged.length === 1 &&
      submittedNow.size === 1 &&
      hasFeature(health, "jobs.source") &&
      parseRoute(location.hash)?.page === "upload"
    )
      go({ page: "processing", jobId: [...submittedNow][0] });
  };
  const act = async (job: JobState, action: JobAction) => {
    try {
      const pending = ["retry", "resume"].includes(action) ? job.input_budget : null;
      if (pending)
        budgetNotices.current.add(
          `${job.id}:${pending.request_id || JSON.stringify(pending)}`,
        );
      const approval = pending ? confirmInputBudget(pending, job.source_name) : undefined;
      if (pending && !approval) return;
      await jobAction(job.id, action, approval);
      if (action === "retry") setSessionJobs((old) => new Set(old).add(job.id));
      refreshJobs();
    } catch (e) {
      showToast(`操作未完成：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  useEffect(() => {
    // One notice per budget event, never one per polling tick. Dismissing it
    // leaves the saved result and the explicit recovery button available.
    const pending = jobs.find(
      (job) =>
        job.input_budget &&
        ["partial", "failed"].includes(job.status) &&
        job.actions?.includes("retry") &&
        !budgetNotices.current.has(
          `${job.id}:${job.input_budget.request_id || JSON.stringify(job.input_budget)}`,
        ),
    );
    if (pending) void act(pending, "retry");
  }, [jobs]);

  const clearHistory = async () => {
    const result = await clearJobHistory();
    const removed = new Set(result.removed_ids);
    for (const [key, id] of submittedFiles.current)
      if (removed.has(id)) submittedFiles.current.delete(key);
    setJobs((old) => old.filter((job) => !removed.has(job.id)));
    setSessionJobs((old) => new Set([...old].filter((id) => !removed.has(id))));
    showToast(
      `已清理 ${removed.size} 条任务记录，文献库与笔记保留。` +
        (result.failed ? `有 ${result.failed} 条未能清理，请重试。` : ""),
    );
    void refreshJobs();
  };

  const remove = async (paper: PaperSummary) => {
    const deletion =
      library.source === "native"
        ? `删除《${paper.title}》及其生成的解析结果？\n此操作无法撤销，原始上传 PDF 保留。`
        : `从文献库移除《${paper.title}》？本地文件仍保留。`;
    if (!confirm(deletion)) return;
    try {
      const how = await removePaper(paper.id);
      showToast(how === "deleted" ? "已删除论文与解析" : "已移出列表，本地文件仍保留");
      setLibrary((old) => ({
        ...old,
        papers: old.papers.filter((p) => p.id !== paper.id),
      }));
      if (route.page === "read" && route.paperId === paper.id) go({ page: "library" });
    } catch (e) {
      showToast(`移除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const update = async (
    paper: PaperSummary,
    values: {
      starred?: boolean;
      reading_status?: "unread" | "reading" | "read";
      opened?: boolean;
    },
  ) => {
    const next = server
      ? await updatePaper(paper.id, values)
      : {
          ...paper,
          ...values,
          ...(values.opened ? { last_opened: new Date().toISOString() } : {}),
        };
    if (embedded)
      writeLocal(`paperlens:paper-state:${embedded.document.sha256}`, {
        starred: next.starred,
        reading_status: next.reading_status,
        last_opened: next.last_opened,
      });
    setLibrary((old) => ({
      ...old,
      papers: old.papers.map((p) => (p.id === paper.id ? next : p)),
    }));
  };
  const read = (paper: PaperSummary) => {
    void update(paper, {
      opened: true,
      reading_status: paper.reading_status === "read" ? "read" : "reading",
    }).catch(() => showToast("阅读状态未保存，可继续阅读"));
    go({ page: "read", paperId: paper.id, mode: prefs.fullReadMode });
  };

  /** Sequential, so the service never races itself on shared folders. */
  const removeMany = async (list: PaperSummary[]): Promise<string[]> => {
    const gone = new Set<string>();
    const failed: string[] = [];
    let deleted = 0;
    let hidden = 0;
    let reason = "";
    for (const paper of list)
      try {
        if ((await removePaper(paper.id)) === "deleted") deleted++;
        else hidden++;
        gone.add(paper.id);
      } catch (e) {
        failed.push(paper.id);
        reason ||= e instanceof Error ? e.message : String(e);
      }
    setLibrary((old) => ({ ...old, papers: old.papers.filter((p) => !gone.has(p.id)) }));
    showToast(
      [
        deleted && `已删除 ${deleted} 篇论文与解析`,
        hidden && `${hidden} 篇已移出列表，本地文件仍保留`,
        failed.length && `${failed.length} 篇未能移除：${reason}`,
      ]
        .filter(Boolean)
        .join("；"),
    );
    return failed;
  };

  useEffect(() => {
    setDrawer(false);
    setTermsOpen(false);
  }, [hash]);
  const toggleSidebar = useCallback(() => {
    setTermsOpen(false);
    if (narrow) setDrawer((open) => !open);
    else {
      setDrawer(false);
      setPrefs({ sidebarCollapsed: !prefs.sidebarCollapsed });
    }
  }, [narrow, prefs.sidebarCollapsed, setPrefs]);
  useEffect(() => {
    setDrawer(false);
  }, [narrow]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (backgroundShortcutBlocked(e)) return;
      if (e.key === "Escape") setDrawer(false);
      else if (
        (e.ctrlKey || e.metaKey) &&
        !e.shiftKey &&
        !e.altKey &&
        e.key.toLowerCase() === "b"
      ) {
        e.preventDefault();
        toggleSidebar();
      } else if (e.key === "?" && !isTyping(e)) setShortcuts((open) => !open);
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [toggleSidebar]);

  /* ---------- chat ---------- */
  const multiChat = server && hasFeature(health, "chat.papers");
  const canChat = multiChat || (server && hasFeature(health, "chat"));
  const canAnnotate = server && hasFeature(health, "annotations");
  const remoteChats = server && hasFeature(health, "conversations");
  const libraryIds = useMemo(
    () => new Set(library.papers.map((p) => p.id)),
    [library.papers],
  );
  // Before any page effect runs, so a conversation opens from the right store.
  useLayoutEffect(() => {
    configureChat({
      multi: multiChat,
      remote: remoteChats,
      stream: Boolean(health?.features?.includes("chat.stream")),
      routing: Boolean(health?.features?.includes("chat.routing")),
      library: server ? libraryIds : null,
    });
  }, [multiChat, remoteChats, server, libraryIds, health]);
  const chatAvailable: true | string | null = canChat
    ? true
    : !server
      ? "离线阅读版不连接模型。用 paperlens serve 打开工作台后即可对话。"
      : health
        ? "当前本地服务不支持对话。请升级 PaperLens 后重启 paperlens serve。"
        : null;

  const currentTitle =
    route.page === "read"
      ? library.papers.find((p) => p.id === route.paperId)?.title || "论文"
      : route.page === "processing"
        ? jobs.find((job) => job.id === route.jobId)?.source_name || "原文阅读"
        : {
            library: "文献库",
            chat: "对话",
            terms: "术语本",
            upload: "导入论文",
            settings: "设置",
          }[route.page];
  useEffect(() => {
    document.title = `${currentTitle} · PaperLens`;
  }, [currentTitle]);

  return (
    <CurrencyProvider currency={prefs.currency} enabled={server}>
      <div
        className={`shell ${docked || mobile ? "" : "is-rail"} ${termsOpen ? "has-term-sidebar" : ""}`}
      >
        {!embedded && (
          <input
            ref={fileInput}
            type="file"
            accept=".pdf,application/pdf"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files?.length) stage(e.target.files);
              e.target.value = "";
            }}
          />
        )}
        <Sidebar
          route={route}
          papers={library.papers}
          embedded={Boolean(embedded)}
          chat={canChat}
          activeJobs={activeJobs}
          service={service}
          outlineRef={setOutlineSlot}
          open={drawer}
          rail={!docked && !mobile}
          onToggle={toggleSidebar}
          onClose={() => setDrawer(false)}
          onShortcuts={() => setShortcuts(true)}
        />
        {route.page === "read" && (route.mode === "text" || route.mode === "results") && (
          <div className="term-sidebar-slot" ref={setTermSidebarSlot} hidden={drawer} />
        )}
        <main className="main">
          <div className="topbar">
            <button
              className="icon-button"
              aria-label="打开侧栏"
              aria-expanded={drawer}
              onClick={() => {
                setTermsOpen(false);
                setDrawer(true);
              }}
            >
              <Icon name="menu" size={18} />
            </button>
            <span className="topbar-title">{currentTitle}</span>
          </div>
          {route.page === "library" && (
            <Library
              papers={library.papers}
              loading={library.loading}
              error={library.error}
              source={library.source}
              storagePath={library.storagePath}
              embedded={Boolean(embedded)}
              activeJobs={activeJobs}
              onUpload={() => go({ page: "upload" })}
              onRemove={remove}
              onRemoveMany={removeMany}
              onRetry={refreshLibrary}
              onDownload={(paper) => void saveOffline(paper, showToast)}
              onUpdate={update}
              onRead={read}
            />
          )}
          {route.page === "chat" && (
            <ChatPage
              id={route.id}
              papers={library.papers}
              available={chatAvailable}
              canHighlights={canAnnotate}
              onToast={showToast}
            />
          )}
          {route.page === "terms" && (
            <TermsPage
              papers={library.papers}
              chat={multiChat ? "papers" : canChat ? "paper" : null}
            />
          )}
          {uploadVisited.current && (
            <div hidden={route.page !== "upload"}>
              <UploadPage
                active={route.page === "upload"}
                papers={library.papers}
                staged={staged}
                jobs={jobs}
                jobsSource={jobsSource}
                sessionJobs={sessionJobs}
                busy={uploading}
                error={uploadError}
                canUpload={server}
                onPick={pick}
                onDropFiles={stage}
                onUnstage={(i) => setStaged((old) => old.filter((_, j) => j !== i))}
                onStart={start}
                onAction={act}
                onClearHistory={
                  hasFeature(health, "jobs.history") ? clearHistory : undefined
                }
                onImported={(id) => {
                  setSessionJobs((old) => new Set(old).add(id));
                  lastActive.current.add(id);
                  void refreshJobs();
                  if (
                    hasFeature(health, "jobs.source") &&
                    parseRoute(location.hash)?.page === "upload"
                  )
                    go({ page: "processing", jobId: id });
                }}
              />
            </div>
          )}
          {settingsVisited.current && (
            <div hidden={route.page !== "settings"}>
              <Settings
                active={route.page === "settings"}
                prefs={prefs}
                onPrefs={setPrefs}
                server={server}
                health={health}
                storagePath={library.storagePath}
              />
            </div>
          )}
          {route.page === "read" && (
            <ReaderRoute
              key={route.paperId}
              paperId={route.paperId}
              mode={route.mode}
              embedded={embedded}
              paper={library.papers.find((p) => p.id === route.paperId)}
              outlineSlot={outlineSlot}
              termSidebarSlot={termSidebarSlot}
              termsOpen={termsOpen}
              onOpenTerms={openTerms}
              onCloseTerms={closeTerms}
              prefs={prefs}
              onPrefs={setPrefs}
              server={server}
              canExplain={server && hasFeature(health, "explain")}
              canChat={canChat}
              canAnnotate={canAnnotate}
              canTranslate={server && hasFeature(health, "translate")}
              canNotes={server && hasFeature(health, "notes")}
              canAutoNotes={server && hasFeature(health, "auto_notes")}
              canStream={server && hasFeature(health, "chat.stream")}
              canReferenceTree={server && hasFeature(health, "references.tree")}
              papers={library.papers}
              onToast={showToast}
            />
          )}
          {route.page === "processing" && (
            <ProcessingReader
              key={route.jobId}
              jobId={route.jobId}
              job={jobs.find((job) => job.id === route.jobId)}
              server={server}
              onAction={act}
            />
          )}
        </main>
        {shortcuts && <Shortcuts onClose={() => setShortcuts(false)} />}
        <div
          className={`toast ${toast ? "is-visible" : ""}`}
          role="status"
          aria-live="polite"
        >
          {toast}
        </div>
      </div>
    </CurrencyProvider>
  );
}

function ReaderRoute({
  paperId,
  mode,
  embedded,
  paper,
  server,
  ...rest
}: {
  paperId: string;
  mode: ReaderMode;
  embedded: DashboardReport | null;
  server: boolean;
  paper?: PaperSummary;
  outlineSlot: HTMLElement | null;
  termSidebarSlot: HTMLElement | null;
  termsOpen: boolean;
  onOpenTerms: () => void;
  onCloseTerms: () => void;
  prefs: Prefs;
  onPrefs: (patch: Partial<Prefs>) => void;
  canExplain: boolean;
  canChat: boolean;
  canAnnotate: boolean;
  canTranslate: boolean;
  canNotes: boolean;
  canAutoNotes: boolean;
  canStream: boolean;
  canReferenceTree: boolean;
  papers: PaperSummary[];
  onToast: (message: string) => void;
}) {
  const local = paperId === LOCAL_ID && embedded;
  const [state, setState] = useState<
    { report: DashboardReport } | { error: string } | null
  >(local ? { report: embedded } : null);
  const source = useMemo(
    () => ({ id: paperId, data_url: paper?.data_url, report_url: paper?.report_url }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [paperId, paper?.data_url, paper?.report_url],
  );
  useLayoutEffect(() => {
    // The library can supply URLs after this paper's graph has finished rendering.
    if (!local) window.__paperlensReady = false;
  }, [paperId, local]);
  useEffect(() => {
    if (local) return;
    if (!server) {
      setState({ error: "离线报告只包含一篇论文。" });
      return;
    }
    let alive = true;
    loadReport(source).then(
      (report) => {
        if (!alive) return;
        // Keep the page self-describing, like a standalone report.
        const node = document.getElementById("paperlens-data");
        if (node) node.textContent = JSON.stringify(report);
        setState({ report });
      },
      (e) => alive && setState({ error: e instanceof Error ? e.message : String(e) }),
    );
    return () => {
      alive = false;
    };
  }, [local, server, source]);

  if (!state)
    return (
      <div className="page">
        <div className="loading-block" aria-busy="true">
          <span className="spinner" />
          正在打开论文…
        </div>
      </div>
    );
  if ("error" in state)
    return (
      <div className="page">
        <div className="empty">
          <Icon name="warning" size={22} />
          <strong>这篇论文暂时无法打开</strong>
          <p>{state.error}</p>
          <a className="button" href={href({ page: "library" })}>
            返回论文列表
          </a>
        </div>
      </div>
    );
  return (
    <Reader
      report={state.report}
      paperId={paperId}
      mode={mode}
      onSaveOffline={
        local ? undefined : () => saveOffline(paper || { id: paperId }, rest.onToast)
      }
      onLoadComplete={local ? undefined : () => loadCompleteReport(source)}
      {...rest}
    />
  );
}

/** Downloads a standalone reader for one paper, built from the page being served. */
async function saveOffline(
  paper: { id: string; data_url?: string | null; report_url?: string | null },
  onToast: (message: string) => void,
) {
  onToast("正在生成离线阅读版…");
  try {
    const [html, report] = await Promise.all([offlineHtml(paper), loadReport(paper)]);
    downloadFile(
      html,
      `${fileStem(readPaperSummary(report).title)}.html`,
      "text/html;charset=utf-8",
    );
    onToast("离线阅读版已准备好");
  } catch (e) {
    onToast(`离线阅读版未生成：${e instanceof Error ? e.message : String(e)}`);
  }
}
