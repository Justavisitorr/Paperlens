import { groupWarnings, pageRanges } from "./qualityWarnings";
import type { StageFailure } from "./types";
import { FailureDetails } from "./FailureDetails";
import "./styles/27-quality-warnings.css";

export function QualityWarnings({
  warnings,
  failures = [],
}: {
  warnings: string[];
  failures?: StageFailure[];
}) {
  const groups = groupWarnings(
    failures.length
      ? warnings.filter((warning) => !warning.startsWith("部分模型请求"))
      : warnings,
  );
  if (!groups.length && !failures.length) return <p>处理流程未报告异常。</p>;
  return (
    <div className="quality-warning-groups">
      <FailureDetails failures={failures} />
      {groups.map((group) => (
        <details className="quality-warning-group" key={group.title}>
          <summary>
            <strong>{group.title}</strong>
            <small>
              {group.pages.length
                ? `${group.pages.length} 页`
                : `${group.details.length} 项`}
            </small>
            <span>{pageRanges(group.pages) || group.summary}</span>
          </summary>
          <ul className="plain-list">
            {group.details.map((detail) => (
              <li key={detail.text}>
                {detail.pages.length > 0 && (
                  <strong>{pageRanges(detail.pages)} · </strong>
                )}
                {detail.text.replace(/^本页\s*/, "")}
              </li>
            ))}
          </ul>
        </details>
      ))}
    </div>
  );
}
