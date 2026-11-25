import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { App } from "../App";
import { embedReportHtml } from "../api";
import { downloadFile } from "../export";
import type { DashboardReport } from "../types";
import {
  describe,
  forgetPaper,
  listPapers,
  readPaper,
  readSample,
  savePaper,
  type SavedPaper,
} from "./library";
import { loadPdf, renderForModel, type LocalPdf } from "./pdf";
import { analyzeBatch, pagesPerRequest, providers, type Provider } from "./provider";
import { makeReport, type Batch } from "./report";
import "./browser.css";

function navigate(id?: string, replace = false) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("paper", id);
  else url.searchParams.delete("paper");
  url.hash = id ? "/read/local" : "";
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}
function listen(fn: () => void) {
  window.addEventListener("popstate", fn);
  window.addEventListener("hashchange", fn);
  return () => {
    window.removeEventListener("popstate", fn);
    window.removeEventListener("hashchange", fn);
  };
}
const activeId = () => new URLSearchParams(location.search).get("paper") || "";
const explain = (error: unknown) =>
  error instanceof Error ? error.message : "操作未完成，请重试。";
const ordered = (rows: SavedPaper[]) =>
  [...rows].sort((a, b) => b.date.localeCompare(a.date));

export default function BrowserWorkspace() {
  const id = useSyncExternalStore(listen, activeId);
  const [active, setActive] = useState<{ id: string; report: DashboardReport } | null>(
    null,
  );
  const [saved, setSaved] = useState<SavedPaper[]>([]);
  const memory = useRef(new Map<string, DashboardReport>());
  const [openError, setOpenError] = useState("");
  const [storeWarning, setStoreWarning] = useState("");
  const [exporting, setExporting] = useState(false);
  const [provider, setProvider] = useState<Provider>("deepseek");
  const [key, setKey] = useState("");
  const [doc, setDoc] = useState<LocalPdf | null>(null);
  const [preview, setPreview] = useState("");
  const [opening, setOpening] = useState(false);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const selection = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const checkpoint = useRef<{ batches: Batch[]; requests: number; seconds: number }>({
    batches: [],
    requests: 0,
    seconds: 0,
  });
  const total = doc ? Math.ceil(doc.pdf.numPages / pagesPerRequest) : 0;
  const completed = checkpoint.current.batches.length;

  useEffect(() => {
    listPapers()
      .then((rows) => setSaved(ordered(rows)))
      .catch(() => setStoreWarning("当前浏览器无法保存文献，可在读完后导出离线报告。"));
  }, []);
  useEffect(() => () => doc?.release(), [doc]);
  useEffect(
    () => () => {
      if (preview) URL.revokeObjectURL(preview);
    },
    [preview],
  );
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!id) {
      setActive(null);
      setOpenError("");
      return;
    }
    let live = true;
    const open = async () => {
      setOpenError("");
      if (!/^(?:bert|[a-f0-9]{64})$/.test(id)) throw new Error("该论文链接无效。");
      const report =
        memory.current.get(id) || (await (id === "bert" ? readSample() : readPaper(id)));
      if (!report)
        throw new Error(
          "这篇论文保存在导入它的浏览器中。请在原浏览器打开，或重新选择 PDF。",
        );
      if (!live) return;
      if (!location.hash.startsWith("#/read/local")) {
        history.replaceState(
          null,
          "",
          location.pathname + location.search + "#/read/local",
        );
      }
      setActive({ id, report });
    };
    open().catch((e) => {
      if (live) setOpenError(explain(e));
    });
    const home = () => {
      if (location.hash === "#/library") navigate(undefined, true);
    };
    window.addEventListener("hashchange", home);
    return () => {
      live = false;
      window.removeEventListener("hashchange", home);
    };
  }, [id]);

  async function choose(file?: File) {
    if (!file || controller.current) return;
    const current = ++selection.current;
    setError("");
    setMessage("");
    setDoc(null);
    setPreview("");
    setOpening(true);
    checkpoint.current = { batches: [], requests: 0, seconds: 0 };
    try {
      const result = await loadPdf(file);
      if (current !== selection.current) {
        result.release();
        return;
      }
      setDoc(result);
      setPreview(URL.createObjectURL(file.slice(0, file.size, "application/pdf")));
    } catch (e) {
      if (current === selection.current) setError(explain(e));
    } finally {
      if (current === selection.current) setOpening(false);
    }
  }
  function changeProvider(next: Provider) {
    if (controller.current) return;
    setProvider(next);
    setKey("");
    setError("");
    setMessage("");
    checkpoint.current = { batches: [], requests: 0, seconds: 0 };
  }
  async function remove(row: SavedPaper) {
    if (!window.confirm("从当前浏览器删除《" + row.title + "》的 PDF 和报告？")) return;
    try {
      if (!memory.current.has(row.id)) await forgetPaper(row.id);
      memory.current.delete(row.id);
      setSaved((items) => items.filter((item) => item.id !== row.id));
    } catch {
      setStoreWarning("浏览器未能删除这份报告，请稍后重试。");
    }
  }
  async function analyze() {
    if (!doc || !key.trim() || controller.current) return;
    const job = new AbortController();
    controller.current = job;
    setWorking(true);
    setError("");
    setStoreWarning("");
    const progress = checkpoint.current;
    const start = performance.now();
    const pageCount = doc.pdf.numPages;
    let stage = "";
    try {
      for (let batch = progress.batches.length; batch < total; batch++) {
        const from = batch * pagesPerRequest + 1,
          to = Math.min(pageCount, from + pagesPerRequest - 1);
        stage =
          "第 " +
          (batch + 1) +
          "/" +
          total +
          " 批 · PDF 第 " +
          from +
          (to === from ? "" : "–" + to) +
          " 页";
        setMessage(stage + " · 正在准备页图");
        const inputs = [],
          local = [];
        for (let i = from; i <= to; i++) {
          const rendered = await renderForModel(doc, i, job.signal);
          inputs.push(rendered.input);
          local.push(rendered.page);
        }
        job.signal.throwIfAborted();
        setMessage(stage + " · " + providers[provider].name + " 正在阅读");
        progress.requests++;
        const answer = await analyzeBatch(provider, key, inputs, job.signal);
        progress.batches.push({ answer, pages: local });
        job.signal.throwIfAborted();
      }
      const report = makeReport(
        doc,
        progress.batches,
        providers[provider].model,
        progress.seconds + (performance.now() - start) / 1000,
        progress.requests,
      );
      const meta = describe(report);
      memory.current.set(meta.id, report);
      try {
        await savePaper(report);
        memory.current.delete(meta.id);
      } catch {
        setStoreWarning("浏览器空间不足或禁止存储。报告保留在此标签页，请导出离线副本。");
      }
      setSaved((old) => ordered([...old.filter((p) => p.id !== meta.id), meta]));
      setKey("");
      setDoc(null);
      setPreview("");
      setMessage("");
      checkpoint.current = { batches: [], requests: 0, seconds: 0 };
      navigate(meta.id);
    } catch (e) {
      progress.seconds += (performance.now() - start) / 1000;
      setMessage("");
      setError(
        stage +
          " · " +
          (job.signal.aborted ? "已停止。已发出的请求可能计费。" : explain(e)) +
          " 已完成 " +
          progress.batches.length +
          "/" +
          total +
          " 批。",
      );
    } finally {
      controller.current = null;
      setWorking(false);
    }
  }
  async function exportReport(report: DashboardReport) {
    setExporting(true);
    setOpenError("");
    try {
      const response = await fetch(new URL("./", document.baseURI), {
        credentials: "omit",
      });
      if (!response.ok) throw new Error("读取离线阅读器失败，请稍后再试。");
      const html = embedReportHtml(await response.text(), report);
      downloadFile(
        html,
        report.document.source_name.replace(/\.pdf$/i, "") + "-PaperLens.html",
        "text/html;charset=utf-8",
      );
    } catch (e) {
      setOpenError(explain(e));
    } finally {
      setExporting(false);
    }
  }

  if (id)
    return (
      <div className="bw-reader">
        <div className="bw-readerbar">
          <button onClick={() => navigate()}>← 网页文献库</button>
          <span role="status">
            {storeWarning ||
              (active?.id === id ? (id === "bert" ? "BERT 公开示例" : "本机文献") : "")}
          </span>
          {active?.id === id && (
            <button disabled={exporting} onClick={() => void exportReport(active.report)}>
              {exporting ? "正在打包…" : "导出离线 HTML"}
            </button>
          )}
        </div>
        {openError && (
          <p className="bw-feedback err" role="alert">
            {openError}
          </p>
        )}
        {active?.id === id ? (
          <App
            key={active.report.document.sha256}
            embedded={active.report}
            bootError=""
          />
        ) : (
          !openError && (
            <p className="bw-loading" role="status">
              正在打开论文与原始 PDF…
            </p>
          )
        )}
      </div>
    );

  return (
    <div className="bw-home">
      <header className="bw-head">
        <a className="bw-brand" href="./">
          <img src="./showcase/paperlens.svg" alt="" />
          PaperLens
        </a>
        <nav aria-label="项目导航">
          <a href="./about.html">项目介绍</a>
          <a href="https://github.com/Justavisitorr/Paperlens">GitHub ↗</a>
        </nav>
      </header>
      <main className="bw-content">
        <div className="bw-heading">
          <h1>论文阅读工作区</h1>
          <p>导入 PDF，打开研究导图，把每条论点放回原文里读。</p>
        </div>
        <section className="bw-import" aria-label="导入论文">
          <div className="bw-select">
            <h2>导入自己的论文</h2>
            <div
              className={"bw-drop" + (drag ? " drag" : "")}
              onDragOver={(e) => {
                e.preventDefault();
                setDrag(true);
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDrag(false);
                void choose(e.dataTransfer.files[0]);
              }}
            >
              <div className="bw-paper-icon" aria-hidden="true">
                PDF
              </div>
              <div>
                <strong>
                  {opening ? "正在读取 PDF…" : doc ? doc.filename : "将 PDF 拖到这里"}
                </strong>
                <p>
                  {doc
                    ? doc.pdf.numPages +
                      " 页 · " +
                      ((doc.source.byte_size || 0) / 1048576).toFixed(1) +
                      " MB"
                    : "或从本机选择一份文件"}
                </p>
              </div>
              <input
                ref={fileInput}
                type="file"
                accept=".pdf,application/pdf"
                aria-label="选择 PDF 文件"
                hidden
                onChange={(e) => {
                  void choose(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
              <button
                className="bw-secondary"
                disabled={working}
                onClick={() => fileInput.current?.click()}
              >
                {doc ? "更换文件" : "选择 PDF"}
              </button>
            </div>
            {preview && (
              <a className="bw-source" href={preview} target="_blank" rel="noreferrer">
                阅读原始 PDF ↗
              </a>
            )}
          </div>
          <form
            className="bw-settings"
            onSubmit={(e) => {
              e.preventDefault();
              void analyze();
            }}
          >
            <label>
              模型服务
              <select
                disabled={working}
                value={provider}
                onChange={(e) => changeProvider(e.target.value as Provider)}
              >
                {Object.entries(providers).map(([val, p]) => (
                  <option key={val} value={val}>
                    {p.name} · {p.model}
                  </option>
                ))}
              </select>
            </label>
            <label>
              自己的 API 密钥
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={key}
                disabled={working}
                onChange={(e) => setKey(e.target.value)}
                placeholder="仅用于本次分析"
              />
            </label>
            <p className="bw-cost">
              {doc
                ? "预计 " + (total - completed) + " 次模型请求。"
                : "每 4 页合为一次请求。"}
              页图直接发给 {providers[provider].name}，费用由你的账户承担。
            </p>
            {working ? (
              <button
                type="button"
                className="bw-secondary"
                onClick={() => controller.current?.abort()}
              >
                停止分析
              </button>
            ) : (
              <button
                type="submit"
                className="bw-primary"
                disabled={!doc || !key.trim() || opening}
              >
                {error && doc ? "重试未完成批次" : "开始分析"}{" "}
                <span aria-hidden="true">→</span>
              </button>
            )}
          </form>
          {(message || error) && (
            <p
              className={"bw-feedback" + (error ? " err" : "")}
              role={error ? "alert" : "status"}
            >
              {error || message}
            </p>
          )}
        </section>
        <section className="bw-library">
          <h2>我的文献</h2>
          <p className="bw-help">
            {storeWarning ||
              "论文与报告保存在当前浏览器。密钥仅在本标签页使用，完成后清除。"}
          </p>
          {saved.map((row) => (
            <div className="bw-entry" key={row.id}>
              <button
                disabled={working}
                className="bw-row"
                onClick={() => navigate(row.id)}
              >
                <span>{row.title}</span>
                <small>{row.pages} 页</small>
                <b aria-hidden="true">→</b>
              </button>
              <button
                className="bw-forget"
                disabled={working}
                aria-label={"删除 " + row.title}
                onClick={() => void remove(row)}
              >
                删除
              </button>
            </div>
          ))}
          <button
            disabled={working}
            className="bw-sample"
            onClick={() => navigate("bert")}
          >
            <img src="./showcase/bert-architecture.webp" alt="BERT 模型结构图" />
            <span>
              <small>公开示例 · 免密钥试读</small>
              <strong>BERT</strong>
              <span>16 页原文 · 21 个导图节点 · 91 条证据</span>
            </span>
            <b aria-hidden="true">→</b>
          </button>
        </section>
      </main>
    </div>
  );
}
