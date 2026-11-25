import { RichText } from "./readerLinks";
import { evidencePageLabel, evidencePages } from "./evidenceSource";
import { conditionLabels, missingResultConditions } from "./experimentConditions";
import type { DashboardReport, Evidence, QuantitativeResult } from "./types";
import "./styles/32-reading-review.css";

export function ResultConditions({
  result,
  report,
  onEvidence,
}: {
  result: QuantitativeResult;
  report: DashboardReport;
  onEvidence?: (evidence: Evidence, page?: number) => void;
}) {
  const missing = missingResultConditions(result);
  return (
    <div className="experiment-conditions" data-result-id={result.id}>
      {result.condition && (
        <p>
          <RichText text={result.condition} />
        </p>
      )}
      {!!result.conditions?.length && (
        <ul className="result-conditions">
          {result.conditions.map((condition, index) => (
            <li key={`${condition.kind}:${index}`}>
              <strong>{conditionLabels[condition.kind]}：</strong>
              <RichText text={condition.value} />
              {condition.evidence_ids.map((id) => {
                const evidence = report.evidence.find((e) => e.id === id);
                if (!evidence) return null;
                const pages = evidencePages(evidence);
                if (onEvidence && pages.length > 1)
                  return (
                    <span key={id}>
                      条件出处 {evidencePageLabel(evidence)}（跨页段落）
                      {pages.map((page) => (
                        <button
                          key={page}
                          className="text-button"
                          data-evidence-id={id}
                          data-block-id={evidence.block_id}
                          onClick={() => onEvidence(evidence, page)}
                        >
                          查看 PDF 第 {page} 页
                        </button>
                      ))}
                    </span>
                  );
                return onEvidence ? (
                  <button
                    key={id}
                    className="text-button"
                    data-evidence-id={id}
                    data-block-id={evidence.block_id}
                    onClick={() => onEvidence(evidence)}
                  >
                    条件出处{" "}
                    {evidence.source_pages?.length
                      ? evidencePageLabel(evidence)
                      : `P${evidence.page}`}
                  </button>
                ) : (
                  <details
                    key={id}
                    data-evidence-id={id}
                    data-block-id={evidence.block_id}
                  >
                    <summary>
                      条件出处{" "}
                      {evidence.source_pages?.length
                        ? evidencePageLabel(evidence)
                        : `P${evidence.page}`}
                    </summary>
                    <blockquote>
                      <RichText text={evidence.quote} />
                    </blockquote>
                  </details>
                );
              })}
            </li>
          ))}
        </ul>
      )}
      {!!missing.length && (
        <small className="condition-missing">
          本报告未提取到：{missing.map((kind) => conditionLabels[kind]).join("、")}
          。请核查原文实验设置；这不代表论文未说明。
        </small>
      )}
      {!result.condition && !result.conditions?.length && !missing.length && (
        <small className="condition-missing">
          本报告未提供实验条件，请对照原文核查。
        </small>
      )}
    </div>
  );
}
