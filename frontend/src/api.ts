/**
 * Browser client for the local PaperLens service.
 *
 * The contract lives in docs/FRONTEND_API.md. Every endpoint beyond the
 * original upload API is feature-detected: a 404/405 falls back to what the
 * current server already offers, so the workspace keeps working while the
 * backend grows. Saved credentials are never read back; account forms send keys only to the local service.
 */
import type { DashboardReport, StageFailure } from "./types";
import type { ReadingNote } from "./readingNotes";
import type { InputBudget, InputBudgetApproval } from "./inputBudget";
import { normalizeSpeedMode, type ProcessingSpeedMode } from "./processingSpeed";
import { hasExternalAssets } from "./reportAssets";
export type { ProcessingSpeedMode } from "./processingSpeed";

export type AutoNoteType = "summary" | "explain" | "connections" | "questions" | "check";
export type AutoNoteContext = "nearby" | "section" | "paper";
export interface AutoNoteSettings {
  enabled: boolean;
  max_paragraphs: number;
  context_scope: AutoNoteContext;
  types: AutoNoteType[];
}
export interface AutoNoteRequest {
  input_budget?: InputBudgetApproval;
  scope: "section" | "paper";
  anchor_id?: string;
  item_id?: string;
  source_version?: string;
  dry_run?: boolean;
}
export interface AutoNotePlan {
  eligible: number;
  planned: number;
  skipped: number;
  max_paragraphs: number;
  types: AutoNoteType[];
  context_scope: AutoNoteContext;
  max_calls: number;
  source_version: string;
  items: { item_id: string; page: number; label: string; kind: string }[];
}
export interface AutoNoteResult extends AutoNotePlan {
  notes: ReadingNote[];
  generated: number;
  errors: { item_id: string; message: string; input_budget?: InputBudget }[];
}
export function planAutoNotes(paperId: string, body: AutoNoteRequest) {
  return request<AutoNotePlan>(`${paperPath(paperId)}/notes/generate`, {
    method: "POST",
    json: { ...body, dry_run: true },
  });
}
export function generateAutoNote(paperId: string, body: AutoNoteRequest) {
  return request<AutoNoteResult>(`${paperPath(paperId)}/notes/generate`, {
    method: "POST",
    json: { ...body, dry_run: false },
  });
}

export type JobStatus =
  "queued" | "running" | "paused" | "complete" | "partial" | "failed" | "cancelled";
export type JobAction = "pause" | "resume" | "cancel" | "retry" | "accelerate" | "ultra";
export type ProcessingProfileId = "economy" | "standard" | "detailed";
export interface ProcessingProfiles {
  default: ProcessingProfileId;
  auto_notes?: AutoNoteSettings;
  profiles: {
    id: ProcessingProfileId;
    label: string;
    description: string;
    /** 0.5, 1 or 2: the multiplier applied to the settings values. */
    factor?: number;
    unlimited_limits?: string[];
    limits: {
      ai_direct?: boolean;
      max_requests: number;
      input_chars: number;
      output_tokens: number;
      reading_output_tokens: number;
      vision_pages: number;
      vision_enabled?: boolean;
      math_enabled?: boolean;
      inline_math_regions?: number;
      chart_crop_long_edge?: number;
      chart_crops_per_page?: number;
      chart_refinement_requests?: number;
      chart_readings_per_figure?: number;
      math_regions?: number;
      math_seconds?: number;
      transcription_regions?: number;
      math_review_batches?: number;
      code_blocks?: boolean;
    };
  }[];
  /** One sentence stating how tiers derive from the settings page. */
  rule?: string;
  cost_note: string;
}
export interface JobState {
  failures?: StageFailure[];
  input_budget?: InputBudget | null;
  source_sha256?: string;
  model?: string;
  id: string;
  source_name: string;
  status: JobStatus;
  /** Available as soon as the upload is saved, independent of processing. */
  source_url?: string | null;
  stage: string;
  message: string;
  progress?: number;
  report_url?: string | null;
  error?: string | null;
  created_at?: string;
  actions?: JobAction[];
  speed_mode?: ProcessingSpeedMode;
  acceleration?: { enabled: boolean; concurrency: number; mode?: ProcessingSpeedMode };
  paper_id?: string | null;
  processing_profile?: ProcessingProfileId;
  profile_label?: string;
}
export interface PaperSummary {
  id: string;
  document_sha256?: string;
  title: string;
  source_name: string;
  created_at?: string;
  pages?: number;
  paper_type?: string;
  status?: string;
  report_url?: string | null;
  data_url?: string | null;
  model?: string;
  node_count?: number;
  evidence_count?: number;
  original_title?: string;
  alternate_title?: string;
  authors?: string[];
  year?: string;
  venue?: string;
  abstract?: string;
  thumbnail_url?: string;
  pdf_url?: string;
  source_url?: string;
  doi?: string;
  reading_status?: "unread" | "reading" | "read";
  starred?: boolean;
  last_opened?: string;
  reading_source_version?: string;
  reading_item_ids?: string[];
  reading_progress?: number;
}
export interface ImportCandidate {
  id: string;
  title: string;
  authors?: string[];
  year?: string | number | null;
  venue?: string;
  abstract?: string;
  url?: string;
  doi?: string;
  pdf_url?: string;
}
export function resolveImport(query: string) {
  return request<{ candidates: ImportCandidate[]; message?: string }>(
    "/api/imports/resolve",
    {
      method: "POST",
      json: { query },
    },
  );
}
export async function startImport(
  candidateId: string,
  profile: ProcessingProfileId,
  unlimitedConfirmed = false,
  accelerated: boolean | ProcessingSpeedMode = false,
) {
  const speedMode = normalizeSpeedMode(accelerated);
  const result = await request<{ id: string }>("/api/imports", {
    method: "POST",
    json: {
      candidate_id: candidateId,
      profile,
      unlimited_confirmed: unlimitedConfirmed,
      accelerated: speedMode !== "normal",
      speed_mode: speedMode,
    },
  });
  remember(result.id);
  return result;
}
export function updatePaper(
  id: string,
  values: {
    title?: string;
    starred?: boolean;
    reading_status?: "unread" | "reading" | "read";
    opened?: boolean;
  },
): Promise<PaperSummary> {
  return request(`/api/library/${encodeURIComponent(id)}`, {
    method: "PATCH",
    json: values,
  });
}
export interface Health {
  ok: boolean;
  queued_jobs: number;
  running_jobs: number;
  features?: string[];
  version?: string;
}
export interface SettingsOption {
  value: string;
  label: string;
}
export type PaperReadingMode = "classic" | "ai_direct";
export interface SettingsField {
  key: string;
  label: string;
  type: "text" | "number" | "select" | "boolean" | "secret" | "textarea";
  value: string | number | boolean | null;
  options?: SettingsOption[];
  help?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  readonly?: boolean;
  /** Route-specific controls retain their saved value while unavailable. */
  disabled_in_modes?: PaperReadingMode[];
  disabled_reason?: string;
  /** For secrets: whether a value is stored. The value itself is never sent. */
  configured?: boolean;
  /** For values that scale with the processing tier: the saved value in each tier. */
  tiers?: Record<ProcessingProfileId, number>;
}
export interface SettingsGroup {
  id: string;
  title: string;
  description?: string;
  fields: SettingsField[];
}
export interface SettingsSchema {
  groups: SettingsGroup[];
  meta?: { source?: string; updated_at?: string; stale?: boolean };
}
/** "library" etc. = native endpoint; "fallback" = reconstructed from jobs. */
export type Source = "native" | "fallback";

export const serverMode = import.meta.env.MODE !== "pages" && /^https?:$/.test(location.protocol);

export class ApiError extends Error {
  status: number;
  constructor(
    message: string,
    status: number,
    public inputBudget?: InputBudget,
  ) {
    super(message);
    this.status = status;
  }
}
const unsupported = (e: unknown) =>
  e instanceof ApiError && (e.status === 404 || e.status === 405);
/** Polled list endpoints the server lacks; probed once per page load. */
const missing = new Set<"jobs" | "library">();

/**
 * Business messages from the service are Chinese and shown as they are;
 * framework defaults ("Not Found", …) are replaced by a plain local line.
 */
function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string" && /[一-鿿]/.test(detail)) return detail;
  if (status === 404 || status === 405) return "服务尚未提供该功能";
  if (status === 413) return "文件太大，服务拒绝接收";
  if (status === 401 || status === 403) return "服务拒绝了这次请求";
  if (status >= 500) return `服务内部错误（${status}），请稍后重试`;
  return `请求失败（${status}）`;
}

async function request<T>(
  path: string,
  init?: RequestInit & { json?: unknown },
): Promise<T> {
  const { json, ...rest } = init || {};
  const response = await fetch(path, {
    ...rest,
    headers: json ? { "Content-Type": "application/json" } : rest.headers,
    body: json ? JSON.stringify(json) : rest.body,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      errorMessage(body.detail, response.status),
      response.status,
      body.input_budget,
    );
  }
  const type = response.headers.get("content-type") || "";
  return (type.includes("json") ? response.json() : response.text()) as Promise<T>;
}

/* ---------- local memory (ids + small metadata only, never reports) ---------- */

const JOBS_KEY = "paperlens.jobs";

export function getJob(id: string, signal?: AbortSignal): Promise<JobState> {
  return request(`/api/jobs/${encodeURIComponent(id)}`, { signal });
}

/** Binary fetch stays on the authenticated, account-scoped job endpoint. */
export async function jobSourcePdf(id: string, signal?: AbortSignal): Promise<Blob> {
  if (!/^[0-9a-f]{32}$/.test(id)) throw new Error("任务编号无效。");
  const response = await fetch(`/api/jobs/${id}/source.pdf`, { signal });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(errorMessage(body.detail, response.status), response.status);
  }
  if (!response.headers.get("content-type")?.toLowerCase().includes("application/pdf"))
    throw new Error("服务没有返回原始 PDF，请确认登录状态后重试。");
  return response.blob();
}
const META_KEY = "paperlens.papers";
const HIDDEN_KEY = "paperlens.hidden";
const read = <T>(key: string, fallback: T): T => {
  try {
    return JSON.parse(localStorage.getItem(key) || "") as T;
  } catch {
    return fallback;
  }
};
const write = (key: string, value: unknown) => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage may be disabled; memory is a convenience only */
  }
};
const knownJobs = () => read<string[]>(JOBS_KEY, []);
const remember = (id: string) =>
  write(JOBS_KEY, [id, ...knownJobs().filter((x) => x !== id)].slice(0, 200));
type Meta = Partial<PaperSummary>;
const metadata = () => read<Record<string, Meta>>(META_KEY, {});
export function rememberReport(id: string, report: DashboardReport) {
  write(META_KEY, {
    ...metadata(),
    [id]: {
      title:
        report.reading?.information_items?.titles?.[0]?.content ||
        report.document.title ||
        report.analysis.title,
      pages: report.document.pages.length,
      paper_type: report.analysis.paper_type,
      node_count: report.analysis.nodes.length,
      evidence_count: report.evidence.length,
      model: report.run.model,
      created_at: report.run.created_at,
    },
  });
}

/* ---------- endpoints ---------- */

export function getHealth(): Promise<Health> {
  return request<Health>("/api/health");
}
export const hasFeature = (health: Health | null, feature: string) =>
  Boolean(health?.features?.includes(feature));

export async function clearJobHistory(): Promise<{
  removed_ids: string[];
  failed: number;
}> {
  const result = await request<{ removed_ids: string[]; failed: number }>(
    "/api/jobs/history",
    { method: "DELETE" },
  );
  const removed = new Set(result.removed_ids);
  write(
    JOBS_KEY,
    knownJobs().filter((id) => !removed.has(id)),
  );
  return result;
}

export async function listJobs(): Promise<{
  jobs: JobState[];
  source: Source;
}> {
  if (!missing.has("jobs"))
    try {
      const body = await request<{ jobs: JobState[] }>("/api/jobs");
      return { jobs: body.jobs, source: "native" };
    } catch (e) {
      if (!unsupported(e)) throw e;
      missing.add("jobs");
    }
  const jobs = await Promise.all(
    knownJobs().map((id) =>
      request<JobState>(`/api/jobs/${encodeURIComponent(id)}`).catch(() => null),
    ),
  );
  return {
    jobs: jobs.filter((j): j is JobState => Boolean(j)),
    source: "fallback",
  };
}

export async function listLibrary(): Promise<{
  papers: PaperSummary[];
  storage_path?: string;
  source: Source;
}> {
  const hidden = new Set(read<string[]>(HIDDEN_KEY, []));
  if (!missing.has("library"))
    try {
      const body = await request<{
        papers: PaperSummary[];
        storage_path?: string;
      }>("/api/library");
      return { ...body, source: "native" };
    } catch (e) {
      if (!unsupported(e)) throw e;
      missing.add("library");
    }
  const { jobs } = await listJobs();
  const meta = metadata();
  const papers = jobs
    .filter(
      (j) => (j.status === "complete" || j.status === "partial") && !hidden.has(j.id),
    )
    .map<PaperSummary>((j) => ({
      id: j.id,
      title: j.source_name.replace(/\.pdf$/i, ""),
      source_name: j.source_name,
      status: j.status,
      report_url: j.report_url,
      created_at: j.created_at,
      ...meta[j.id],
    }));
  return { papers, source: "fallback" };
}

export function getProcessingProfiles(signal?: AbortSignal): Promise<ProcessingProfiles> {
  return request<ProcessingProfiles>("/api/processing-profiles", { signal });
}

/** A [min, max] USD pair per pricing period; cache hits are unknown up front. */
export type CostRange =
  | { status: "range"; off_peak?: [number, number]; peak?: [number, number] }
  | {
      status: "partial";
      off_peak?: [number, number];
      peak?: [number, number];
      unpriced_models: string[];
    }
  | { status: "unknown"; reason?: string };
export type Spread = { low: number; typical: number; high: number };
export interface ProfileEstimate {
  profile: ProcessingProfileId;
  label: string;
  /** Compatible sample count; time calibration may use individual model calls. */
  samples: number;
  /** Compatibility flag; estimates use each tier's current feature settings. */
  same_profile: boolean;
  time_basis?: string;
  budget_limited?: boolean;
  /** Request cap × per-request limits; null when requests are unlimited. */
  ceiling: {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    cost: CostRange;
  } | null;
  typical: {
    requests: Spread;
    prompt_tokens: Spread;
    completion_tokens: Spread;
    seconds: Spread;
    cost: { low: CostRange; typical: CostRange; high: CostRange };
  } | null;
}
export interface ProcessingEstimate {
  pages: number;
  pages_assumed: boolean;
  model: string;
  history_runs: number;
  chars_per_token: number;
  price_checked_at: string | null;
  price_stale: boolean;
  parallel_jobs?: number;
  document_features?: Record<string, number | string>;
  profiles: ProfileEstimate[];
}

// Reuse only PDF metadata, never an estimate made with earlier settings.
const estimateDocuments = new WeakMap<
  File,
  Pick<ProcessingEstimate, "pages" | "document_features">
>();

/** Estimate one staged PDF (its pages are counted), or a typical paper. */
export async function estimateProcessing(
  file?: File,
  signal?: AbortSignal,
  accelerated: boolean | ProcessingSpeedMode = false,
): Promise<ProcessingEstimate> {
  const form = new FormData();
  const speedMode = normalizeSpeedMode(accelerated);
  form.append("accelerated", String(speedMode !== "normal"));
  form.append("speed_mode", speedMode);
  const document = file && estimateDocuments.get(file);
  if (document) {
    form.append("pages", String(document.pages));
    if (document.document_features)
      form.append("document_features", JSON.stringify(document.document_features));
  } else if (file) form.append("file", file);
  const result = await request<ProcessingEstimate>("/api/estimates", {
    method: "POST",
    body: form,
    signal,
  });
  if (file && !result.pages_assumed && !signal?.aborted)
    estimateDocuments.set(file, {
      pages: result.pages,
      document_features: result.document_features,
    });
  return result;
}

export interface RunCost {
  status: "complete" | "partial" | "unknown" | "none";
  currency: "USD";
  price_checked_at: string | null;
  price_stale: boolean;
  models: {
    model: string;
    status: string;
    off_peak?: [number, number];
    peak?: [number, number];
  }[];
  off_peak?: [number, number];
  peak?: [number, number];
}

/** A finished run's recorded usage at today's reference prices. */
export function runCost(run: DashboardReport["run"]): Promise<RunCost> {
  return request<RunCost>("/api/estimates/run", { method: "POST", json: { run } });
}

export async function submitJob(
  file: File,
  profile: ProcessingProfileId = "standard",
  unlimitedConfirmed = false,
  accelerated: boolean | ProcessingSpeedMode = false,
): Promise<{ id: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("profile", profile);
  form.append("unlimited_confirmed", String(unlimitedConfirmed));
  const speedMode = normalizeSpeedMode(accelerated);
  form.append("accelerated", String(speedMode !== "normal"));
  form.append("speed_mode", speedMode);
  const result = await request<{ id: string }>("/api/jobs", {
    method: "POST",
    body: form,
  });
  remember(result.id);
  return result;
}

export function jobAction(
  id: string,
  action: JobAction,
  inputBudget?: InputBudgetApproval,
): Promise<JobState> {
  return request<JobState>(`/api/jobs/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    ...(inputBudget
      ? { json: { input_budget: inputBudget } }
      : ["accelerate", "ultra"].includes(action)
        ? { json: { confirmed: true } }
        : {}),
  });
}

/** Extracts the embedded report from a standalone dashboard HTML. */
function reportFromHtml(html: string): DashboardReport {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const raw = doc.getElementById("paperlens-data")?.textContent?.trim();
  if (!raw || raw.startsWith("__PAPERLENS_")) throw new Error("报告中没有数据");
  return JSON.parse(raw) as DashboardReport;
}

interface ReportSource {
  id: string;
  data_url?: string | null;
  report_url?: string | null;
}

function reportDataPath(paper: ReportSource, web: boolean): string {
  const path = paper.data_url || `/api/library/${encodeURIComponent(paper.id)}/report`;
  const url = new URL(path, "http://paperlens.invalid");
  if (web) url.searchParams.set("view", "web");
  else url.searchParams.delete("view");
  return path.startsWith("/") ? url.pathname + url.search : url.href;
}

async function fetchReport(paper: ReportSource, web: boolean): Promise<DashboardReport> {
  const id = encodeURIComponent(paper.id);
  try {
    return await request<DashboardReport>(reportDataPath(paper, web));
  } catch (e) {
    if (!unsupported(e)) throw e;
  }
  const html = await request<string>(paper.report_url || `/api/jobs/${id}/report`);
  return reportFromHtml(html);
}

const reports = new Map<string, Promise<DashboardReport>>();
export function loadReport(paper: ReportSource): Promise<DashboardReport> {
  const cached = reports.get(paper.id);
  if (cached) return cached;
  const job = fetchReport(paper, true);
  reports.set(paper.id, job);
  job.then(
    (report) => rememberReport(paper.id, report),
    () => reports.delete(paper.id),
  );
  return job;
}

// Full reports are only held while an explicit export is in flight, not in the
// reader cache. Two simultaneous export actions still share one full download.
const completeReports = new Map<string, Promise<DashboardReport>>();
export function loadCompleteReport(paper: ReportSource): Promise<DashboardReport> {
  const cached = completeReports.get(paper.id);
  if (cached) return cached;
  const job = fetchReport(paper, false).then((report) => {
    if (hasExternalAssets(report))
      throw new Error("完整论文资源尚未准备好，无法生成离线文件。");
    return report;
  });
  completeReports.set(paper.id, job);
  const clear = () => {
    if (completeReports.get(paper.id) === job) completeReports.delete(paper.id);
  };
  void job.then(clear, clear);
  return job;
}

const DATA_SLOT =
  /(<script id="paperlens-data" type="application\/json">)\s*(?:null|__PAPERLENS_[A-Z]+__)\s*(<\/script>)/;
const NOTICES =
  /<script id="paperlens-open-source-notices" type="application\/json">[\s\S]*?<\/script>/;

export function embedReportHtml(template: string, report: DashboardReport): string {
  if (!DATA_SLOT.test(template)) throw new Error("当前页面资源不完整，无法生成离线文件");
  const data = JSON.stringify(report)
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
  return template.replace(DATA_SLOT, (_, open, close) => open + data + close);
}

/**
 * A standalone copy of the page being served, with one paper's report inside.
 * The dashboard a job saved keeps the frontend it was generated with, so an
 * older paper would otherwise download without later reader features.
 */
export async function offlineHtml(paper: {
  id: string;
  data_url?: string | null;
  report_url?: string | null;
}): Promise<string> {
  const [template, report] = await Promise.all([
    request<string>("/", { cache: "no-store" }),
    loadCompleteReport(paper),
  ]);
  let html = embedReportHtml(template, report);
  // Carry over the open-source notices the service appends to saved reports.
  const saved = paper.report_url
    ? await request<string>(paper.report_url).catch(() => "")
    : "";
  const notices = saved.match(NOTICES)?.[0];
  const end = html.lastIndexOf("</body>");
  if (notices && end >= 0) html = html.slice(0, end) + notices + html.slice(end);
  return html;
}

/**
 * Deletes on the server when supported; otherwise only hides the paper from
 * this browser's list. Returns which of the two happened.
 */
export async function removePaper(id: string): Promise<"deleted" | "hidden"> {
  reports.delete(id);
  completeReports.delete(id);
  try {
    await request(`/api/library/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    return "deleted";
  } catch (e) {
    if (!unsupported(e)) throw e;
  }
  write(HIDDEN_KEY, [...read<string[]>(HIDDEN_KEY, []), id]);
  return "hidden";
}

export async function getSettings(): Promise<SettingsSchema | null> {
  try {
    return await request<SettingsSchema>("/api/settings");
  } catch (e) {
    if (unsupported(e)) return null;
    throw e;
  }
}

export function saveSettings(
  values: Record<string, unknown>,
): Promise<{ errors?: Record<string, string>; schema?: SettingsSchema }> {
  return request("/api/settings", { method: "PUT", json: { values } });
}

export interface Explanation {
  answer: string;
  evidence_ids?: string[];
  model?: string;
  /** Source sentences the answer rests on (BACKEND_API_ADDENDUM). */
  quotes?: { page: number; block_id: string; quote: string }[];
  /** The quotes were matched in the paper; not a verdict on the answer. */
  grounded?: boolean;
  warnings?: string[];
}
/** T14: explain a text selection; the service holds the credentials. */
export function explainSelection(
  paperId: string,
  selection: { block_id: string; start: number; end: number; text: string },
): Promise<Explanation> {
  return request<Explanation>(`/api/library/${encodeURIComponent(paperId)}/explain`, {
    method: "POST",
    json: selection,
  });
}

/* ---------- reading assistant: annotations, questions, conversations ---------- */

/** A pick in block text: UTF-16 offsets into `document.blocks[].text`. */
export interface Selection {
  block_id: string;
  start: number;
  end: number;
  text: string;
}
export interface AnnotationAppearance {
  color?: "yellow" | "green" | "blue" | "pink";
  style?: "highlight" | "underline";
}
export interface Annotation extends AnnotationAppearance {
  id: string;
  selection: Selection;
  exact: string;
  note: string;
  page: number;
  section?: string;
  created_at: string;
  updated_at?: string;
  source_version?: string;
  document_sha256?: string;
}
const paperPath = (paperId: string) => `/api/library/${encodeURIComponent(paperId)}`;
export async function listAnnotations(paperId: string): Promise<Annotation[]> {
  return (
    await request<{ annotations: Annotation[] }>(`${paperPath(paperId)}/annotations`)
  ).annotations;
}
export function createAnnotation(
  paperId: string,
  selection: Selection & AnnotationAppearance & { note?: string },
): Promise<Annotation> {
  return request(`${paperPath(paperId)}/annotations`, {
    method: "POST",
    json: selection,
  });
}
export function updateAnnotation(
  paperId: string,
  id: string,
  patch: AnnotationAppearance & { note?: string },
): Promise<Annotation> {
  return request(`${paperPath(paperId)}/annotations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    json: patch,
  });
}
export async function deleteAnnotation(paperId: string, id: string): Promise<void> {
  await request(`${paperPath(paperId)}/annotations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export interface ChatQuote {
  /** 1-based; the answer marks it as [n]. Absent on single-paper replies. */
  n?: number;
  paper_id?: string;
  page: number;
  block_id: string;
  quote: string;
  section?: string;
}
export interface ChatReply extends Explanation {
  id?: string;
  quotes?: ChatQuote[];
  papers?: { paper_id: string; ref: string; title: string }[];
}
export interface ChatHistory {
  role: "user" | "assistant";
  content: string;
}
/** `POST /api/chat`: several papers, picked passages and the reader's own terms. */
export function askPapers(
  body: {
    input_budget?: InputBudgetApproval;
    model_role?: "chat" | "conversation";
    question: string;
    history: ChatHistory[];
    papers: string[];
    reading_items?: { paper_id: string; item_id: string; source_version: string }[];
    passages: (Selection & { paper_id: string })[];
    terms: { term: string; explanation: string; note: string; paper_id?: string }[];
  },
  signal?: AbortSignal,
): Promise<ChatReply> {
  return request("/api/chat", { method: "POST", json: body, signal });
}
/** One paper only; used when the service predates `chat.papers`. */
export function askPaper(
  paperId: string,
  body: { question: string; history: ChatHistory[]; selection?: Selection },
  signal?: AbortSignal,
): Promise<ChatReply> {
  return request(`${paperPath(paperId)}/chat`, { method: "POST", json: body, signal });
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  pinned: boolean;
  messages: number;
  paper_ids: string[];
  preview: string;
}
export async function listConversations(): Promise<ConversationSummary[]> {
  return (await request<{ conversations: ConversationSummary[] }>("/api/conversations"))
    .conversations;
}
export function getConversation<T>(id: string): Promise<T> {
  return request(`/api/conversations/${id}`);
}
export function putConversation(
  id: string,
  doc: unknown,
): Promise<{ id: string; updated_at: string }> {
  return request(`/api/conversations/${id}`, { method: "PUT", json: doc });
}
export async function deleteConversation(id: string): Promise<void> {
  await request(`/api/conversations/${id}`, { method: "DELETE" });
}

/* ---------- saved model connections and per-task assignments ---------- */
export interface ModelProvider {
  id: string;
  name: string;
  base_url: string;
  protocol: "openai" | "anthropic";
  key_url?: string;
  docs_url?: string;
  requires_key: boolean;
}
export interface ServiceModel {
  id: string;
  name?: string;
  vision: "supported" | "unsupported" | "unknown";
}
export interface RemoteModels {
  items: ServiceModel[];
  status: "live" | "cached" | "empty" | "failed";
  fetched_at?: string | null;
  error?: string | null;
}
export interface ModelAccount {
  id: string;
  name: string;
  provider_id: string;
  base_url: string;
  protocol: "openai" | "anthropic";
  configured: boolean;
  readonly?: boolean;
  models: RemoteModels;
}
export type ModelRole =
  "main" | "reading" | "verification" | "vision" | "chat" | "conversation";
export interface ModelAssignment {
  account_id: string;
  model: string;
  vision_confirmed?: boolean;
}
export type ModelRouting = Record<ModelRole, ModelAssignment | null>;
export interface ModelConnectionsState {
  providers: ModelProvider[];
  accounts: ModelAccount[];
  routing: ModelRouting;
  roles: { id: ModelRole; label: string; description: string }[];
}
export interface AccountInput {
  provider_id: string;
  name: string;
  api_key?: string;
  base_url?: string;
  protocol?: "openai" | "anthropic";
}
export function getConnections(): Promise<ModelConnectionsState> {
  return request("/api/connections");
}
export function saveModelAccount(
  body: AccountInput,
  id?: string,
): Promise<{ account: ModelAccount }> {
  return request(`/api/connections/accounts${id ? `/${encodeURIComponent(id)}` : ""}`, {
    method: id ? "PATCH" : "POST",
    json: body,
  });
}
export async function deleteModelAccount(id: string): Promise<void> {
  await request(`/api/connections/accounts/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
export function refreshAccountModels(id: string): Promise<RemoteModels> {
  return request(`/api/connections/accounts/${encodeURIComponent(id)}/models/refresh`, {
    method: "POST",
    json: {},
  });
}
export function saveModelRouting(
  routing: ModelRouting,
): Promise<{ routing: ModelRouting }> {
  return request("/api/connections/routing", { method: "PUT", json: { routing } });
}

/* ---------- reading translation: made only when the reader asks ---------- */

export interface Translation {
  text: string;
  model?: string;
}
export interface TranslateResult {
  items: Record<string, Translation>;
  remaining: string[];
  warnings: string[];
}
export async function listTranslations(
  paperId: string,
): Promise<Record<string, Translation>> {
  return (
    await request<{ items: Record<string, Translation> }>(
      `${paperPath(paperId)}/translations`,
    )
  ).items;
}
export function translateItems(
  paperId: string,
  itemIds: string[],
): Promise<TranslateResult> {
  return request<TranslateResult>(`${paperPath(paperId)}/translate`, {
    method: "POST",
    json: { item_ids: itemIds },
  });
}
