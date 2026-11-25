import { createRoot } from "react-dom/client";
import { useState } from "react";
import { PdfCrop, PdfSource } from "../src/PdfSource";
import type { DashboardReport } from "../src/types";
const report = JSON.parse(
  document.getElementById("pdf-test-data")!.textContent!,
) as DashboardReport;
const defaultBox = { x0: 0.1, y0: 0.2, x1: 0.8, y1: 0.5 };
function Harness() {
  const [current, setCurrent] = useState(report);
  (window as unknown as { setPdfTestReport: typeof setCurrent }).setPdfTestReport =
    setCurrent;
  const box =
    (current as DashboardReport & { test_box?: typeof defaultBox }).test_box ||
    defaultBox;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "700px 450px",
        height: "850px",
        gap: "24px",
      }}
    >
      <PdfSource report={current} page={1} box={box} />
      <div>
        <PdfCrop report={current} page={1} box={box} alt="原图裁剪" />
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<Harness />);
