import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import type { DashboardReport } from "./types";
import "./styles/index.css";

window.__paperlensReady = false;
window.__paperlensErrors = [];
window.__paperlensValidate = async () => ({
  graphs: [],
  errors: ["没有加载论文报告"],
});
window.addEventListener("error", (event) => {
  window.__paperlensErrors.push(event.message);
});
window.addEventListener("unhandledrejection", (event) => {
  window.__paperlensErrors.push(String(event.reason));
});

class ErrorBoundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: "" };
  static getDerivedStateFromError(error: Error) {
    window.__paperlensErrors.push(error.message);
    return { error: error.message };
  }
  render() {
    return this.state.error ? (
      <div className="page">
        <div className="empty">
          <strong>页面出现错误</strong>
          <p>{this.state.error}</p>
          <button className="button" onClick={() => location.reload()}>
            重新加载
          </button>
        </div>
      </div>
    ) : (
      this.props.children
    );
  }
}

const raw = document.getElementById("paperlens-data")?.textContent?.trim();
let report: DashboardReport | null = null;
let error = "";
// "null" (served workspace) and the unreplaced placeholder both mean: no embedded report.
if (raw && raw !== "null" && !raw.startsWith("__PAPERLENS_")) {
  try {
    const data = JSON.parse(raw) as DashboardReport;
    if (
      !data.analysis?.nodes ||
      !data.document?.blocks ||
      !data.graphs ||
      !data.run ||
      !data.quality
    )
      throw new Error("报告结构不完整，请重新生成。");
    report = data;
  } catch (reason) {
    error = `报告数据读取失败：${String(reason)}`;
    window.__paperlensErrors.push(error);
  }
}
createRoot(document.getElementById("root")!).render(
  <ErrorBoundary>
    <App embedded={report} bootError={error} />
  </ErrorBoundary>,
);
