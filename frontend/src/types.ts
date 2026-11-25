/** Presentation mirror of src/paperlens/models.py. No credentials belong here. */
export type NodeKind =
  | "theme"
  | "question"
  | "method"
  | "evidence"
  | "contribution"
  | "conclusion"
  | "limitation"
  | "concept"
  | "background";
export interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}
export interface PaperNode {
  id: string;
  title: string;
  kind: NodeKind;
  summary: string;
  explanation: string;
  evidence_ids: string[];
  origin: "author" | "inference" | "reader_guide";
  support: "supported" | "partial" | "unverified";
  parent_id: string | null;
}
export interface PaperEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  relation: string;
}
export interface Evidence {
  id: string;
  block_id: string;
  page: number;
  source_pages?: number[];
  page_label: string;
  section: string;
  quote: string;
  bbox: Box;
  match: "exact" | "normalized" | "approximate" | "block";
}
export interface GraphView {
  id: string;
  title: string;
  description: string;
  node_ids: string[];
  edge_ids: string[];
  mermaid: string;
  validated: boolean;
}
export interface PagePreview {
  page: number;
  width: number;
  height: number;
  data_url: string;
}
export interface Block {
  id: string;
  page: number;
  source_pages?: number[];
  source_block_ids?: string[];
  text: string;
  raw_text: string;
  bbox: Box;
  section: string;
  kind: string;
  order: number;
  /** Optional, added by newer parsers (Codex T10). */
  text_warnings?: string[];
  formula?: Formula | null;
  docling_label?: string;
  heading_level?: number | null;
  table?: {
    rows: number;
    cols: number;
    cells: {
      text: string;
      row_start: number;
      row_end: number;
      col_start: number;
      col_end: number;
      column_header: boolean;
    }[];
  } | null;
  text_source?: "pdf_text" | "ocr";
  ocr_lines?: { text: string; bbox: Box; confidence?: number | null }[];
}
export interface Formula {
  latex: string;
  mathml: string;
  source: "pdf_text" | "docling" | "vision";
  status: "unresolved" | "renderable" | "invalid";
  verified: boolean;
  warnings: string[];
}
export interface GlossaryTerm {
  term: string;
  explanation: string;
  evidence_ids: string[];
  support?: "supported" | "partial" | "unverified";
  review_notes?: string[];
}
export interface ChartStructure {
  chart_type:
    | "unknown"
    | "table"
    | "bar"
    | "line"
    | "scatter"
    | "pie"
    | "heatmap"
    | "diagram"
    | "other";
  axes: {
    id: "x" | "y" | "y_right";
    panel?: string;
    label: string;
    unit: string;
    scale: "linear" | "log" | "category" | "unknown";
    ticks: number[];
  }[];
  series: string[];
  panels: string[];
}
export interface VisualEvidence {
  id: string;
  page: number;
  label: string;
  kind: "figure" | "table" | "formula" | "page";
  description: string;
  source_block_ids: string[];
  bbox?: Box | null;
  status: "analyzed" | "unavailable" | "unreadable";
  warnings: string[];
  chart?: ChartStructure | null;
}
export interface QuantitativeResult {
  id: string;
  metric: string;
  method: string;
  dataset: string;
  value: number | null;
  value_text: string;
  unit: string;
  condition: string;
  page: number;
  figure_label: string;
  source: "text" | "table_text" | "figure_label" | "visual_estimate" | "figure_only";
  precision: "reported" | "approximate" | "unavailable";
  visual_id?: string | null;
  evidence_ids: string[];
  notes: string;
  panel?: string;
  series?: string;
  x?: number | string | null;
  axis?: "y" | "y_right";
  visual_bbox?: Box | null;
  value_bbox?: number[] | null;
  readout_uncertainty?: number | null;
  error_bar?: number[] | null;
  label_matched?: boolean;
  conditions?: {
    kind: ExperimentConditionKind;
    value: string;
    evidence_ids: string[];
  }[];
  missing_conditions?: ExperimentConditionKind[];
  node_ids?: string[];
}
export type ExperimentConditionKind =
  | "dataset"
  | "split"
  | "sample_size"
  | "baseline"
  | "hyperparameters"
  | "hardware"
  | "evaluation_protocol"
  | "other";
export interface ReadingPart {
  block_id: string;
  start: number;
  end: number;
  join: string;
  bbox?: Box | null;
}
export interface ReadingTranscription {
  segments: { text: string; formula?: Formula | null }[];
  source: "vision" | "docling";
  verified: boolean;
  warnings: string[];
}
/** Exact UTF-16 runs in reading content that retain original source coordinates. */
export interface ReadingContentSpan {
  start: number;
  end: number;
  source: ReadingPart;
}
export interface ReadingItem {
  id: string;
  kind:
    | "paragraph"
    | "heading"
    | "reference"
    | "figure"
    | "table"
    | "formula"
    | "algorithm"
    | "code";
  code_language?: string;
  parts: ReadingPart[];
  level: number;
  label: string;
  href?: string;
  asset: string;
  asset_crop?: { version: string; source: "pdf_objects" | "page"; box: Box } | null;
  page: number;
  bbox?: Box | null;
  result_ids: string[];
  visual_ids: string[];
  transcription?: ReadingTranscription | null;
  /** Faithful reading projection; parts still address immutable source text. */
  content?: string | null;
  content_spans?: ReadingContentSpan[];
  continuation?: boolean;
  reference_number?: string;
  source_pages?: number[];
  table?: Block["table"];
}
export interface ReadingDocument {
  version: string;
  source_version: string;
  items: ReadingItem[];
  references: ReadingItem[];
  information: Record<string, string[]>;
  information_parts?: Record<string, ReadingPart[]>;
  information_items?: Record<string, ReadingItem[]>;
  structured_pages?: number[];
  hidden_ids: string[];
  warnings: string[];
}
export interface DashboardReport {
  schema_version: string;
  reading?: ReadingDocument | null;
  source_pdf?: { data_url: string; sha256: string; byte_size?: number } | null;
  document: {
    title: string;
    source_name: string;
    sha256: string;
    parser: string;
    pages: {
      number: number;
      width: number;
      height: number;
      label: string;
      rotation?: number;
      status: string;
      text_chars: number;
      warnings: string[];
    }[];
    blocks: Block[];
    warnings: string[];
  };
  analysis: {
    title: string;
    summary: string;
    paper_type: "empirical" | "theoretical" | "review" | "other";
    research_question: string;
    reading_guide: string[];
    nodes: PaperNode[];
    edges: PaperEdge[];
    glossary: GlossaryTerm[];
    warnings: string[];
  };
  evidence: Evidence[];
  graphs: GraphView[];
  previews: PagePreview[];
  visuals?: VisualEvidence[];
  quantitative_results?: QuantitativeResult[];
  translations?: Record<string, { text: string; model: string }>;
  quality: {
    total_pages: number;
    parsed_pages: number;
    text_chars: number;
    grounded_nodes: number;
    total_nodes: number;
    checks: string[];
    warnings: string[];
  };
  run: {
    id: string;
    created_at: string;
    model: string;
    parser: string;
    execution_mode?: "api" | "codex_session";
    status: "complete" | "partial";
    failures?: StageFailure[];
    timings: Record<string, number>;
    usage: {
      requests: number;
      prompt_tokens: number;
      completion_tokens: number;
      prompt_cache_hit_tokens?: number | null;
      prompt_cache_miss_tokens?: number | null;
    };
    provider_base_url?: string;
    prompt_version: string;
    processing_profile?: string;
    processing_limits?: Record<string, number | boolean>;
  };
}
export interface StageFailure {
  stage: string;
  label: string;
  model: string;
  pages: number[];
  batches: number;
  items: number;
  code: string;
  message: string;
  guidance: string;
  retryable: boolean;
  http_status?: number | null;
  parameter?: string;
}
export interface GraphValidation {
  id: string;
  valid: boolean;
  error?: string;
  svg?: string;
}
declare global {
  interface Window {
    paperLensWorkspaceReady?: boolean;
    paperLensWorkspaceId?: string;
    paperLensCloudReady?: Promise<void>;
    paperLensAccount?: { user: { id: string; name: string }; workspace: string; csrf: string };
    paperLensSync?: { snapshot: () => { status: string; pending: number }; flush: () => Promise<void> };
    __paperlensReady: boolean;
    __paperlensReaderReady?: boolean;
    __paperlensErrors: string[];
    __paperlensValidate: () => Promise<{
      graphs: GraphValidation[];
      errors: string[];
    }>;
  }
}
