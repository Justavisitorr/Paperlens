import { createRoot } from "react-dom/client";
import { ContinuousTextView } from "../src/ContinuousTextView";
import type { DashboardReport } from "../src/types";
import "../src/styles/index.css";
const report = JSON.parse(
  document.getElementById("reader-test-data")!.textContent!,
) as DashboardReport;
createRoot(document.getElementById("root")!).render(
  <div className="reader-main" style={{ height: "100dvh", overflow: "auto" }}>
    <ContinuousTextView
      report={report}
      query=""
      onClearQuery={() => {}}
      onExport={() => {}}
      outlineSlot={null}
    />
  </div>,
);
