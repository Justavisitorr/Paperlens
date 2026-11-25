import { AnalysisText } from "./AnalysisText";
import { useMemo, useRef, useState, type ReactNode } from "react";
import { useFocusTrap } from "./dialog";
import { Icon } from "./icons";
import { kindLabels } from "./graph";
import { ResultConditions } from "./ResultConditions";
import { resultNodeIds } from "./experimentConditions";
import { sanitizeMathML } from "./tex";
import { AssetImage } from "./AssetImage";
import { evidencePageLabel, evidencePages } from "./evidenceSource";
import { PdfSource } from "./PdfSource";
import { unverifiedModelSource } from "./pdfSourceNavigation";
import type { DashboardReport, Evidence, Formula, PaperNode } from "./types";

const originLabels = {
  author: "作者论述",
  inference: "AI推断",
  reader_guide: "阅读辅助",
};
const matchLabels = {
  exact: "文字一致",
  normalized: "排版不同",
  approximate: "近似摘录",
  block: "原文段落",
};
const supportLabels = {
  supported: "模型支持",
  partial: "部分支持",
  unverified: "待核查",
};

function MathFragment({ formula, inline }: { formula: Formula; inline: boolean }) {
  const html = useMemo(
    () => (formula.mathml ? sanitizeMathML(formula.mathml, inline) : null),
    [formula, inline],
  );
  return html ? (
    <span
      className={inline ? "transcription-math" : "evidence-math"}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  ) : (
    <code>{formula.latex}</code>
  );
}

/**
 * The words of a piece of evidence as a reader should see them. A text layer
 * that lost its mathematics reads as noise ("t i = argmin i 1 2 …"), so a
 * recognized equation or a paragraph's recognized transcription is shown
 * instead, and the literal match stays one click away.
 */
function EvidenceQuote({
  evidence,
  report,
}: {
  evidence: Evidence;
  report: DashboardReport;
}) {
  const block = report.document.blocks.find((b) => b.id === evidence.block_id);
  const formula =
    block?.kind === "formula" &&
    block.formula?.status === "renderable" &&
    block.formula.mathml
      ? block.formula
      : null;
  const transcribed = formula
    ? null
    : report.reading?.items.find(
        (i) =>
          i.transcription?.segments.some((s) => s.formula) &&
          i.parts.some((p) => p.block_id === evidence.block_id),
      )?.transcription;
  if (!formula && !transcribed) return <blockquote>{evidence.quote}</blockquote>;
  return (
    <>
      <blockquote className={formula ? "evidence-formula" : undefined}>
        {formula ? (
          <MathFragment formula={formula} inline={false} />
        ) : (
          transcribed!.segments.map((s, i) =>
            s.formula ? (
              <MathFragment key={i} formula={s.formula} inline />
            ) : (
              <span key={i}>{s.text}</span>
            ),
          )
        )}
      </blockquote>
      <details className="evidence-raw">
        <summary>{formula ? "公式识别" : "公式转录"} · 查看摘录</summary>
        <p>{evidence.quote}</p>
      </details>
    </>
  );
}

export function EvidenceCard({
  evidence,
  report,
  expanded = false,
}: {
  evidence: Evidence;
  report: DashboardReport;
  expanded?: boolean;
}) {
  const [open, setOpen] = useState(expanded);
  const [large, setLarge] = useState(false);
  const [selectedPage, setSelectedPage] = useState(evidence.page);
  const pages = evidencePages(evidence);
  const preview = report.previews.find((p) => p.page === selectedPage);
  const preciseRegion =
    pages.length === 1 && !unverifiedModelSource(report.document.parser);
  const bbox = evidence.bbox;
  return (
    <article className="evidence-card" id={`evidence-${evidence.id}`}>
      <div className="evidence-heading">
        <span className="page-badge">
          {pages.length === 1 ? `P${evidence.page}` : evidencePageLabel(evidence)}
        </span>
        <span>
          {evidence.section || "原文证据"}
          {evidence.page_label && evidence.page_label !== String(evidence.page)
            ? ` · 印刷页 ${evidence.page_label}`
            : ""}
        </span>
        <Icon name="quote" size={15} />
      </div>
      <EvidenceQuote evidence={evidence} report={report} />
      {pages.length > 1 && (
        <p className="tiny-note">
          来源段落跨越这些页面，摘录的具体页内位置尚未单独核实。
        </p>
      )}
      <div className="evidence-footer">
        <span title="匹配方式只说明摘录与原文的关系，不代表论点已经人工验证。">
          <Icon name={evidence.match === "approximate" ? "info" : "check"} size={13} />
          {matchLabels[evidence.match]}
        </span>
        <button
          className="text-button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
        >
          {open ? "收起原页" : "定位原页"}
          <Icon
            name="chevron"
            size={13}
            style={{ transform: open ? "rotate(90deg)" : undefined }}
          />
        </button>
      </div>
      {open && pages.length > 1 && (
        <div className="evidence-footer" aria-label="证据来源页">
          {pages.map((page) => (
            <button
              key={page}
              className="text-button"
              aria-pressed={selectedPage === page}
              onClick={() => setSelectedPage(page)}
            >
              查看 PDF 第 {page} 页
            </button>
          ))}
        </div>
      )}
      {open &&
        ((pages.length > 1 || !preview) && report.source_pdf ? (
          <PdfSource report={report} page={selectedPage} onNavigate={setSelectedPage} />
        ) : preview ? (
          <>
            <button
              className="page-preview"
              onClick={() => setLarge(true)}
              title={`放大 PDF 第 ${selectedPage} 页`}
            >
              <AssetImage
                src={preview.data_url}
                retryable={false}
                alt={`PDF 第 ${selectedPage} 页${preciseRegion ? "，黄色区域为引用段落" : ""}`}
                width={preview.width}
                height={preview.height}
              />
              {preciseRegion && (
                <span
                  className="evidence-highlight"
                  style={{
                    left: `${bbox.x0 * 100}%`,
                    top: `${bbox.y0 * 100}%`,
                    width: `${(bbox.x1 - bbox.x0) * 100}%`,
                    height: `${(bbox.y1 - bbox.y0) * 100}%`,
                  }}
                />
              )}
              <span className="page-zoom-label">
                <Icon name="expand" size={13} />
                点击放大
              </span>
            </button>
            <p className="tiny-note">
              {preciseRegion ? "位置高亮为原文段落范围；" : "请对照原页核查；"}页码按 PDF
              物理页计。
            </p>
          </>
        ) : (
          <div className="preview-unavailable">
            此页未嵌入影像，请在原始 PDF 第 {selectedPage} 页核对。上方摘录仍可离线阅读。
          </div>
        ))}
      {large && preview && (
        <PageModal page={selectedPage} onClose={() => setLarge(false)}>
          <header>
            <strong>原文定位 · PDF 第 {selectedPage} 页</strong>
            <button aria-label="关闭原页" onClick={() => setLarge(false)}>
              <Icon name="close" />
            </button>
          </header>
          <div className="page-modal-scroll">
            <div className="page-preview large-preview">
              <AssetImage
                loading="eager"
                src={preview.data_url}
                alt={`PDF 第 ${selectedPage} 页`}
                width={preview.width}
                height={preview.height}
              />
              {preciseRegion && (
                <span
                  className="evidence-highlight"
                  style={{
                    left: `${bbox.x0 * 100}%`,
                    top: `${bbox.y0 * 100}%`,
                    width: `${(bbox.x1 - bbox.x0) * 100}%`,
                    height: `${(bbox.y1 - bbox.y0) * 100}%`,
                  }}
                />
              )}
            </div>
          </div>
        </PageModal>
      )}
    </article>
  );
}

function PageModal({
  page,
  onClose,
  children,
}: {
  page: number;
  onClose: () => void;
  children: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  useFocusTrap(box);
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="page-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`PDF 第 ${page} 页`}
        tabIndex={-1}
        ref={box}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onClose();
          }
        }}
      >
        {children}
      </div>
    </div>
  );
}

export function NodeDetails({
  node,
  report,
  onCollapse,
  isCollapsed,
  hasChildren,
  onNavigate,
}: {
  node: PaperNode;
  report: DashboardReport;
  onCollapse: () => void;
  isCollapsed: boolean;
  hasChildren: boolean;
  onNavigate: (id: string) => void;
}) {
  const evidence = node.evidence_ids
    .map((id) => report.evidence.find((e) => e.id === id))
    .filter((e): e is Evidence => Boolean(e));
  const related = report.analysis.edges.filter(
    (e) => e.source === node.id || e.target === node.id,
  );
  const results = (report.quantitative_results || []).filter((result) =>
    resultNodeIds(report, result).includes(node.id),
  );
  return (
    <div className="node-details" key={node.id}>
      <div className="detail-type">
        <span className={`kind-dot kind-${node.kind}`} />
        {kindLabels[node.kind]}
        <span className={`origin-tag ${node.origin}`}>{originLabels[node.origin]}</span>
      </div>
      <h2>
        <AnalysisText text={node.title} />
      </h2>
      <div
        className={`support-tag ${node.support}`}
        title="支持状态来自模型对原文的核查，尚不等于人工证实。"
      >
        <Icon name={node.support === "supported" ? "check" : "info"} size={13} />
        {supportLabels[node.support]}
      </div>
      <p className="node-summary">
        <AnalysisText text={node.summary} />
      </p>
      {node.explanation && (
        <section className="plain-explanation">
          <div>
            <Icon name="book" size={16} />
            <strong>换个容易理解的说法</strong>
          </div>
          <p>
            <AnalysisText text={node.explanation} />
          </p>
        </section>
      )}
      {node.origin === "inference" && (
        <p className="inference-note">
          此项为基于文内信息的推断，应结合下方证据判断，不代表作者明确主张。
        </p>
      )}
      {hasChildren && (
        <button className="node-collapse-button" onClick={onCollapse}>
          <Icon name={isCollapsed ? "expand" : "collapse"} size={15} />
          {isCollapsed ? "展开下级节点" : "折叠下级节点"}
        </button>
      )}
      {!!results.length && (
        <section className="node-experiment-conditions">
          <h3>关联实验结果与条件</h3>
          {results.map((result) => (
            <div key={result.id}>
              <strong>
                {result.metric} · {result.value_text || (result.value ?? "未获得数值")}{" "}
                {result.unit}
              </strong>
              <ResultConditions result={result} report={report} />
            </div>
          ))}
        </section>
      )}
      <div className="section-title">
        <h3>原文依据</h3>
        <span>{evidence.length} 条</span>
      </div>
      {evidence.length ? (
        evidence.map((e) => <EvidenceCard key={e.id} evidence={e} report={report} />)
      ) : (
        <div className="empty-evidence">
          <Icon name="info" />
          <p>此项尚未关联可定位的原文证据。请作为阅读提示，回到全文核查。</p>
        </div>
      )}
      {related.length > 0 && (
        <section className="related-section">
          <h3>沿着论证继续阅读</h3>
          {related.map((edge) => {
            const other = report.analysis.nodes.find(
              (n) => n.id === (edge.source === node.id ? edge.target : edge.source),
            );
            return (
              other && (
                <button key={edge.id} onClick={() => onNavigate(other.id)}>
                  <span>
                    <small>
                      {edge.label || (edge.source === node.id ? "进一步" : "来自")}
                    </small>
                    <AnalysisText text={other.title} />
                  </span>
                  <Icon name="arrow" size={15} />
                </button>
              )
            );
          })}
        </section>
      )}
    </div>
  );
}
