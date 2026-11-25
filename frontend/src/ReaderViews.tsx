import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Icon } from "./icons";
import { QualityWarnings } from "./WarningGroups";
import { AnalysisText } from "./AnalysisText";
import { Markdown } from "./markdown";
import { groupWarnings } from "./qualityWarnings";
import { EvidenceCard } from "./EvidencePanel";
import {
  removeTerms,
  restoreTerms,
  saveTerm,
  paperKey,
  takeJump,
  termKey,
  useTerms,
  type SavedTerm,
} from "./terms";
import { segments } from "./annotations";
import { runCost, serverMode, type Annotation, type RunCost } from "./api";
import { costText, formatDuration } from "./estimate";
import { exchangeNote, useMoney } from "./currency";
import type { DashboardReport, Evidence, Formula, ReadingTranscription } from "./types";
import { sanitizeMathML, stripMathDelimiters, texToMathML } from "./tex";
import { RichText, linkify, type LinkContext } from "./readerLinks";

export const paperTypes: Record<string, string> = {
  empirical: "实证研究",
  theoretical: "理论研究",
  review: "文献综述",
  other: "学术论文",
};
export const supportLabels = {
  supported: "模型支持",
  partial: "部分支持",
  unverified: "待核查",
};

export function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim();
  if (!q) return <>{text}</>;
  const index = text.toLocaleLowerCase().indexOf(q.toLocaleLowerCase());
  return index < 0 ? (
    <>{text}</>
  ) : (
    <>
      {text.slice(0, index)}
      <mark>{text.slice(index, index + q.length)}</mark>
      {text.slice(index + q.length)}
    </>
  );
}

/* ---------- formulas: MathML is whitelisted element-by-element ---------- */

export function FormulaView({
  formula,
  fallback,
  inline = false,
}: {
  formula: Formula;
  fallback: string;
  inline?: boolean;
}) {
  const [source, setSource] = useState(false);
  const html = useMemo(
    () =>
      formula.status === "renderable" && formula.mathml
        ? sanitizeMathML(formula.mathml, inline)
        : null,
    [formula, inline],
  );
  if (inline)
    return html ? (
      <span className="transcription-math" dangerouslySetInnerHTML={{ __html: html }} />
    ) : (
      <code className="transcription-math-source">{formula.latex || fallback}</code>
    );
  return (
    <figure className="formula">
      {html && !source ? (
        <div className="formula-math" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <code className="formula-source">{formula.latex || fallback}</code>
      )}
      <figcaption>
        <span className={`status-tag ${formula.verified ? "ok" : "warn"}`}>
          {formula.verified
            ? "已核对"
            : formula.status === "renderable"
              ? "尚未核对"
              : formula.status === "invalid"
                ? "公式识别失败，显示原始文本"
                : "公式待恢复"}
        </span>
        {html && formula.latex && (
          <button className="text-button" onClick={() => setSource(!source)}>
            {source ? "显示公式" : "查看 LaTeX"}
          </button>
        )}
        {formula.latex && formula.status === "renderable" && (
          <button
            className="text-button"
            onClick={() =>
              void navigator.clipboard?.writeText(formula.latex).catch(() => {})
            }
          >
            复制 LaTeX
          </button>
        )}
        {!!formula.warnings.length && (
          <small className="transcription-warning">{formula.warnings.join("；")}</small>
        )}
      </figcaption>
    </figure>
  );
}

/**
 * A displayed equation in the reader, TeX first as EasyRead shows it. A click
 * on the equation unfolds the original page crop beneath it for comparison;
 * the copy and source tools stay quiet until the equation is hovered.
 */
export function TexFormula({
  formula,
  image,
  number = "",
  onSource,
}: {
  formula: Formula;
  /** The original crop, shown once the reader asks to compare. */
  image?: ReactNode;
  number?: string;
  onSource?: () => void;
}) {
  const [source, setSource] = useState(false);
  const latex = stripMathDelimiters(formula.latex || "");
  const html = useMemo(
    () =>
      (formula.mathml ? sanitizeMathML(formula.mathml, false) : null) ||
      texToMathML(latex, true),
    [formula, latex],
  );
  const math = (
    <span className="tex-formula-line">
      {html && !source ? (
        <span className="tex-formula-math" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <code className="formula-source">{latex}</code>
      )}
      {number && (
        <span className="equation-number" aria-label={`公式 ${number}`}>
          ({number})
        </span>
      )}
    </span>
  );
  return (
    <div className="tex-formula">
      {image ? (
        <details className="tex-formula-compare">
          <summary title="点击对照原图">{math}</summary>
          <div className="tex-formula-original">
            <small>原图</small>
            {image}
          </div>
        </details>
      ) : (
        math
      )}
      <span className="tex-formula-tools" onClick={(e) => e.stopPropagation()}>
        {html && (
          <button
            className="text-button"
            aria-pressed={source}
            onClick={() => setSource(!source)}
          >
            {source ? "显示公式" : "查看 LaTeX"}
          </button>
        )}
        <button
          className="text-button"
          onClick={() => void navigator.clipboard?.writeText(latex).catch(() => {})}
        >
          复制
        </button>
        {onSource && (
          <button className="text-button" onClick={onSource}>
            原页
          </button>
        )}
      </span>
    </div>
  );
}

/** Recognition output is readable/copyable, never an original-source selection. */
export function TranscriptionView({
  transcription,
  query = "",
  ctx,
}: {
  transcription: ReadingTranscription;
  query?: string;
  ctx?: LinkContext;
}) {
  return (
    <span className="reading-transcription">
      <span className="transcription-content">
        {transcription.segments.map((segment, index) =>
          segment.formula ? (
            <FormulaView
              key={index}
              formula={segment.formula}
              fallback={segment.text}
              inline
            />
          ) : (
            <RichText key={index} text={segment.text} query={query} ctx={ctx} />
          ),
        )}
      </span>
    </span>
  );
}

/* ---------- locating: scroll-spy + a quiet rail on the right ---------- */

const motionOk = () =>
  document.documentElement.dataset.motion !== "reduced" &&
  !matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Brings an element under the sticky toolbar and marks it briefly. */
export function jumpTo(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  let ancestor = el.parentElement;
  while (ancestor) {
    if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
    ancestor = ancestor.parentElement;
  }
  const pane = el.closest<HTMLElement>(".reader-main");
  if (pane && motionOk()) {
    // Far jumps snap most of the way, then glide the last stretch: the eye
    // still sees the direction, without a long blurry slide.
    const delta = el.getBoundingClientRect().top - pane.getBoundingClientRect().top;
    const near = pane.clientHeight * 0.8;
    if (Math.abs(delta) > near * 2) pane.scrollTop += delta - Math.sign(delta) * near;
  }
  el.scrollIntoView({ block: "start", behavior: motionOk() ? "smooth" : "auto" });
  el.classList.remove("is-flash");
  void el.offsetWidth;
  el.classList.add("is-flash");
  setTimeout(() => el.classList.remove("is-flash"), 1600);
}

/**
 * The first element (in document order) inside the reading band, a strip at
 * the top of the scrolling pane starting `offset` px down (just below any
 * sticky toolbar). When nothing is in the band the previous answer stays,
 * i.e. "the one you last passed".
 *
 * `pin` marks a jump target as current until the reader scrolls by hand, so
 * entries near the end, which can never reach the band, still light up.
 */
export function useScrollSpy(
  ids: string[],
  offset: number,
): [string | null, (id: string) => void] {
  const [active, setActive] = useState<string | null>(ids[0] ?? null);
  const pinned = useRef<string | null>(null);
  const pin = useCallback((id: string) => {
    pinned.current = id;
    setActive(id);
  }, []);
  const key = ids.join("\n");
  useEffect(() => {
    const list = key ? key.split("\n") : [];
    setActive(list[0] ?? null);
    pinned.current = null;
    const root = document
      .getElementById(list[0] || "")
      ?.closest<HTMLElement>(".reader-main");
    const elements = list.map((id) => document.getElementById(id)).filter((el) => !!el);
    let frame = 0;
    const settle = () => {
      frame = 0;
      if (pinned.current) return;
      const line = (root?.getBoundingClientRect().top || 0) + offset;
      // A long listing can occupy several screens with no heading in view.
      // Its section is the last heading passed, not the last one observed.
      let found = elements[0]?.id ?? null;
      for (const el of elements) {
        if (el.getBoundingClientRect().top > line) break;
        found = el.id;
      }
      setActive(found);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(settle);
    };
    const release = () => {
      pinned.current = null;
      schedule();
    };
    const events = ["wheel", "touchmove", "pointerdown", "keydown"] as const;
    for (const type of events) root?.addEventListener(type, release, { passive: true });
    const scroller = root || window;
    scroller.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    const resize = new ResizeObserver(schedule);
    if (elements[0]?.parentElement) resize.observe(elements[0].parentElement);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      resize.disconnect();
      scroller.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      for (const type of events) root?.removeEventListener(type, release);
    };
  }, [key, offset]);
  return [active, pin];
}

interface RailItem {
  id: string;
  title: string;
  level?: number;
  meta?: string;
}
function LocatorRail({
  label,
  head,
  items,
  active,
  onJump,
}: {
  label: string;
  head: ReactNode;
  items: RailItem[];
  active: string | null;
  onJump: (id: string) => void;
}) {
  const list = useRef<HTMLOListElement>(null);
  const [marker, setMarker] = useState<{ top: number; height: number } | null>(null);
  useLayoutEffect(() => {
    const box = list.current;
    const el = box?.querySelector<HTMLElement>("[aria-current='true']");
    if (!box || !el) return setMarker(null);
    setMarker({ top: el.offsetTop, height: el.offsetHeight });
    // Keep the current entry in sight without scrolling the page itself.
    if (
      el.offsetTop < box.scrollTop ||
      el.offsetTop + el.offsetHeight > box.scrollTop + box.clientHeight
    )
      box.scrollTo({
        top: el.offsetTop - box.clientHeight / 3,
        behavior: motionOk() ? "smooth" : "auto",
      });
  }, [active, items]);
  return (
    <nav className="locator" aria-label={label}>
      <div className="locator-head">{head}</div>
      <ol ref={list}>
        {marker && (
          <span
            className="locator-marker"
            aria-hidden="true"
            style={{ transform: `translateY(${marker.top}px)`, height: marker.height }}
          />
        )}
        {items.map((item) => (
          <li key={item.id}>
            <button
              aria-current={active === item.id}
              className={`level-${item.level || 0}`}
              title={item.title}
              onClick={() => onJump(item.id)}
            >
              <span>{item.title}</span>
              {item.meta && <small>{item.meta}</small>}
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

/** Block text with 勾画 painted in; the text itself is unchanged. */
export function Marked({
  text,
  query,
  marks,
  onMark,
  ctx,
}: {
  text: string;
  query: string;
  marks: Annotation[];
  onMark?: (annotation: Annotation, el: HTMLElement) => void;
  ctx?: LinkContext;
}) {
  const ranges = marks.map((a) => ({
    id: a.id,
    start: a.selection.start,
    end: a.selection.end,
  }));
  return (
    <>
      {segments(text, query, ranges).map((piece, i) => {
        const text = ctx ? linkify(piece.text, ctx) : piece.text;
        const inner = piece.hit ? <mark>{text}</mark> : text;
        const a = piece.mark ? marks.find((m) => m.id === piece.mark) : undefined;
        if (!a) return <span key={i}>{inner}</span>;
        return (
          <mark
            key={i}
            className={`annotation ${a.note ? "has-note" : ""}`}
            data-annotation={a.id}
            data-color={a.color}
            data-style={a.style}
            role={onMark ? "button" : undefined}
            tabIndex={onMark ? 0 : undefined}
            aria-label={a.note ? `勾画笔记：${a.note}` : "查看勾画"}
            title={a.note || "勾画 · 点击查看"}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onMark?.(a, e.currentTarget);
              }
            }}
            onClick={(e) => {
              // A click that ends a new selection is not a click on the mark.
              if (getSelection()?.toString()) return;
              onMark?.(a, e.currentTarget);
            }}
          >
            {inner}
          </mark>
        );
      })}
    </>
  );
}

/* ---------- glossary ---------- */

export function GlossaryView({
  report,
  paperId,
}: {
  report: DashboardReport;
  paperId: string;
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [termError, setTermError] = useState("");
  const terms = report.analysis.glossary;
  const owner = paperKey(paperId, report.analysis.title);
  const library = useTerms();
  const kept = useMemo(() => new Map(library.map((t) => [t.id, t])), [library]);
  const [removed, setRemoved] = useState<SavedTerm | null>(null);
  useEffect(() => {
    if (!removed) return;
    const timer = setTimeout(() => setRemoved(null), 6000);
    return () => clearTimeout(timer);
  }, [removed]);
  useEffect(() => {
    // Arrived from “术语本”.
    const name = takeJump("glossary");
    const index = name ? terms.findIndex((t) => t.term === name) : -1;
    if (index >= 0) requestAnimationFrame(() => jumpTo(`term-${index}`));
  }, [terms]);
  const items = useMemo(
    () => terms.map((t, i) => ({ id: `term-${i}`, title: t.term })),
    [terms],
  );
  const [active, pin] = useScrollSpy(
    useMemo(() => items.map((i) => i.id), [items]),
    24,
  );
  return (
    <section className="reading-view has-rail">
      <div className="reading-col term-list">
        {termError && <p role="alert">{termError}</p>}
        {terms.map((term, index) => {
          const evidence = term.evidence_ids
            .map((id) => report.evidence.find((e) => e.id === id))
            .filter((e): e is Evidence => Boolean(e));
          const expanded = open.has(index);
          const key = termKey(owner, term.term);
          const mine = kept.get(key);
          const saved = Boolean(mine);
          return (
            <article className="term" id={`term-${index}`} key={`${term.term}-${index}`}>
              <div className="term-head">
                <h3>
                  <AnalysisText text={term.term} />
                </h3>
                {term.support && (
                  <span className={`status-tag ${term.support}`}>
                    {supportLabels[term.support]}
                  </span>
                )}
                <button
                  className={`term-save ${saved ? "is-saved" : ""}`}
                  aria-pressed={saved}
                  title={saved ? "从术语本移除" : "收藏术语"}
                  onClick={() => {
                    try {
                      setTermError("");
                      if (mine) {
                        removeTerms([key]);
                        setRemoved(mine);
                      } else
                        saveTerm({
                          term: term.term,
                          explanation: term.explanation,
                          paperId: owner,
                          paperTitle: report.analysis.title,
                          origin: "glossary",
                          quote: evidence[0]?.quote,
                          page: evidence[0]?.page,
                          blockId: evidence[0]?.block_id,
                        });
                    } catch (e) {
                      setTermError(e instanceof Error ? e.message : String(e));
                    }
                  }}
                >
                  <Icon name="star" size={14} />
                  <span>{saved ? "已收藏术语" : "收藏术语"}</span>
                </button>
              </div>
              <p>
                <AnalysisText text={term.explanation} />
              </p>
              {mine?.note && (
                <div className="saved-term-note glossary-note">
                  <span className="saved-term-note-label">我的笔记</span>
                  <Markdown text={mine.note} />
                </div>
              )}
              {!!term.review_notes?.length && (
                <ul className="review-notes">
                  {term.review_notes.map((note, i) => (
                    <li key={i}>
                      <AnalysisText text={note} />
                    </li>
                  ))}
                </ul>
              )}
              {evidence.length > 0 && (
                <button
                  className="text-button"
                  aria-expanded={expanded}
                  onClick={() =>
                    setOpen((old) => {
                      const next = new Set(old);
                      if (!next.delete(index)) next.add(index);
                      return next;
                    })
                  }
                >
                  <Icon name="quote" size={13} />
                  {expanded ? "收起原文" : `原文出处 ${evidence.length}`}
                </button>
              )}
              {expanded &&
                evidence.map((e) => (
                  <EvidenceCard key={e.id} evidence={e} report={report} />
                ))}
            </article>
          );
        })}
        {!terms.length && <p className="muted">本次分析未生成术语条目。</p>}
      </div>
      {terms.length > 1 && (
        <LocatorRail
          label="术语定位"
          head={
            <>
              术语 <small>{terms.length}</small>
            </>
          }
          items={items}
          active={active}
          onJump={(id) => {
            pin(id);
            jumpTo(id);
          }}
        />
      )}
      {removed && (
        <div className="undo-bar" role="status">
          <span>
            已从术语本移除“{removed.term}”{removed.note ? "，连同笔记" : ""}
          </span>
          <button
            className="text-button"
            onClick={() => {
              restoreTerms([removed]);
              setRemoved(null);
            }}
          >
            撤销
          </button>
        </div>
      )}
    </section>
  );
}

/* ---------- processing & verification ---------- */

export function QualityView({
  report,
  warnings,
}: {
  report: DashboardReport;
  warnings: string[];
  paperId?: string;
  onNavigate?: (nodeId: string) => void;
}) {
  const q = report.quality;
  const usage = report.run.usage;
  const cost = useRunCost(report);
  const timings = Object.values(report.run.timings || {}).filter(
    (v): v is number => typeof v === "number",
  );
  const seconds = report.run.timings?.total ?? timings.reduce((a, b) => a + b, 0);
  const rows: [string, ReactNode][] = [
    ["分析模型", report.run.model],
    ...(report.run.execution_mode === "codex_session"
      ? ([["处理方式", "Codex 会话直接处理 · 未调用外部模型 API"]] as [
          string,
          ReactNode,
        ][])
      : []),
    ["读取工具", report.run.parser],
    [
      "代码排版",
      report.run.processing_limits?.code_blocks ? "已开启 · 保留换行与缩进" : "未开启",
    ],
    ["生成时间", new Date(report.run.created_at).toLocaleString("zh-CN")],
    ["调用次数", `${usage.requests} 次`],
    [
      "文字用量",
      report.run.execution_mode === "codex_session"
        ? "本地接口用量不包含 Codex 会话用量"
        : `${usage.prompt_tokens.toLocaleString()} / ${usage.completion_tokens.toLocaleString()} tokens`,
    ],
    ...(usage.prompt_cache_hit_tokens != null
      ? ([
          [
            "缓存用量",
            `${usage.prompt_cache_hit_tokens.toLocaleString()} / ${(usage.prompt_cache_miss_tokens || 0).toLocaleString()} tokens`,
          ],
        ] as [string, ReactNode][])
      : []),
    ...(seconds > 0
      ? ([["处理用时", formatDuration(seconds)]] as [string, ReactNode][])
      : []),
    ...(cost
      ? ([["参考花费", <RunCostValue cost={cost} />]] as [string, ReactNode][])
      : []),
    ["处理版本", report.run.prompt_version],
    ["报告版本", report.schema_version],
  ];
  return (
    <section className="reading-view">
      <div className="stat-row">
        <Stat value={q.parsed_pages} total={q.total_pages} label="已读页数" />
        <Stat value={q.grounded_nodes} total={q.total_nodes} label="有据要点" />
        <Stat value={report.evidence.length} label="原文摘录" />
        <Stat value={q.text_chars.toLocaleString()} label="文字数量" />
      </div>
      <div className="two-col">
        <section className="panel-box">
          <h3>
            <Icon name="check" size={16} />
            检查记录
          </h3>
          <ul className="plain-list">
            {q.checks.length ? (
              q.checks.map((c, i) => <li key={i}>{c}</li>)
            ) : (
              <li>报告未提供额外检查记录。</li>
            )}
          </ul>
        </section>
        <section className="panel-box">
          <h3>
            <Icon name="info" size={16} />
            需要留意{" "}
            {warnings.length > 0 && (
              <span className="count-pill warn">{groupWarnings(warnings).length} 类</span>
            )}
          </h3>
          <QualityWarnings warnings={warnings} failures={report.run.failures} />
        </section>
      </div>
      <section className="panel-box">
        <h3>处理记录</h3>
        <dl className="kv">
          {rows.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      </section>
    </section>
  );
}

/** Price the recorded usage on a live service; offline pages show nothing. */
function useRunCost(report: DashboardReport): RunCost | null {
  const [cost, setCost] = useState<RunCost | null>(null);
  const run = report.run;
  useEffect(() => {
    setCost(null);
    if (!serverMode || run.execution_mode === "codex_session" || !run.usage.requests)
      return;
    let active = true;
    runCost(run)
      .then((value) => active && value.status !== "none" && setCost(value))
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [run]);
  return cost;
}

function RunCostValue({ cost }: { cost: RunCost }) {
  const money = useMoney();
  const unpriced = cost.models.filter((m) => m.status === "unknown").map((m) => m.model);
  const checked = cost.price_checked_at?.slice(0, 10);
  if (cost.status === "unknown")
    return (
      <span className="run-cost">
        价格未收录
        <small>{unpriced.join("、")} 不在参考价格目录中，实际以服务商账单为准。</small>
      </span>
    );
  return (
    <span className="run-cost">
      {cost.status === "partial" && "已知部分 "}
      {costText({ status: "range", off_peak: cost.off_peak, peak: cost.peak }, money)}
      {exchangeNote(money) && <small>{exchangeNote(money)}</small>}
      <small>
        {`按 ${checked}${cost.price_stale ? "（已过期）" : ""} 参考价折算，非账单`}
        {unpriced.length > 0 && `；${unpriced.join("、")} 价格未收录，未计入`}
      </small>
    </span>
  );
}

function Stat({
  value,
  total,
  label,
}: {
  value: ReactNode;
  total?: number;
  label: string;
}) {
  return (
    <div className="stat">
      <strong>
        {value}
        {total !== undefined && <small> / {total}</small>}
      </strong>
      <span>{label}</span>
    </div>
  );
}
