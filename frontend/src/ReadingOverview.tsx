import { useEffect, useId, useRef, useState } from "react";
import type { DashboardReport } from "./types";
import { AnalysisText } from "./AnalysisText";
import "./styles/32-reading-review.css";

function OverviewCards({
  title,
  nodes,
  onNavigate,
}: {
  title: string;
  nodes: DashboardReport["analysis"]["nodes"];
  onNavigate: (id: string) => void;
}) {
  const [index, setIndex] = useState(0);
  const id = useId();
  const tabs = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const strip = tabs.current;
    const tab = strip?.querySelector<HTMLElement>('[aria-selected="true"]');
    if (!strip || !tab) return;
    const bounds = strip.getBoundingClientRect();
    const selected = tab.getBoundingClientRect();
    // Reveal only along this strip; do not move the reader's vertical position.
    if (selected.left < bounds.left) strip.scrollLeft += selected.left - bounds.left;
    else if (selected.right > bounds.right)
      strip.scrollLeft += selected.right - bounds.right;
  }, [index]);
  const current = nodes[index];
  const select = (next: number) =>
    setIndex(Math.max(0, Math.min(nodes.length - 1, next)));
  return (
    <section className="overview-card-section" aria-label={title}>
      <h3>{title}</h3>
      {!current ? (
        <p>本报告未提取这类要点，可继续查看原文。</p>
      ) : (
        <>
          <div className="overview-card-navigation">
            <div
              ref={tabs}
              className="overview-card-tabs"
              role="tablist"
              aria-label={title}
              onKeyDown={(event) => {
                const next =
                  event.key === "ArrowLeft"
                    ? index - 1
                    : event.key === "ArrowRight"
                      ? index + 1
                      : event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? nodes.length - 1
                          : null;
                if (next === null) return;
                event.preventDefault();
                event.stopPropagation();
                const target = Math.max(0, Math.min(nodes.length - 1, next));
                select(target);
                document.getElementById(`${id}-tab-${target}`)?.focus();
              }}
            >
              {nodes.map((node, i) => (
                <button
                  key={node.id}
                  role="tab"
                  id={`${id}-tab-${i}`}
                  aria-label={`${title} ${i + 1}：${node.title}`}
                  title={node.title}
                  aria-selected={index === i}
                  aria-controls={`${id}-panel`}
                  tabIndex={index === i ? 0 : -1}
                  onClick={() => select(i)}
                >
                  {i + 1}
                </button>
              ))}
            </div>
            <span className="overview-card-count" aria-live="polite">
              {index + 1} / {nodes.length}
            </span>
          </div>
          <div className="overview-card-stage">
            {nodes[index - 1] ? (
              <button
                className="overview-card-preview previous"
                aria-label={`上一张：${nodes[index - 1].title}`}
                onClick={() => select(index - 1)}
              >
                <span aria-hidden="true">
                  <AnalysisText text={nodes[index - 1].title} />
                </span>
              </button>
            ) : (
              <span />
            )}
            <article
              className="overview-current-card"
              role="tabpanel"
              id={`${id}-panel`}
              aria-labelledby={`${id}-title`}
              tabIndex={0}
            >
              <h4 id={`${id}-title`}>
                <AnalysisText text={current.title} />
              </h4>
              <p>
                <AnalysisText text={current.summary} />
              </p>
              <button
                className="text-button"
                data-node-id={current.id}
                onClick={() => onNavigate(current.id)}
              >
                查看论点与证据 →
              </button>
            </article>
            {nodes[index + 1] ? (
              <button
                className="overview-card-preview next"
                aria-label={`下一张：${nodes[index + 1].title}`}
                onClick={() => select(index + 1)}
              >
                <span aria-hidden="true">
                  <AnalysisText text={nodes[index + 1].title} />
                </span>
              </button>
            ) : (
              <span />
            )}
          </div>
        </>
      )}
    </section>
  );
}

export function ReadingOverview({
  report,
  onMap,
  onText,
  onNavigate,
}: {
  report: DashboardReport;
  onMap: () => void;
  onText: () => void;
  onNavigate: (id: string) => void;
}) {
  const { analysis } = report;
  return (
    <section className="reading-view reading-overview" aria-label="阅读总览">
      <header>
        <div className="eyebrow">阅读总览</div>
        <h2>
          <AnalysisText text={analysis.title || report.document.title} />
        </h2>
        <p className="review-status-note">
          从研究问题、贡献和局限开始，再沿论证与原文核对。
        </p>
      </header>
      <section>
        <h3>一句话总结</h3>
        <p className="lede">
          <AnalysisText text={analysis.summary || "本报告未提供总结。"} />
        </p>
      </section>
      <section>
        <h3>研究问题</h3>
        <p>
          <AnalysisText text={analysis.research_question || "本报告未提供研究问题。"} />
        </p>
      </section>
      <div className="overview-actions">
        <button className="button primary" onClick={onMap}>
          进入论证图
        </button>
        <button className="button" onClick={onText}>
          开始阅读正文
        </button>
      </div>
      <OverviewCards
        title="主要贡献"
        nodes={analysis.nodes.filter((n) => n.kind === "contribution")}
        onNavigate={onNavigate}
      />
      <OverviewCards
        title="局限与适用边界"
        nodes={analysis.nodes.filter((n) => n.kind === "limitation")}
        onNavigate={onNavigate}
      />
      {!!analysis.reading_guide.length && (
        <section>
          <h3>阅读指南</h3>
          <ol className="steps">
            {analysis.reading_guide.map((step, i) => (
              <li key={i}>
                <AnalysisText text={step} />
              </li>
            ))}
          </ol>
        </section>
      )}
    </section>
  );
}
