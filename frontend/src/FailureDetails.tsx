import type { StageFailure } from "./types";
import { pageRanges } from "./qualityWarnings";
import "./styles/27-quality-warnings.css";

export function FailureDetails({ failures = [] }: { failures?: StageFailure[] }) {
  if (!failures.length) return null;
  return (
    <details className="failure-details">
      <summary>
        未完成步骤 · {[...new Set(failures.map((f) => f.label))].join("、")}
      </summary>
      <ul>
        {failures.map((failure, index) => (
          <li key={`${failure.stage}-${index}`}>
            <strong>{failure.label}</strong>
            <p className="failure-scope">
              {[
                failure.model,
                failure.pages.length ? `PDF ${pageRanges(failure.pages)}` : "",
                failure.batches > 0 ? `${failure.batches} 个批次` : "",
                failure.items > 0 ? `${failure.items} 项` : "",
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
            <p>
              {failure.http_status &&
              !failure.message.includes(`HTTP ${failure.http_status}`)
                ? `HTTP ${failure.http_status} · `
                : ""}
              {failure.message}
            </p>
            <p className="failure-guidance">{failure.guidance}</p>
          </li>
        ))}
      </ul>
    </details>
  );
}
