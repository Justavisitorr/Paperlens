import type { DashboardReport } from "../types";
import { asDataUrl } from "./pdf";

export interface SavedPaper {
  id: string;
  title: string;
  pages: number;
  date: string;
}
function openLibrary(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("paperlens-pages-library", 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore("reports");
      request.result.createObjectStore("index", { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function access<T>(
  stores: string[],
  mode: IDBTransactionMode,
  work: (tx: IDBTransaction) => IDBRequest<T>,
): Promise<T> {
  const db = await openLibrary();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(stores, mode);
      const request = work(tx);
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = tx.onabort = () => reject(tx.error || new Error("浏览器存储不可用。"));
    });
  } finally {
    db.close();
  }
}
export const listPapers = () =>
  access<SavedPaper[]>(["index"], "readonly", (tx) => tx.objectStore("index").getAll());
export const readPaper = (id: string) =>
  access<DashboardReport | undefined>(["reports"], "readonly", (tx) =>
    tx.objectStore("reports").get(id),
  );
export function describe(report: DashboardReport): SavedPaper {
  return {
    id: report.document.sha256,
    title: report.document.title,
    pages: report.document.pages.length,
    date: report.run.created_at,
  };
}
export async function savePaper(report: DashboardReport) {
  const meta = describe(report);
  await access(["reports", "index"], "readwrite", (tx) => {
    tx.objectStore("reports").put(report, meta.id);
    return tx.objectStore("index").put(meta);
  });
}
export async function forgetPaper(id: string) {
  await access(["reports", "index"], "readwrite", (tx) => {
    tx.objectStore("reports").delete(id);
    return tx.objectStore("index").delete(id);
  });
}
export async function readSample(): Promise<DashboardReport> {
  const read = async (path: string) => {
    const response = await fetch(new URL("./showcase/" + path, document.baseURI), {
      credentials: "omit",
    });
    if (!response.ok) throw new Error("示例论文加载失败，请重试。");
    return response;
  };
  const [report, pdf] = await Promise.all([
    read("bert-report.json").then((r) => r.json() as Promise<DashboardReport>),
    read("bert.pdf").then((r) => r.blob()),
  ]);
  const data = await asDataUrl(pdf.slice(0, pdf.size, "application/pdf"));
  report.source_pdf = {
    data_url: data,
    sha256: report.document.sha256,
  };
  return report;
}
