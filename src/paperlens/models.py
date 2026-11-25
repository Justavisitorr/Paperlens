"""Versioned records shared by parsing, reasoning and presentation.

Coordinates use the displayed page, a top-left origin and normalized 0–1 units.
Physical page numbers are one-based; printed labels are separate. Records never
contain credentials or absolute input paths, so they can be exported safely.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .chart_models import ChartStructure

SCHEMA_VERSION = "1.0"
NodeKind = Literal[
    "theme",
    "question",
    "method",
    "evidence",
    "contribution",
    "conclusion",
    "limitation",
    "concept",
    "background",
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Box(Record):
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def check_order(self) -> Box:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("A bounding box must have ordered corners")
        return self


class Page(Record):
    number: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    label: str = ""
    rotation: int = 0
    status: Literal["ok", "blank", "image_only", "failed"] = "ok"
    text_chars: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class FormulaRecord(Record):
    latex: str | None = None
    mathml: str | None = None
    source: Literal["pdf_text", "docling", "vision"] = "pdf_text"
    status: Literal["unresolved", "renderable", "invalid"] = "unresolved"
    verified: bool = False
    warnings: list[str] = Field(default_factory=list)


class TableCell(Record):
    text: str
    row_start: int
    row_end: int
    col_start: int
    col_end: int
    bbox: Box | None = None
    column_header: bool = False


class TableGrid(Record):
    rows: int
    cols: int
    cells: list[TableCell] = Field(default_factory=list)


class OCRLine(Record):
    text: str
    bbox: Box
    confidence: float | None = None


class Block(Record):
    id: str
    page: int = Field(ge=1)
    text: str
    raw_text: str
    bbox: Box
    section: str = ""
    kind: Literal[
        "text",
        "heading",
        "table",
        "caption",
        "reference",
        "formula",
        "picture",
        "algorithm",
        "header",
        "footer",
    ] = "text"
    order: int = Field(ge=0)
    text_warnings: list[str] = Field(default_factory=list)
    formula: FormulaRecord | None = None
    docling_label: str = ""
    heading_level: int | None = None
    parent_ref: str = ""
    source_ref: str = ""
    table: TableGrid | None = None
    caption_ids: list[str] = Field(default_factory=list)
    text_source: Literal["pdf_text", "ocr"] = "pdf_text"
    ocr_lines: list[OCRLine] = Field(default_factory=list)
    # A reviewed continuation can span several original physical pages. `page`
    # is its starting page, not an assertion that every quoted word is there.
    source_pages: list[int] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)


class PaperDocument(Record):
    schema_version: str = SCHEMA_VERSION
    title: str
    source_name: str
    sha256: str
    parser: str
    pages: list[Page]
    blocks: list[Block]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_indices(self) -> PaperDocument:
        numbers = [p.number for p in self.pages]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("All physical pages must be represented in order")
        ids = [b.id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("Block IDs must be unique")
        if any(b.page not in numbers for b in self.blocks):
            raise ValueError("A block references an unknown physical page")
        if any(b.source_pages and (
            b.page not in b.source_pages or not set(b.source_pages) <= set(numbers)
        ) for b in self.blocks):
            raise ValueError("A block's source pages must belong to the original PDF")
        return self


class Evidence(Record):
    id: str
    block_id: str
    page: int = Field(ge=1)
    page_label: str = ""
    section: str = ""
    quote: str
    bbox: Box
    match: Literal["exact", "normalized", "approximate", "block"] = "block"
    source_pages: list[int] = Field(default_factory=list)


class PaperNode(Record):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    kind: NodeKind
    summary: str
    explanation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    origin: Literal["author", "inference", "reader_guide"] = "author"
    support: Literal["supported", "partial", "unverified"] = "unverified"
    parent_id: str | None = None


class PaperEdge(Record):
    id: str
    source: str
    target: str
    label: str = ""
    relation: Literal[
        "motivates", "uses", "supports", "derives", "limits", "explains", "contains"
    ] = "contains"


class GlossaryTerm(Record):
    term: str
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)
    support: Literal["supported", "partial", "unverified"] = "unverified"
    review_notes: list[str] = Field(default_factory=list)


class PaperAnalysis(Record):
    title: str
    summary: str
    paper_type: Literal["empirical", "theoretical", "review", "other"] = "other"
    research_question: str
    reading_guide: list[str] = Field(default_factory=list)
    nodes: list[PaperNode]
    edges: list[PaperEdge]
    glossary: list[GlossaryTerm] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_graph(self) -> PaperAnalysis:
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Node IDs must be unique")
        if len({e.id for e in self.edges}) != len(self.edges):
            raise ValueError("Edge IDs must be unique")
        known = set(ids)
        if any(e.source not in known or e.target not in known for e in self.edges):
            raise ValueError("Edge endpoints must exist")
        parents = {n.id: n.parent_id for n in self.nodes}
        for node in self.nodes:
            if node.parent_id is not None and node.parent_id not in known:
                raise ValueError("Parent must exist")
            seen: set[str] = set()
            current: str | None = node.id
            while current is not None:
                if current in seen:
                    raise ValueError("Hierarchy cannot contain a cycle")
                seen.add(current)
                current = parents.get(current)
        return self


class GraphView(Record):
    id: str
    title: str
    description: str
    node_ids: list[str]
    edge_ids: list[str]
    mermaid: str
    validated: bool = False


class PagePreview(Record):
    page: int = Field(ge=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    data_url: str


class Usage(Record):
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None


class StageFailure(Record):
    stage: str
    label: str
    model: str = ""
    pages: list[int] = Field(default_factory=list)
    batches: int = 1
    items: int = 0
    code: str = "processing_error"
    message: str
    guidance: str
    retryable: bool = True
    http_status: int | None = None
    parameter: str = ""


class RunInfo(Record):
    id: str
    created_at: str
    model: str
    parser: str
    status: Literal["complete", "partial"]
    timings: dict[str, float] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    prompt_version: str = "1.0"
    provider_base_url: str | None = None
    usage_scope: Literal["current_invocation"] | None = None
    execution_mode: Literal["api", "codex_session"] = "api"
    derived_from_run_id: str | None = None
    processing_profile: str = "standard"
    processing_limits: dict[str, int | bool] = Field(default_factory=dict)
    processing_mode: Literal["analysis", "review"] = "analysis"
    usage_by_model: dict[str, Usage] = Field(default_factory=dict)
    usage_by_provider: list[dict] = Field(default_factory=list)
    model_routing: dict[str, dict] = Field(default_factory=dict)
    cache_hits: dict[str, int] = Field(default_factory=dict)
    failures: list[StageFailure] = Field(default_factory=list)


class Quality(Record):
    total_pages: int
    parsed_pages: int
    text_chars: int
    grounded_nodes: int
    total_nodes: int
    checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VisualEvidence(Record):
    id: str = Field(pattern=r"^V[0-9]+$")
    page: int = Field(ge=1)
    label: str = ""
    kind: Literal["figure", "table", "formula", "page"] = "figure"
    description: str = ""
    source_block_ids: list[str] = Field(default_factory=list)
    bbox: Box | None = None
    status: Literal["analyzed", "unavailable", "unreadable"] = "unavailable"
    warnings: list[str] = Field(default_factory=list)
    chart: ChartStructure | None = None


ExperimentConditionKind = Literal[
    "dataset",
    "split",
    "sample_size",
    "baseline",
    "hyperparameters",
    "hardware",
    "evaluation_protocol",
    "other",
]


class ExperimentCondition(Record):
    kind: ExperimentConditionKind
    value: str
    evidence_ids: list[str] = Field(default_factory=list)


class QuantitativeResult(Record):
    id: str
    metric: str
    method: str = ""
    dataset: str = ""
    value: float | None = Field(default=None, allow_inf_nan=False)
    value_text: str = ""
    unit: str = ""
    condition: str = ""
    conditions: list[ExperimentCondition] = Field(default_factory=list)
    # These are fields not extracted from the available local evidence, not a
    # claim that the original paper omits them or that all apply to every result.
    missing_conditions: list[ExperimentConditionKind] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    page: int = Field(ge=1)
    figure_label: str = ""
    source: Literal["text", "table_text", "figure_label", "visual_estimate", "figure_only"]
    precision: Literal["reported", "approximate", "unavailable"]
    visual_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    panel: str = ""
    series: str = ""
    x: float | str | None = None
    axis: Literal["y", "y_right"] = "y"
    visual_bbox: Box | None = None
    value_bbox: list[float] | None = None
    readout_uncertainty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error_bar: list[float] | None = None
    # A matching numeric token is corroboration only, never proof of association.
    label_matched: bool = False

    @model_validator(mode="after")
    def check_precision(self) -> QuantitativeResult:
        if self.source == "figure_only" or self.precision == "unavailable":
            if self.value is not None or self.value_text:
                raise ValueError("Unavailable chart values must not contain a numeric claim")
        if self.source == "visual_estimate" and self.precision != "approximate":
            raise ValueError("A visual estimate cannot be an exact reported value")
        return self


class ReadingPart(Record):
    # UTF-16 offsets address the unmodified block text used by existing notes.
    block_id: str
    start: int = 0
    end: int
    join: str = ""
    bbox: Box | None = None


class TranscriptionSegment(Record):
    text: str = ""
    formula: FormulaRecord | None = None


class ReadingTranscription(Record):
    segments: list[TranscriptionSegment]
    source: Literal["vision", "docling"]
    verified: bool = False
    warnings: list[str] = Field(default_factory=list)


class ReadingContentSpan(Record):
    start: int
    end: int
    source: ReadingPart


class FigureCrop(Record):
    version: str
    source: Literal["pdf_objects", "page"]
    box: Box


class ReadingItem(Record):
    id: str
    kind: Literal[
        "paragraph", "heading", "reference", "figure", "table", "formula", "algorithm", "code"
    ]
    code_language: str = ""
    parts: list[ReadingPart] = Field(default_factory=list)
    level: int = 1
    label: str = ""
    href: str = ""
    asset: str = ""
    asset_crop: FigureCrop | None = None
    page: int = 1
    bbox: Box | None = None
    result_ids: list[str] = Field(default_factory=list)
    visual_ids: list[str] = Field(default_factory=list)
    transcription: ReadingTranscription | None = None
    # A semantic reading edition; source parts remain immutable evidence anchors.
    content: str | None = None
    continuation: bool = False
    reference_number: str = ""
    source_pages: list[int] = Field(default_factory=list)
    table: TableGrid | None = None
    content_spans: list[ReadingContentSpan] = Field(default_factory=list)


class ReadingDocument(Record):
    version: str = "2"
    source_version: str
    items: list[ReadingItem]
    references: list[ReadingItem] = Field(default_factory=list)
    information: dict[str, list[str]] = Field(default_factory=dict)
    information_parts: dict[str, list[ReadingPart]] = Field(default_factory=dict)
    information_items: dict[str, list[ReadingItem]] = Field(default_factory=dict)
    structured_pages: list[int] = Field(default_factory=list)
    hidden_ids: list[str] = Field(default_factory=list)
    # Source-PDF TOC spans hidden only from the reading/translation projection.
    excluded_toc_parts: list[ReadingPart] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SourcePDF(Record):
    data_url: str
    sha256: str


class ReadingTranslation(Record):
    text: str
    model: str = ""
    source: str
    source_complete: bool = True
    created_at: str = ""


class DashboardReport(Record):
    translations: dict[str, ReadingTranslation] = Field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    document: PaperDocument
    analysis: PaperAnalysis
    evidence: list[Evidence]
    graphs: list[GraphView]
    previews: list[PagePreview]
    quality: Quality
    run: RunInfo
    visuals: list[VisualEvidence] = Field(default_factory=list)
    quantitative_results: list[QuantitativeResult] = Field(default_factory=list)
    reading: ReadingDocument | None = None
    source_pdf: SourcePDF | None = None

    @model_validator(mode="after")
    def check_references(self) -> DashboardReport:
        blocks = {b.id: b for b in self.document.blocks}
        evidence = {e.id: e for e in self.evidence}
        nodes = {n.id for n in self.analysis.nodes}
        edges = {e.id: e for e in self.analysis.edges}
        if len(evidence) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique")
        for item in self.evidence:
            block = blocks.get(item.block_id)
            # Native PDF reading deliberately publishes page-only evidence:
            # estimated paragraph coordinates must not imply verified locations.
            page_only = (block is not None
                         and self.document.parser in {"openai-pdf", "model-page-images"}
                         and block.source_ref == "ai-direct:unverified-position"
                         and item.bbox == Box(x0=0, y0=0, x1=1, y1=1))
            if block is None or item.page != block.page or (item.bbox != block.bbox and not page_only):
                raise ValueError("Evidence must resolve to its original page and block box")
            if set(item.source_pages or [item.page]) != set(block.source_pages or [block.page]):
                raise ValueError("Evidence source pages must match its source block")
        for node in self.analysis.nodes:
            if not set(node.evidence_ids) <= evidence.keys():
                raise ValueError("Node evidence IDs must exist")
            if node.support == "supported" and not node.evidence_ids:
                raise ValueError("A supported node must have matched evidence")
        for term in self.analysis.glossary:
            if not set(term.evidence_ids) <= evidence.keys():
                raise ValueError("Glossary evidence IDs must exist")
            if term.support == "supported" and not term.evidence_ids:
                raise ValueError("A supported term must have matched evidence")
        visual_ids = {v.id for v in self.visuals}
        if len(visual_ids) != len(self.visuals):
            raise ValueError("Visual IDs must be unique")
        for visual in self.visuals:
            if (
                visual.page > len(self.document.pages)
                or not set(visual.source_block_ids) <= blocks.keys()
            ):
                raise ValueError("Visual sources must exist in the document")
        for result in self.quantitative_results:
            if (
                result.page > len(self.document.pages)
                or not set(result.evidence_ids) <= evidence.keys()
            ):
                raise ValueError("Quantitative result references must exist")
            if not set(result.node_ids) <= nodes or any(
                not set(condition.evidence_ids) <= evidence.keys()
                for condition in result.conditions
            ):
                raise ValueError("Quantitative condition and node references must exist")
            if result.visual_id is not None and result.visual_id not in visual_ids:
                raise ValueError("Quantitative visual reference must exist")
        if len({g.id for g in self.graphs}) != len(self.graphs):
            raise ValueError("Graph view IDs must be unique")
        for graph in self.graphs:
            selected = set(graph.node_ids)
            if not selected <= nodes or not set(graph.edge_ids) <= edges.keys():
                raise ValueError("Graph members must exist")
            if any(
                edges[e].source not in selected or edges[e].target not in selected
                for e in graph.edge_ids
            ):
                raise ValueError("View edge endpoints must be visible")
        if any(p.page > len(self.document.pages) for p in self.previews):
            raise ValueError("Preview page must exist")
        if self.quality.total_nodes != len(nodes):
            raise ValueError("Quality node denominator must match the report")
        if self.quality.total_pages != len(self.document.pages):
            raise ValueError("Quality page denominator must match the report")
        return self
