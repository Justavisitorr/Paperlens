"""Generate bounded, grounded margin notes using the reading assistant."""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import copy_context
from pathlib import Path
from threading import Event, RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .analysis.evidence import normalize_quote
from .config import Settings
from .control import (
    ProcessingPaused,
    capture_concurrency_limit,
    checkpoint,
    processing_control,
    processing_stage,
)
from .conversation import SYSTEM, ConversationService, _terms
from .errors import InputBudgetExceeded, PaperLensError, ProviderError, ValidationError
from .input_budget import approved_limit, temporary_input_budget
from .models import DashboardReport, ReadingItem, Usage
from .notes import NoteInput, NoteService, item_content, reading_items
from .prompt_content import prompt_item_content
from .provider import JsonProvider, RequestBudget, sum_usage
from .reader import _utf16_index
from .reading_parallel import coordinated_map, note_concurrency
from .storage import fingerprint

TYPES = {
    "summary": ("要点摘要", "概括本段的具体主张、作用和限定条件"),
    "explain": ("概念与公式解释", "解释关键概念、符号、公式或表格的读法，保留适用条件"),
    "connections": ("前后联系", "说明本段与提供的上下文的具体联系；未提供的内容不能编造"),
    "questions": ("值得追问的问题", "给出一两个紧贴本段、值得读者追问的问题，明确是建议"),
    "check": (
        "疑点与核对提示",
        "提示需要核对的条件或识别疑点，依据不足时说明；不把转录疑点断言为作者错误",
    ),
}

_VISUAL_LABEL = re.compile(
    r"(?<![A-Za-z])(?P<kind>fig(?:ure)?\.?|tab(?:le)?\.?|图|表)\s*"
    r"(?P<number>S?\d+(?:\.\d+)*)(?:[A-Za-z])?(?![A-Za-z0-9])", re.I,
)


def _visual_key(match):
    kind = match["kind"].casefold()
    return ("figure" if kind.startswith("fig") or kind == "图" else "table",
            match["number"].casefold())


def _referenced_captions(report, text):
    """Resolve only unambiguous original captions, never a mention's page."""
    wanted = {_visual_key(match) for match in _VISUAL_LABEL.finditer(text)}
    candidates = {}
    for block in report.document.blocks:
        match = _VISUAL_LABEL.match(block.text.lstrip())
        if match and block.kind == "caption" and _visual_key(match) in wanted:
            candidates.setdefault(_visual_key(match), []).append(block)
    return [blocks[0] for blocks in candidates.values() if len(blocks) == 1]


def _validate_visual_pages(report, answer):
    # Check explicit physical locations only; a page may legitimately discuss a
    # figure printed elsewhere, and printed page labels are a separate system.
    pages = {_visual_key(_VISUAL_LABEL.match(b.text.lstrip())): b.page
             for b in _referenced_captions(report, answer)}
    physical_page = r"(?:PDF\s*(?:物理)?|物理)\s*第?\s*(\d+)\s*页"
    for match in _VISUAL_LABEL.finditer(answer):
        expected = pages.get(_visual_key(match))
        before, after = answer[:match.start()], answer[match.end():]
        location = re.search(physical_page + r"\s*(?:的|上的)?\s*$", before, re.I)
        if location is None:
            location = re.match(r"\s*(?:位于|在|见于)\s*" + physical_page, after, re.I)
        if expected is not None and location and int(location[1]) != expected:
            raise ValidationError("笔记中的图表物理页与原文图注不一致，未保存。")


def _prune_note_context(report, paper_id, prepared):
    """Optional transcriptions must have their complete source intervals supplied."""
    body = json.loads(prepared[4])
    supplied = prepared[3].get(paper_id, {})
    blocks = {block.id: block for block in report.document.blocks}
    items = reading_items(report)

    def covered(entry):
        item = items.get(entry.get("item_id"))
        if item is None or not item.parts:
            return False
        for part in item.parts:
            block = blocks.get(part.block_id)
            if block is None or part.block_id not in supplied:
                return False
            try:
                fragment = block.text[_utf16_index(block.text, part.start):
                                      _utf16_index(block.text, part.end)]
            except ValidationError:
                return False
            source = normalize_quote(fragment)
            if not source or source not in normalize_quote(supplied[part.block_id]):
                return False
        return True

    for selected in body.get("reading_context", []):
        selected["neighbor_paragraphs"] = [
            entry for entry in selected.get("neighbor_paragraphs", []) if covered(entry)]
    if "scope_reading_context" in body:
        body["scope_reading_context"] = [
            entry for entry in body["scope_reading_context"] if covered(entry)]
    return (*prepared[:4], json.dumps(body, ensure_ascii=False, separators=(",", ":")), *prepared[5:])


class _NotesStopped(PaperLensError):
    """A sibling failed before this request was admitted; no extra paid attempt."""


class GenerationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["section", "paper"] = "paper"
    anchor_id: str | None = Field(default=None, max_length=200)
    item_id: str | None = Field(default=None, max_length=200)
    source_version: str | None = Field(default=None, max_length=200)
    dry_run: bool = False


def section_items(items: list[ReadingItem], anchor_id: str) -> list[ReadingItem]:
    position = next((i for i, item in enumerate(items) if item.id == anchor_id), None)
    if position is None:
        raise ValidationError("找不到当前阅读段落，请刷新后重试。")
    start = next((i for i in range(position, -1, -1) if items[i].kind == "heading"), 0)
    level = items[start].level if items[start].kind == "heading" else 0
    end = next(
        (
            i
            for i in range(position + 1, len(items))
            if items[i].kind == "heading" and (not level or items[i].level <= level)
        ),
        len(items),
    )
    return items[start:end]


class AutoNoteService:
    def __init__(self, storage_root: Path, notes: NoteService | None = None) -> None:
        self.notes = notes or NoteService(storage_root)
        self.conversations = ConversationService(storage_root)
        # Repeated button presses cannot generate two paid requests for one note.
        self._lock = RLock()
        self._paper_locks: dict[str, RLock] = {}

    def _paper_lock(self, paper_id: str):
        with self._lock:
            return self._paper_locks.setdefault(paper_id, RLock())

    @staticmethod
    def types(settings: Settings) -> list[str]:
        return [name for name in TYPES if getattr(settings, f"paperlens_auto_notes_{name}")]

    def _identity(self, report, item, settings):
        data = [
            "auto-notes-v1",
            report.reading.source_version,
            item.id,
            item_content(report, item),
            self.types(settings),
            settings.paperlens_auto_notes_context,
        ]
        return hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:32]

    def _resume_id(self, report, settings):
        routed = settings.for_role("chat")
        return fingerprint({
            "kind": "pipeline-auto-notes-v4", "document": report.document.sha256,
            "source_version": report.reading.source_version,
            "reading": report.reading.model_dump(mode="json"),
            "model": routed.llm_model, "provider": routed.llm_base_url,
            "account": routed.llm_account_id,
            "types": self.types(settings), "context": settings.paperlens_auto_notes_context,
            "max_paragraphs": settings.paperlens_auto_notes_max_paragraphs,
            "analysis": report.analysis.model_dump(mode="json"),
        })[:32]

    @staticmethod
    def _note_payload(note, paper_id):
        def rebind(value):
            if isinstance(value, list):
                return [rebind(item) for item in value]
            if isinstance(value, dict):
                return {key: paper_id if key == "paper_id" else rebind(item)
                        for key, item in value.items()}
            return value

        return rebind({key: note[key] for key in NoteInput.model_fields})

    @staticmethod
    def _substantive(report, item) -> bool:
        """Spend note requests on readable body content, not extraction debris."""
        if item.kind != "paragraph":
            return True
        text = item_content(report, item)["content"].strip()
        compact = re.sub(r"\s+", " ", text)
        if re.search(
            r"^(?:isbn\b|issn\b|doi\s*:|(?:\[?\d{1,3}\]?\s*)?(?:https?://|www\.)|"
            r"all rights reserved\b|"
            r"copyright\b|©|published (?:by|online)\b|printed in\b|"
            r"(?:received|accepted|revised)\s*[:\d]|电子邮箱\s*[:：]|版权|出版信息)",
            compact, re.I,
        ):
            return False
        if re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", compact):
            return False
        latin = re.findall(r"[A-Za-z]+", compact)
        han = len(re.findall(r"[\u3400-\u9fff]", compact))
        return han >= 8 or (len(latin) >= 4 and sum(map(len, latin)) >= 20)

    def plan(
        self, paper_id: str, report: DashboardReport, settings: Settings, payload: dict
    ) -> dict:
        request = GenerationInput.model_validate(payload)
        if report.reading is None:
            raise ValidationError("论文尚无可阅读正文，请先完成处理。")
        if request.source_version and request.source_version != report.reading.source_version:
            raise ValidationError("阅读正文版本已经变化，请刷新后重新生成。")
        items = report.reading.items
        if request.scope == "section":
            if not request.anchor_id:
                raise ValidationError("请选择当前小节中的段落。")
            items = section_items(items, request.anchor_id)
        blocks = {b.id for b in report.document.blocks if b.text.strip()}
        eligible = [
            i
            for i in items
            if i.kind in {"paragraph", "formula", "table"}
            and any(p.block_id in blocks for p in i.parts)
            and (item_content(report, i)["content"].strip() or i.table)
            and self._substantive(report, i)
        ]
        # Spread the bounded selection across the requested range, then keep reading order.
        limit = min(settings.paperlens_auto_notes_max_paragraphs, settings.llm_max_requests)
        chosen = (
            eligible
            if len(eligible) <= limit
            else [eligible[(i * (len(eligible) - 1)) // max(1, limit - 1)] for i in range(limit)]
        )
        existing = {n["id"] for n in self.notes.list(paper_id, report)}
        types = self.types(settings)
        skipped = sum(self._identity(report, item, settings) in existing for item in chosen)
        chosen = (
            [i for i in chosen if self._identity(report, i, settings) not in existing]
            if types
            else []
        )
        if request.item_id:
            if request.item_id not in {i.id for i in eligible}:
                raise ValidationError("该对象不在本次笔记生成范围内。")
            # A previewed object remains valid as earlier planned notes are saved.
            chosen = [i for i in chosen if i.id == request.item_id]
        return {
            "eligible": len(eligible),
            "planned": len(chosen),
            "skipped": skipped,
            "max_paragraphs": settings.paperlens_auto_notes_max_paragraphs,
            "types": types,
            "context_scope": settings.paperlens_auto_notes_context,
            "max_calls": min(len(chosen), settings.llm_max_requests),
            "source_version": report.reading.source_version,
            "items": [
                {"item_id": i.id, "page": i.page, "label": i.label, "kind": i.kind} for i in chosen
            ],
        }

    def _prepare(self, paper_id, report, settings, item):
        items = report.reading.items
        scope = settings.paperlens_auto_notes_context
        framing = []
        if scope == "section":
            context = section_items(items, item.id)
        elif scope == "nearby":
            index = next(i for i, candidate in enumerate(items) if candidate.id == item.id)
            context = [item]
            for stream in (reversed(items[:index]), iter(items[index + 1 :])):
                neighbour = next((i for i in stream if i.kind == "paragraph"), None)
                if neighbour:
                    context.append(neighbour)
            section = section_items(items, item.id)
            framing = [i for i in section[:1] if i.kind == "heading"]
            introduction = next((i for i in section if i.kind == "paragraph"
                                 and self._substantive(report, i)), None)
            if introduction:
                framing.append(introduction)
            selected = {i.id for i in [*context, *framing]}
            context = [i for i in items if i.id in selected]
        else:
            context = items
        captions = _referenced_captions(report, item_content(report, item)["content"])[:2]
        source_ids = {p.block_id for i in context for p in i.parts}
        source_ids.update(b.id for b in captions)
        scoped = report.model_copy(
            update={
                "document": report.document.model_copy(
                    update={"blocks": [b for b in report.document.blocks if b.id in source_ids]}
                ),
                "reading": report.reading.model_copy(update={"items": context}),
                "analysis": report.analysis
                if scope == "paper"
                else report.analysis.model_copy(update={"summary": "", "research_question": ""}),
            }
        )
        terms = _terms(item_content(report, item)["content"])
        ranked = sorted(
            (i for i in context if i.id != item.id),
            key=lambda i: -len(terms & _terms(item_content(report, i)["content"])),
        )
        extra, remaining = [], settings.llm_max_input_chars // 4
        for other in ranked:
            entry = {
                "item_id": other.id,
                **prompt_item_content(report, other),
                "transcription_verified": False,
            }
            length = len(json.dumps(entry, ensure_ascii=False))
            if length <= remaining:
                extra.append(entry)
                remaining -= length
        directions = "；".join(f"## {TYPES[t][0]}：{TYPES[t][1]}" for t in self.types(settings))
        # Reuse the existing exact-selection path to keep framing/captions in the
        # source budget. This does not certify the reading edition as evidence.
        priority_ids = {b.id for b in captions}
        priority_ids.update(p.block_id for i in framing for p in i.parts[:1])
        priority_ids.difference_update(p.block_id for p in item.parts)
        passages = []
        for block in scoped.document.blocks:
            if block.id in priority_ids and block.text.strip():
                text = block.text[:320]
                passages.append({"paper_id": paper_id, "block_id": block.id,
                                 "start": 0, "end": len(text.encode("utf-16-le")) // 2,
                                 "text": text})
        request = {
            "papers": [paper_id],
            "passages": passages[:6],
            "question": "先概括 reading_context[0] 所选对象本身；扩展须说明与它的关系，不能让邻段替代笔记主体。"
            "为所选对象写简洁页边笔记。仅用所选类型的 Markdown 小标题，每类一到两句；"
            "无实质价值的追问、疑点、联系可省略，不能为填栏目制造质疑。"
            "按原文区分任务、能力/维度、方法、指标与数据集，不把不同层级概念当同类。"
            "论文事实须有引文；概念解释、建议须与作者结论区分。局部未提及不代表整篇未说明，"
            "不要因转录碎片反复质疑作者。quote 须逐字复制 blocks 对应 block_id 的 text，"
            "保留原语言/符号，不用转录、译文或概括；优先摘所选对象中支撑当前句子的短原文。"
            "用[n]定位即可；必须写图表物理页时取原文图注的 blocks.page（非印刷页），"
            "不从提及图表的段落猜页码，无图注则省略页码。不能核对的事实省略：" + directions,
            "reading_items": [
                {
                    "paper_id": paper_id,
                    "item_id": item.id,
                    "source_version": report.reading.source_version,
                }
            ],
        }
        target_sources = {p.block_id for p in item.parts}
        for optional in (True, False):
            try:
                prepared = self.conversations._prepare(
                    {paper_id: scoped}, settings,
                    request if optional else {**request, "passages": []},
                    extra_reading_context=extra if optional else None,
                )
            except ValidationError:
                if optional and (passages or extra):
                    continue
                raise
            if target_sources & prepared[3].get(paper_id, {}).keys():
                return _prune_note_context(report, paper_id, prepared)
            # Optional framing must not consume the selected object's source
            # budget. Rebuild locally before any model request is admitted.
        raise ValidationError("选中对象的原文超出输入预算，未生成笔记。")

    def generate(
        self,
        paper_id: str,
        report: DashboardReport,
        settings: Settings,
        payload: dict,
        *,
        budget: RequestBudget | None = None,
        resume: bool = False,
    ) -> dict:
        if "input_budget" in payload:
            limit = approved_limit(payload["input_budget"])
            with temporary_input_budget(limit):
                return self.generate(paper_id, report, settings,
                    {k: v for k, v in payload.items() if k != "input_budget"},
                    budget=budget, resume=resume)
        with self._paper_lock(paper_id):
            # Freeze source/context once; no request depends on a previous note.
            report = report.model_copy(deep=True)
            # Pipeline run IDs change after pause/retry. Cache only validated AI
            # notes, keyed by exact reading/source identity and generation settings.
            cache_id = self._resume_id(report, settings) if resume and report.reading else None
            if cache_id and not payload.get("dry_run"):
                existing = {note["id"] for note in self.notes.list(paper_id, report)}
                for cached in self.notes.list(cache_id, report):
                    if cached["id"] not in existing and not cached["detached"]:
                        self.notes.save(
                            paper_id, report, cached["id"], self._note_payload(cached, paper_id)
                        )
            plan = self.plan(paper_id, report, settings, payload)
            if payload.get("dry_run"):
                return plan
            result = {
                **plan,
                "notes": [],
                "generated": 0,
                "errors": [],
                "usage": Usage().model_dump(),
                "usage_records": [],
            }
            if not plan["planned"]:
                return result
            # One actual request per reading object; bulk notes do not multiply chat retries.
            routed = settings.for_role("chat").model_copy(update={"llm_max_retries": 0})
            shared = budget or RequestBudget(settings.llm_max_requests)
            items = {item.id: item for item in report.reading.items}
            completed: dict[int, dict] = {}
            usages: list[Usage] = []
            saved_notes: dict[int, dict] = {}
            stopped = Event()

            def generate_one(entry):
                item = items[entry["item_id"]]
                outcome = {"item_id": item.id, "stop": False, "usage": Usage()}
                provider = None
                inherited = copy_context()
                inherited_limit = capture_concurrency_limit()

                def check_attempt():
                    inherited.run(checkpoint)
                    if stopped.is_set():
                        raise _NotesStopped()

                try:
                    check_attempt()
                    config, request, refs, excerpts, serialized, reply, has_blocks = (
                        self._prepare(paper_id, report, settings, item)
                    )
                    if not has_blocks:
                        raise ValidationError("没有可核对的原文，未生成笔记。")
                    with (
                        processing_control(check_attempt, inherited_limit),
                        processing_stage("auto_notes"),
                        JsonProvider(routed, budget=shared) as provider,
                    ):
                        try:
                            raw = provider.json(
                                SYSTEM, serialized, max_tokens=routed.llm_max_output_tokens,
                            )
                            reply = self.conversations._finish(
                                {paper_id: report}, config, request, refs, excerpts, reply, raw,
                                recover_block_ids=True,
                            )
                        except ProviderError:
                            # Stop queued siblings before provider audit/close I/O.
                            stopped.set()
                            raise
                        if not reply["grounded"]:
                            raise ValidationError("笔记未通过原文引文核对，未保存。")
                        _validate_visual_pages(report, reply["answer"])
                        reply["usage"] = provider.usage.model_dump()
                        outcome["reply"] = reply
                except ProcessingPaused as exc:
                    outcome.update(paused=exc, stop=True)
                except _NotesStopped:
                    outcome["stop"] = True
                except InputBudgetExceeded as exc:
                    stopped.set()
                    outcome.update(error=str(exc), input_budget=exc.budget, stop=True)
                except ProviderError as exc:
                    stopped.set()
                    outcome.update(error=f"{exc} 本批已停止，已完成笔记保留。", stop=True)
                    if getattr(exc, "details", None):
                        outcome["provider_error"] = exc.details
                except (PaperLensError, OSError, ValueError) as exc:
                    outcome["error"] = str(exc) if isinstance(exc, PaperLensError) else (
                        "此段笔记保存失败，已完成笔记保留。"
                    )
                finally:
                    if provider is not None:
                        outcome["usage"] = provider.usage.model_copy()
                outcome["stop"] = outcome["stop"] or stopped.is_set()
                return outcome

            def collect(index, outcome):
                completed[index] = outcome
                usages.append(outcome["usage"])
                if "reply" in outcome:
                    item, reply = items[outcome["item_id"]], outcome["reply"]
                    try:
                        saved = self.notes.save(paper_id, report,
                            self._identity(report, item, settings), {
                                "item_id": item.id, "kind": "ai", "text": reply["answer"],
                                "reply": reply, "auto_types": plan["types"],
                            })
                        saved_notes[index] = saved
                        if cache_id:
                            self.notes.save(cache_id, report, saved["id"],
                                            self._note_payload(saved, cache_id))
                    except (PaperLensError, OSError, ValueError) as exc:
                        outcome["error"] = str(exc) if isinstance(exc, PaperLensError) else (
                            "此段笔记保存失败，已完成笔记保留。"
                        )
                if "paused" in outcome:
                    raise outcome["paused"]

            try:
                coordinated_map(generate_one, plan["items"], limit=note_concurrency,
                                on_result=collect, stop_when=lambda outcome: outcome["stop"])
            except ProcessingPaused as exc:
                # Keep billed in-flight attempts visible to the pipeline audit.
                exc.usage_records = [{
                    "model": routed.llm_model, "account_id": routed.llm_account_id,
                    "provider_base_url": routed.llm_base_url,
                    "usage": sum_usage(usages).model_dump(),
                }]
                raise
            finally:
                result["usage"] = sum_usage(usages).model_dump()
                result["usage_records"] = [{
                    "model": routed.llm_model, "account_id": routed.llm_account_id,
                    "provider_base_url": routed.llm_base_url, "usage": result["usage"],
                }]
            result["notes"] = [saved_notes[i] for i in sorted(saved_notes)]
            result["generated"] = len(saved_notes)
            result["errors"] = [
                {"item_id": completed[i]["item_id"], "message": completed[i]["error"]}
                | ({"input_budget": completed[i]["input_budget"]}
                   if "input_budget" in completed[i] else {})
                | ({"provider_error": completed[i]["provider_error"]}
                   if "provider_error" in completed[i] else {})
                for i in sorted(completed) if "error" in completed[i]
            ]
            return result
