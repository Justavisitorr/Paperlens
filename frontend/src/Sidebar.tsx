import { Icon, type IconName } from "./icons";
import { href, type Route } from "./router";
import type { PaperSummary } from "./api";
import { useTerms } from "./terms";
import { useEffect, useState } from "react";

export type ServiceState =
  | { kind: "offline-file" }
  | { kind: "connecting" }
  | { kind: "online"; queued: number; running: number }
  | { kind: "unreachable" };

export function Sidebar({
  route,
  papers,
  embedded,
  chat = false,
  activeJobs,
  service,
  outlineRef,
  open,
  rail,
  onToggle,
  onClose,
  onShortcuts,
}: {
  route: Route;
  papers: PaperSummary[];
  embedded: boolean;
  /** The local service can answer questions across papers. */
  chat?: boolean;
  activeJobs: number;
  service: ServiceState;
  outlineRef: (el: HTMLDivElement | null) => void;
  open: boolean;
  /** Folded to icons; `open` then shows the full sidebar as an overlay. */
  rail: boolean;
  onToggle: () => void;
  onClose: () => void;
  onShortcuts: () => void;
}) {
  const activePaper = route.page === "read" ? route.paperId : null;
  const terms = useTerms().length;
  const items: [Route["page"], IconName, string, React.ReactNode?][] = [
    ["library", "library", "文献库", papers.length || undefined],
    ...(chat && !embedded
      ? ([["chat", "chat", "对话"]] as [Route["page"], IconName, string][])
      : []),
    ["terms", "bookmark", "术语本", terms || undefined],
    ...(embedded
      ? []
      : ([
          [
            "upload",
            "upload",
            "导入论文",
            activeJobs ? <span className="badge live">{activeJobs}</span> : undefined,
          ],
        ] as [Route["page"], IconName, string, React.ReactNode?][])),
    ["settings", "gear", "设置"],
  ];
  const isActive = (page: Route["page"]) =>
    route.page === page ||
    (page === "library" && route.page === "read") ||
    (page === "upload" && route.page === "processing");
  return (
    <>
      <div
        className={`scrim ${open ? "is-open" : ""}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className={`sidebar ${open ? "is-open" : ""} ${rail ? "is-rail" : ""}`}
        aria-label="工作区"
      >
        <div className="sidebar-head">
          <a className="brand" href={href({ page: "library" })} onClick={onClose}>
            <span className="brand-mark">
              <Icon name="lens" size={17} />
            </span>
            PaperLens
          </a>
          <button
            className="icon-button small sidebar-toggle"
            title={`${rail && !open ? "展开" : "收起"}侧栏（Ctrl B）`}
            aria-label={rail && !open ? "展开侧栏" : "收起侧栏"}
            aria-expanded={!rail || open}
            onClick={onToggle}
          >
            <Icon name="panel" size={16} />
          </button>
        </div>
        <nav className="side-nav" aria-label="主导航">
          {items.map(([page, icon, label, extra]) => (
            <a
              key={page}
              href={href({ page } as Route)}
              className={isActive(page) ? "is-active" : ""}
              aria-current={isActive(page) ? "page" : undefined}
              title={rail ? label : undefined}
              onClick={onClose}
            >
              <Icon name={icon} size={16} />
              <span>{label}</span>
              {typeof extra === "number" ? <small>{extra}</small> : extra}
            </a>
          ))}
        </nav>
        {!embedded && window.paperLensWorkspaceReady && (
          <nav className="side-nav" aria-label="账户">
            <a href={window.paperLensCloudReady ? "/account" : "/_paperlens/migration"} title="账户与资料">
              <Icon name="book" size={16} /><span>{window.paperLensCloudReady ? "账户与资料" : "迁移资料"}</span>
            </a>
          </nav>
        )}
        <div className="side-section">
          <div className="side-label">{embedded ? "当前论文" : "我的论文"}</div>
          <div className="paper-list">
            {papers.map((paper) => {
              const active = paper.id === activePaper;
              return (
                <div key={paper.id} className={`paper-item ${active ? "is-active" : ""}`}>
                  <a
                    className="paper-link"
                    href={href({ page: "read", paperId: paper.id, mode: "overview" })}
                    aria-current={active ? "page" : undefined}
                    title={paper.title}
                  >
                    <Icon name="file" size={15} />
                    <span>{paper.title}</span>
                    {paper.status === "partial" && (
                      <i className="dot warn" title="部分完成" />
                    )}
                  </a>
                  {active && (
                    // Row clicks reach here (twisties stop propagation): picking a
                    // node closes the overlay so the map can show it.
                    <div className="outline-slot" ref={outlineRef} onClick={onClose} />
                  )}
                </div>
              );
            })}
            {!papers.length && (
              <p className="side-empty">
                {embedded ? "报告未包含论文。" : "还没有论文。上传一篇 PDF 开始阅读。"}
              </p>
            )}
          </div>
        </div>
        <footer className="sidebar-foot">
          <ServiceBadge service={service} />
          <button
            className="icon-button small"
            title="键盘快捷键（?）"
            aria-label="键盘快捷键"
            onClick={onShortcuts}
          >
            <Icon name="keyboard" size={15} />
          </button>
        </footer>
      </aside>
    </>
  );
}

function ServiceBadge({ service }: { service: ServiceState }) {
  const cloud = Boolean(window.paperLensCloudReady);
  const [sync, setSync] = useState(window.paperLensSync?.snapshot().status || "");
  useEffect(() => {
    const update = () => setSync(window.paperLensSync?.snapshot().status || "");
    window.addEventListener("paperlens:sync", update);
    return () => window.removeEventListener("paperlens:sync", update);
  }, []);
  const [tone, title, detail] =
    service.kind === "offline-file"
      ? ["ok", "离线报告", "阅读与导出无需网络"]
      : service.kind === "connecting"
        ? ["idle", cloud ? "正在连接研究服务" : "正在连接本地服务", ""]
        : service.kind === "unreachable"
          ? ["bad", cloud ? "研究服务未连接" : "本地服务未连接", "已打开的论文仍可阅读"]
          : [
              "ok",
              cloud ? "研究服务已连接" : "本地服务已连接",
              service.running || service.queued
                ? `处理中 ${service.running} · 排队 ${service.queued}`
                : cloud ? sync : "论文与解析保存在本地",
            ];
  return (
    <div
      className="service"
      role="status"
      title={detail ? `${title} · ${detail}` : title}
    >
      <i className={`dot ${tone}`} />
      <span>
        {title}
        {detail && <small>{detail}</small>}
      </span>
    </div>
  );
}
