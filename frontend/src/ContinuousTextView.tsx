import { DeferredReadingSection } from "./DeferredReadingSection";
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  FormulaView,
  TexFormula,
  Marked,
  TranscriptionView,
  jumpTo,
  useScrollSpy,
} from "./ReaderViews";
import { PdfSource } from "./PdfSource";
import { unverifiedModelSource } from "./pdfSourceNavigation";
import { ResultConditions } from "./ResultConditions";
import { ImageViewer } from "./ImageViewer";
import { AssetImage } from "./AssetImage";
import { backgroundShortcutBlocked, sourceShortcutTarget } from "./dialog";
import { readingMatches, stepSearchIndex } from "./readingSearch";
import { sectionHeadings } from "./readingStructure";
import { CodeBlock } from "./CodeBlock";
import { listingLanguage, readingRuns } from "./readingCode";
import { ReadingMargin, useNoteMargin, type NoteGroup } from "./ReadingMargin";
import { Icon } from "./icons";
import { takeJump } from "./terms";
import { isTyping } from "./Shortcuts";
import type { Annotation } from "./api";
import type { Picked } from "./SelectionExplain";
import type { Box, DashboardReport, ReadingItem, ReadingPart } from "./types";
import {
  informationParts,
  referenceNumber,
  usesSourceOffsets,
  readingDocument,
  readingText,
  sourceLabel,
} from "./reading";
import {
  RichText,
  referenceIndex,
  referenceBacklinks,
  itemXrefKey,
  linkify,
  mostlyChinese,
  splitCaption,
  xrefIndex,
  type CitationBacklink,
  type LinkContext,
} from "./readerLinks";
import { useTranslations } from "./translations";
import { useTermReplacements } from "./useTermReplacements";
import { discoverTermCandidates, replaceTranslationTerms } from "./termReplacements";
import { TermTranslationSidebar } from "./TermTranslationSidebar";
import { matchesReference, ReferenceSearch } from "./ReferenceSearch";
import { CitationCopyButton } from "./CitationCopyButton";
import { ReferenceTree } from "./ReferenceTree";
import { ReferenceSourceActions } from "./ReferenceSourceActions";
import { displayReferenceText, referencePreview, useSourcePreference } from "./referenceSources";
import { sanitizeMathML, texRanges, texToMathML } from "./tex";
import "./styles/18-continuous-reader.css";
import {
  itemSource,
  positionKey,
  readLocal,
  readingPosition,
  readingPreferences,
  readingMeasure,
  useAvailableReadingWidth,
  writeLocal,
  type ReadingPosition,
  type ReadingPreferences,
} from "./readingExperience";

const labels: Record<string, string> = {
  publication: "出版信息",
  citation: "引用本文",
  authors: "作者信息",
  declarations: "基金声明",
  footnotes: "原文脚注",
  titles: "其他标题",
};
/** 原文 / 对照 / 译文, as in EasyRead's reading bar. */
type Language = "original" | "bilingual" | "zh";
const LANGUAGES: [Language, string, string][] = [
  ["original", "原文", "只看原文"],
  ["bilingual", "对照", "译文在上，原文在下"],
  ["zh", "译文", "只看译文，可逐段展开原文"],
];

export function ReadingLanguageSwitch({
  language,
  onChange,
}: {
  language: Language;
  onChange: (language: Language) => void;
}) {
  return (
    <div
      className="segmented language-switch"
      role="radiogroup"
      aria-label="阅读语言"
      aria-orientation="horizontal"
      onKeyDown={(event) => {
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        const index = LANGUAGES.findIndex(([id]) => id === language);
        const next =
          event.key === "ArrowRight" || event.key === "ArrowDown"
            ? (index + 1) % LANGUAGES.length
            : event.key === "ArrowLeft" || event.key === "ArrowUp"
              ? (index + LANGUAGES.length - 1) % LANGUAGES.length
              : event.key === "Home"
                ? 0
                : event.key === "End"
                  ? LANGUAGES.length - 1
                  : null;
        if (next === null) return;
        event.preventDefault();
        event.stopPropagation();
        onChange(LANGUAGES[next][0]);
        event.currentTarget
          .querySelectorAll<HTMLButtonElement>("[role='radio']")
          [next]?.focus();
      }}
    >
      {LANGUAGES.map(([id, name, hint]) => (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={language === id}
          tabIndex={language === id ? 0 : -1}
          title={hint}
          onClick={() => onChange(id)}
        >
          {name}
        </button>
      ))}
    </div>
  );
}

const TRANSLATABLE = new Set(["paragraph", "heading", "figure", "table"]);
const TITLE_ID = "@title";

type CitationPosition = ReadingPosition & {
  viewport?: {
    scrollTop: number;
    paneWidth: number;
    proseWidth: number;
    anchorTop: number;
    anchorHeight: number;
  };
};

/** A citation can start above the reading line, where a clamped fraction loses space. */
export function captureCitationPosition(
  position: ReadingPosition,
  prose: HTMLElement | null,
): CitationPosition {
  const el = document.getElementById(position.id);
  const pane = prose?.closest<HTMLElement>(".reader-main");
  if (!el || !prose?.contains(el) || !pane) return position;
  const rect = el.getBoundingClientRect();
  return {
    ...position,
    viewport: {
      scrollTop: pane.scrollTop,
      paneWidth: pane.clientWidth,
      proseWidth: prose.clientWidth,
      anchorTop: rect.top - prose.getBoundingClientRect().top,
      anchorHeight: rect.height,
    },
  };
}

/** Preserve exact citation position if its layout is unchanged; otherwise use the anchor. */
export function restoreReadingPosition(
  position: CitationPosition,
  prose: HTMLElement | null,
  line: number,
) {
  const el = document.getElementById(position.id);
  const pane = prose?.closest<HTMLElement>(".reader-main");
  if (!el || !prose?.contains(el) || !pane) return;
  const rect = el.getBoundingClientRect();
  const saved = position.viewport;
  if (
    saved &&
    pane.clientWidth === saved.paneWidth &&
    prose.clientWidth === saved.proseWidth &&
    Math.abs(rect.top - prose.getBoundingClientRect().top - saved.anchorTop) < 1 &&
    Math.abs(rect.height - saved.anchorHeight) < 1
  ) {
    pane.scrollTop = saved.scrollTop;
  } else {
    pane.scrollTop += rect.top + rect.height * position.fraction - line;
  }
}

/** A wide table shrinks a little to fit the column before it scrolls. */
function FitTable({ children }: { children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = box.current;
    const table = el?.querySelector("table");
    if (!el || !table) return;
    const fit = () => {
      table.style.removeProperty("zoom");
      const ratio = el.clientWidth / Math.max(1, table.scrollWidth);
      const floor = el.clientWidth < 520 ? 0.58 : 0.72;
      if (ratio < 0.995) table.style.setProperty("zoom", String(Math.max(floor, ratio)));
    };
    fit();
    // Zooming the table changes the box's height, so only a new width refits, and the
    // refit waits for the next frame instead of resizing inside the observer callback.
    let width = el.clientWidth;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(fit);
    });
    observer.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);
  return (
    <div className="table-scroll" ref={box}>
      {children}
    </div>
  );
}

export function ContinuousTextView({
  report,
  paperId = "",
  query,
  searchIndex,
  onSearchIndexChange,
  searchTarget,
  onClearQuery,
  onExport,
  selectionTool,
  marks = [],
  onMark,
  toolbarExtra,
  outlineSlot,
  termSidebarSlot,
  termsOpen = false,
  onOpenTerms,
  onCloseTerms,
  canTranslate = false,
  canReferenceTree = false,
  onAskPassage,
  onAskItem,
  onCreateNote,
  onCheckItem,
  noteGroups = [],
  renderNotes,
  noteCount,
  sidePanelOpen = false,
  onCurrentItem,
}: {
  report: DashboardReport;
  paperId?: string;
  query: string;
  searchIndex?: number;
  onSearchIndexChange?: (index: number) => void;
  searchTarget?: { id: string; tick: number } | null;
  onClearQuery: () => void;
  onExport: () => void;
  selectionTool?: ReactNode;
  marks?: Annotation[];
  onMark?: (a: Annotation, el: HTMLElement) => void;
  toolbarExtra?: ReactNode;
  outlineSlot: HTMLElement | null;
  termSidebarSlot?: HTMLElement | null;
  termsOpen?: boolean;
  onOpenTerms?: () => void;
  onCloseTerms?: () => void;
  /** The local service can make a reading translation on request. */
  canTranslate?: boolean;
  canReferenceTree?: boolean;
  /** Takes a whole paragraph into the paper's quick chat. */
  onAskPassage?: (picked: Picked) => void;
  onAskItem?: (item: ReadingItem) => void;
  onCreateNote?: (item: ReadingItem, kind: "note" | "question", quote?: string) => void;
  onCheckItem?: (item: ReadingItem) => void;
  noteGroups?: NoteGroup[];
  renderNotes?: (itemId: string) => ReactNode;
  noteCount?: (itemId: string) => number;
  sidePanelOpen?: boolean;
  onCurrentItem?: (itemId: string) => void;
}) {
  const reading = useMemo(() => readingDocument(report), [report]);
  const blocks = useMemo(
    () => new Map(report.document.blocks.map((b) => [b.id, b])),
    [report],
  );
  const runs = useMemo(() => readingRuns(reading.items, blocks), [reading, blocks]);
  const codeListings = useMemo(
    () =>
      new Map(
        runs
          .filter((run) => run[0].kind === "code")
          .map((run) => {
            const fragments = run.map((part) => ({
              id: part.id,
              text: readingText(part, blocks),
              blockIds: [...new Set(part.parts.map((p) => p.block_id))],
            }));
            return [
              run[0].id,
              {
                fragments,
                text: fragments.map((part) => part.text).join("\n"),
                language: listingLanguage(run),
              },
            ];
          }),
      ),
    [runs, blocks],
  );
  const [source, setSource] = useState<{
    page: number;
    box?: Box | null;
    itemId?: string;
  } | null>(null);
  const [image, setImage] = useState<ReadingItem | null>(null);
  const [following, setFollowing] = useState(true);
  const prose = useRef<HTMLElement>(null);
  const column = useRef<HTMLDivElement>(null);
  const noteMargin = useNoteMargin(
    column,
    !!noteGroups.length && !source && !sidePanelOpen,
  );
  const toolbar = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const bar = toolbar.current;
    const shell = bar?.closest<HTMLElement>(".shell");
    if (!bar || !shell) return;
    // Follow the actual toolbar edge, including wrapped controls and typesetting.
    const update = () => {
      shell.style.setProperty(
        "--term-sidebar-top",
        `${Math.max(0, bar.getBoundingClientRect().bottom)}px`,
      );
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(bar);
    for (const element of shell.querySelectorAll(".reader-head, .topbar"))
      observer.observe(element);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      shell.style.removeProperty("--term-sidebar-top");
    };
  }, []);
  const progress = useRef<HTMLDivElement>(null);
  const [preferences, setPreferences] = useState(() =>
    readingPreferences(readLocal("paperlens:reading-type")),
  );
  const availableReadingWidth = useAvailableReadingWidth(
    column,
    prose,
    noteMargin,
    String(!!source),
  );
  const measure = readingMeasure(preferences, availableReadingWidth);
  const [typeOpen, setTypeOpen] = useState(false);
  const translations = useTranslations(
    paperId,
    canTranslate && !!paperId,
    report.translations,
  );
  const termReplacements = useTermReplacements();
  const displayedTranslations = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(translations.items).map(([id, item]) => [
          id,
          replaceTranslationTerms(item.text, termReplacements.rules),
        ]),
      ),
    [translations.items, termReplacements.rules],
  );
  const [termsVisited, setTermsVisited] = useState(false);
  useEffect(() => {
    if (termsOpen) setTermsVisited(true);
  }, [termsOpen]);
  const discoverTerms = termsOpen || termsVisited;
  const termCandidates = useMemo(
    () =>
      discoverTerms
        ? discoverTermCandidates(report.analysis.glossary || [], translations.items)
        : [],
    [discoverTerms, report.analysis.glossary, translations.items],
  );
  // A paper already in Chinese has nothing to translate.
  const chinese = useMemo(
    () =>
      mostlyChinese(
        reading.items
          .filter((i) => i.kind === "paragraph")
          .slice(0, 40)
          .map((i) => readingText(i, blocks))
          .join(" "),
      ),
    [reading, blocks],
  );
  const hasTranslations = Object.keys(translations.items).length > 0;
  const offerLanguages = !chinese && (canTranslate || hasTranslations);
  const [chosenLanguage, setChosenLanguage] = useState<Language>(() => {
    const saved = readLocal("paperlens:reading-language");
    return LANGUAGES.some(([id]) => id === saved) ? (saved as Language) : "original";
  });
  const language: Language = offerLanguages ? chosenLanguage : "original";
  const [shownOriginal, setShownOriginal] = useState<Record<string, boolean>>({});
  const [active, setActive] = useState<string | null>(null);
  const [copied, setCopied] = useState("");
  const positionIds = useMemo(
    () =>
      new Set([
        "article-opening",
        ...reading.items.map((i) => i.id),
        "article-references",
        ...reading.references.map((i) => i.id),
        "article-information",
      ]),
    [reading],
  );
  const storageKey = positionKey(reading.source_version);
  const [resume, setResume] = useState<ReadingPosition | null>(() =>
    readingPosition(readLocal(storageKey), positionIds),
  );
  const currentPosition = useRef<ReadingPosition | null>(null);
  const pendingLayout = useRef<ReadingPosition | null>(null);
  const [readingAnchor, setReadingAnchor] = useState<{
    id: string;
    part?: number;
  } | null>(null);
  const openSource = (target: { page: number; box?: Box | null; itemId?: string }) => {
    pendingLayout.current = capturePosition();
    setFollowing(false);
    setSource(target);
  };
  const readingLine = () => (toolbar.current?.getBoundingClientRect().bottom || 80) + 16;
  const capturePosition = (): ReadingPosition | null => {
    const elements = Array.from(
      prose.current?.querySelectorAll<HTMLElement>(
        "#article-opening,.reading-item,.reference-card,#article-references,#article-information",
      ) || [],
    );
    const line = readingLine();
    // Prefer the last block that has reached the reading line, including a
    // paragraph taller than the viewport. No document-wide/source-pane query.
    const el =
      elements.filter((e) => e.getBoundingClientRect().top <= line).at(-1) || elements[0];
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return {
      id: el.id,
      fraction: Math.min(1, Math.max(0, (line - rect.top) / Math.max(1, rect.height))),
    };
  };
  const restorePosition = (position: CitationPosition) =>
    restoreReadingPosition(position, prose.current, readingLine());
  const changePreferences = (change: Partial<ReadingPreferences>) => {
    pendingLayout.current = capturePosition();
    setPreferences((old) => readingPreferences({ ...old, ...change }));
  };
  const changeLanguage = (next: Language) => {
    pendingLayout.current = capturePosition();
    setChosenLanguage(next);
    writeLocal("paperlens:reading-language", next);
  };
  useLayoutEffect(() => {
    writeLocal("paperlens:reading-type", preferences);
    if (pendingLayout.current) restorePosition(pendingLayout.current);
    pendingLayout.current = null;
  }, [preferences, typeOpen, !!source, language, termReplacements.rules]);
  useEffect(() => {
    setResume(readingPosition(readLocal(storageKey), positionIds));
    currentPosition.current = null;
    const pane = prose.current?.closest<HTMLElement>(".reader-main");
    if (!pane) return;
    let frame = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const measure = (save = false) => {
      const room = pane.scrollHeight - pane.clientHeight;
      progress.current?.style.setProperty(
        "--read",
        String(room > 0 ? Math.min(1, pane.scrollTop / room) : 0),
      );
      const position = capturePosition();
      if (!position) return;
      currentPosition.current = position;
      const item = reading.items.find((i) => i.id === position.id);
      let part: number | undefined;
      if (item && (usesSourceOffsets(item, blocks) || item.content_spans?.length)) {
        const line = readingLine();
        const spans = Array.from(
          document
            .getElementById(item.id)
            ?.querySelectorAll<HTMLElement>("[data-raw-text]") || [],
        );
        const span =
          spans.filter((el) => el.getBoundingClientRect().top <= line).at(-1) || spans[0];
        if (span) {
          const index = item.parts.findIndex(
            (p) =>
              p.block_id === span.dataset.blockId &&
              p.start <= Number(span.dataset.sourceStart) &&
              p.end > Number(span.dataset.sourceStart),
          );
          if (index >= 0) part = index;
        }
      }
      setReadingAnchor((old) =>
        old?.id === position.id && old.part === part ? old : { id: position.id, part },
      );
      if (save) {
        clearTimeout(timer);
        timer = setTimeout(() => writeLocal(storageKey, position), 250);
      }
    };
    const scroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => measure(true));
    };
    // Measuring inside the observer callback would set state mid-layout.
    let resizeFrame = 0;
    const resized = new ResizeObserver(() => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => measure());
    });
    if (prose.current) resized.observe(prose.current);
    pane.addEventListener("scroll", scroll, { passive: true });
    const flush = () => {
      // An untouched first screen must not erase a saved later position.
      if (timer && currentPosition.current)
        writeLocal(storageKey, currentPosition.current);
    };
    window.addEventListener("pagehide", flush);
    measure();
    return () => {
      flush();
      cancelAnimationFrame(frame);
      cancelAnimationFrame(resizeFrame);
      clearTimeout(timer);
      resized.disconnect();
      pane.removeEventListener("scroll", scroll);
      window.removeEventListener("pagehide", flush);
    };
  }, [storageKey, reading, positionIds]);
  useEffect(() => {
    if (!source || !following || !readingAnchor) return;
    const item = reading.items.find((i) => i.id === readingAnchor.id);
    if (item) {
      const target = itemSource(item, blocks, readingAnchor.part);
      setSource((old) =>
        old?.itemId === item.id && old.page === target.page && old.box === target.box
          ? old
          : { ...target, itemId: item.id },
      );
    }
  }, [readingAnchor, following, !!source, reading, blocks]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [returnTo, setReturnTo] = useState<CitationPosition | null>(null);
  const [lastCitation, setLastCitation] = useState<{
    referenceId: string;
    position: CitationPosition;
  } | null>(null);
  const [citationChoice, setCitationChoice] = useState<{
    keys: string[];
    from: string;
    position: CitationPosition;
    left: number;
    top: number;
  } | null>(null);
  const [backlinkChoice, setBacklinkChoice] = useState<string | null>(null);
  const choiceClose = useRef<HTMLButtonElement>(null);
  const choiceTrigger = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (citationChoice) choiceClose.current?.focus();
  }, [citationChoice]);
  const closeCitationChoice = () => {
    setCitationChoice(null);
    requestAnimationFrame(() => choiceTrigger.current?.focus());
  };
  const [referenceQuery, setReferenceQuery] = useState("");
  const sourcePreference = useSourcePreference();
  const hits = useMemo(() => readingMatches(report, query), [report, query]);
  const [localHit, setLocalHit] = useState(-1);
  const hit = searchIndex ?? localHit;
  const setHit = (index: number) => {
    setLocalHit(index);
    onSearchIndexChange?.(index);
  };
  useEffect(() => {
    setLocalHit(-1);
  }, [query]);
  const [pendingJump, setPendingJump] = useState<string | null>(null);
  const refs = useMemo(
    () => referenceIndex(reading.references, blocks),
    [reading, blocks],
  );
  const backlinks = useMemo(
    () => referenceBacklinks(reading.items, blocks, refs),
    [reading.items, blocks, refs],
  );
  const referenceKeys = useMemo(() => {
    const result = new Map<string, string[]>();
    for (const [key, item] of refs)
      result.set(item.id, [...(result.get(item.id) || []), key]);
    return result;
  }, [refs]);
  const referenceLabel = (item: ReadingItem) => {
    const label = referenceNumber(item);
    if (label) return `[${label}]`;
    const authorYear = (referenceKeys.get(item.id) || []).find(
      (key) => !key.includes("&") && /(?:18|19|20)\d{2}[a-z]?$/i.test(key),
    );
    return authorYear?.replace(/((?:18|19|20)\d{2}[a-z]?)$/i, " · $1") || "";
  };
  const targets = useMemo(() => xrefIndex(reading.items, blocks), [reading, blocks]);
  const links: LinkContext = useMemo(() => ({ refs, targets }), [refs, targets]);
  const toc = useMemo(
    () => [
      { id: "article-opening", label: "开篇", level: 1, kind: "heading" },
      ...sectionHeadings(reading.items).map((i) => ({
        id: i.id,
        label: i.label || readingText(i, blocks).trim() || "章节",
        level: i.level,
        kind: i.kind,
      })),
      ...(reading.references.length
        ? [
            {
              id: "article-references",
              label: `参考文献 · ${reading.references.length}`,
              level: 1,
              kind: "heading",
            },
          ]
        : []),
      { id: "article-information", label: "论文信息", level: 1, kind: "heading" },
    ],
    [reading, blocks],
  );
  const [current, pin] = useScrollSpy(
    useMemo(() => toc.map((x) => x.id), [toc]),
    82,
  );
  const parentIds = useMemo(() => {
    const out = new Map<string, string>();
    let parent = "article-opening";
    for (const x of toc) {
      if (x.level === 1) parent = x.id;
      else out.set(x.id, parent);
    }
    return out;
  }, [toc]);
  const currentParent = parentIds.get(current || "") || current;
  const isOpen = (id: string) => expanded[id] ?? currentParent === id;
  /** The section around the reading position: its heading up to the next one of equal rank. */
  const section = useMemo(() => {
    const items = reading.items;
    let at = items.findIndex((i) => i.id === readingAnchor?.id);
    if (at < 0) at = 0;
    let start = at;
    while (start > 0 && items[start].kind !== "heading") start--;
    const head = items[start]?.kind === "heading" ? items[start] : null;
    let end = start + 1;
    while (
      end < items.length &&
      !(items[end].kind === "heading" && (!head || items[end].level <= head.level))
    )
      end++;
    const ids = items
      .slice(head ? start : 0, end)
      .filter((i) => TRANSLATABLE.has(i.kind))
      .map((i) => i.id);
    return head ? ids : [TITLE_ID, ...ids];
  }, [reading, readingAnchor]);
  const untranslated = section.filter((id) => !translations.items[id]);
  const translating = translations.pending.size > 0;
  const translationOf = (id: string) =>
    language === "original" ? undefined : displayedTranslations[id];
  // J / K step through paragraphs, like turning lines with a finger.
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (
        backgroundShortcutBlocked(e) ||
        sourceShortcutTarget(e) ||
        e.ctrlKey ||
        e.metaKey ||
        e.altKey ||
        isTyping(e)
      )
        return;
      if (e.key === "Escape" && active) {
        setActive(null);
        return;
      }
      const k = e.key.toLowerCase();
      if (k !== "j" && k !== "k") return;
      const items = Array.from(
        prose.current?.querySelectorAll<HTMLElement>(".reading-item") || [],
      );
      if (!items.length) return;
      const line = readingLine();
      const top = (el: HTMLElement) => el.getBoundingClientRect().top;
      const target =
        k === "j"
          ? items.find((el) => top(el) > line + 4)
          : items.filter((el) => top(el) < line - 4).at(-1);
      if (!target) return;
      e.preventDefault();
      restorePosition({ id: target.id, fraction: 0 });
      setActive(target.id);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [reading, active]);
  const jump = (id: string, remember = true) => {
    if (remember) {
      setReturnTo(capturePosition());
    }
    pin(id);
    if (reading.references.some((reference) => reference.id === id)) {
      setReferenceQuery("");
      setPendingJump(id);
    } else jumpTo(id);
  };
  useLayoutEffect(() => {
    if (!pendingJump) return;
    jumpTo(pendingJump);
    setPendingJump(null);
  }, [pendingJump, referenceQuery]);
  useEffect(() => {
    if (!searchTarget) return;
    const index = hits.findIndex((match) => match.id === searchTarget.id);
    if (index < 0) return;
    setHit(index);
    jump(searchTarget.id);
  }, [searchTarget]);
  useEffect(() => {
    const id = takeJump("text");
    if (id) {
      const target = reading.items.find(
        (item) => item.id === id || item.parts.some((p) => p.block_id === id),
      );
      requestAnimationFrame(() => jumpTo(target?.id || `block-${id}`));
    }
    try {
      const pending = JSON.parse(
        sessionStorage.getItem("paperlens:pending-reading-item") || "null",
      );
      if (pending?.paperId === paperId) {
        sessionStorage.removeItem("paperlens:pending-reading-item");
        requestAnimationFrame(() => jumpTo(pending.itemId));
      }
    } catch {
      /* No pending jump. */
    }
  }, [paperId]);
  useEffect(() => {
    const id = active || readingAnchor?.id;
    if (id) onCurrentItem?.(id);
  }, [active, readingAnchor?.id, onCurrentItem]);
  const citationPosition = (from: string) =>
    captureCitationPosition(
      capturePosition() || { id: from, fraction: 0 },
      prose.current,
    );
  const cite = (number: string, from: string, captured?: CitationPosition) => {
    const ref = refs.get(number);
    if (ref) {
      const position = captured || citationPosition(from);
      setReturnTo(position);
      setLastCitation({ referenceId: ref.id, position });
      setCitationChoice(null);
      setBacklinkChoice(null);
      pin("article-references");
      setReferenceQuery("");
      setPendingJump(ref.id);
    }
  };
  const goToBacklink = (backlink: CitationBacklink) => {
    setBacklinkChoice(null);
    if (language === "zh")
      setShownOriginal((old) => ({ ...old, [backlink.itemId]: true }));
    jump(backlink.itemId);
    const element = document.getElementById(backlink.itemId);
    if (element && prose.current?.contains(element)) {
      element.tabIndex = -1;
      element.focus({ preventScroll: true });
    }
  };
  /* Hover previews for citations and cross-references (delegated, one popover). */
  const [preview, setPreview] = useState<{
    kind: "cite" | "xref";
    keys: string[];
    left: number;
    top: number;
    below: boolean;
  } | null>(null);
  const previewTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const linkTarget = (e: { target: EventTarget | null }) =>
    (e.target as Element | null)?.closest?.<HTMLElement>("[data-cite],[data-xref]");
  const showPreview = (el: HTMLElement) => {
    clearTimeout(previewTimer.current);
    previewTimer.current = setTimeout(() => {
      const rect = el.getBoundingClientRect();
      const below = rect.top < window.innerHeight * 0.45;
      setPreview({
        kind: el.dataset.cite ? "cite" : "xref",
        keys: (
          el.dataset.cites ||
          el.dataset.xrefs ||
          el.dataset.cite ||
          el.dataset.xref ||
          ""
        ).split(" "),
        left: Math.max(12, Math.min(window.innerWidth - 392, rect.left - 40)),
        top: below ? rect.bottom + 8 : rect.top - 8,
        below,
      });
    }, 160);
  };
  const hidePreview = () => {
    clearTimeout(previewTimer.current);
    setPreview(null);
  };
  const scheduleHidePreview = () => {
    clearTimeout(previewTimer.current);
    previewTimer.current = setTimeout(() => setPreview(null), 220);
  };
  useEffect(() => () => clearTimeout(previewTimer.current), []);
  const onProseClick = (e: ReactMouseEvent) => {
    const link = linkTarget(e);
    const from =
      (e.target as Element).closest<HTMLElement>(".reading-item,.reference-card")?.id ||
      "article-opening";
    if (link) {
      hidePreview();
      if (link.dataset.cite) {
        const keys = (link.dataset.cites || link.dataset.cite).split(" ").filter(Boolean);
        if (keys.length > 1) {
          const rect = link.getBoundingClientRect();
          choiceTrigger.current = link;
          setCitationChoice({
            keys,
            from,
            position: citationPosition(from),
            left: Math.max(12, Math.min(window.innerWidth - 392, rect.left - 40)),
            top: Math.min(window.innerHeight - 80, rect.bottom + 8),
          });
        } else cite(keys[0], from);
      } else {
        const item = targets.get(link.dataset.xref || "");
        if (item) jump(item.id);
      }
      return;
    }
    // A tap on a paragraph opens its actions; selecting words does not.
    if (getSelection()?.toString().trim()) return;
    if (
      (e.target as Element).closest(
        "button,a,input,select,summary,details,.para-actions,.reading-note-card",
      )
    )
      return;
    const item = (e.target as Element).closest<HTMLElement>(".reading-item");
    setActive((old) => (item && old !== item.id ? item.id : null));
  };
  const span = (part: ReadingPart, withAnchor = true): ReactNode => {
    const b = blocks.get(part.block_id);
    if (!b) return null;
    const text = b.text.slice(part.start, part.end);
    const math = texRanges(text);
    if (math.length) {
      const pieces: ReactNode[] = [];
      let cursor = 0;
      // MathML has a different DOM text representation. Never attach source
      // offsets to it; prose on either side retains the actual original ranges.
      if (withAnchor)
        pieces.push(<span key="anchor" id={`block-${b.id}`} aria-hidden="true" />);
      for (const run of math) {
        if (run.start > cursor)
          pieces.push(
            <Fragment key={`text-${cursor}`}>
              {span(
                {
                  ...part,
                  start: part.start + cursor,
                  end: part.start + run.start,
                  join: "",
                },
                false,
              )}
            </Fragment>,
          );
        pieces.push(
          <RichText key={`math-${run.start}`} text={text.slice(run.start, run.end)} />,
        );
        cursor = run.end;
      }
      if (cursor < text.length)
        pieces.push(
          <Fragment key={`text-${cursor}`}>
            {span({ ...part, start: part.start + cursor, join: "" }, false)}
          </Fragment>,
        );
      return (
        <Fragment key={`${b.id}-${part.start}`}>
          {part.join}
          {pieces}
        </Fragment>
      );
    }
    const annotations = marks.filter(
      (a) =>
        a.selection.block_id === b.id &&
        a.selection.start < part.end &&
        a.selection.end > part.start,
    );
    const shifted = annotations.map((a) => ({
      ...a,
      selection: {
        ...a.selection,
        start: Math.max(0, a.selection.start - part.start),
        end: Math.min(text.length, a.selection.end - part.start),
      },
    }));
    return (
      <Fragment key={`${b.id}-${part.start}`}>
        {part.join}
        <span
          id={withAnchor ? `block-${b.id}` : undefined}
          data-block-id={b.id}
          data-source-start={part.start}
          data-raw-text
        >
          {query || shifted.length ? (
            <Marked
              text={text}
              query={query}
              marks={shifted}
              ctx={links}
              onMark={(a, el) => {
                const original = marks.find((m) => m.id === a.id);
                if (original) onMark?.(original, el);
              }}
            />
          ) : (
            linkify(text, links)
          )}
        </span>
      </Fragment>
    );
  };
  /** Empty anchors keep search and jumps working while an original is folded away. */
  const anchors = (item: ReadingItem) =>
    [...new Set(item.parts.map((p) => p.block_id))].map((id) => (
      <span key={id} id={`block-${id}`} aria-hidden="true" />
    ));
  const originalText = (item: ReadingItem, limit?: number, sourceAnchors = true): ReactNode => {
    if (usesSourceOffsets(item, blocks)) {
      if (limit === undefined)
        return item.parts.length ? item.parts.map((p) => span(p, sourceAnchors)) : item.label;
      let remaining = limit;
      return item.parts.map((part) => {
        if (remaining <= 0) return null;
        const join = part.join.slice(0, remaining);
        remaining -= join.length;
        const block = blocks.get(part.block_id);
        const count = Math.min(remaining, block?.text.slice(part.start, part.end).length || 0);
        remaining -= count;
        return span({ ...part, join, end: part.start + count }, sourceAnchors);
      });
    }
    if (item.content != null) {
      const content = limit === undefined ? item.content : item.content.slice(0, limit);
      const pieces: ReactNode[] = [];
      let cursor = 0;
      for (const run of item.content_spans || []) {
        const b = blocks.get(run.source.block_id);
        if (
          run.start < cursor ||
          run.end <= run.start ||
          run.end > content.length ||
          !b ||
          run.source.start < 0 ||
          run.source.end > b.text.length ||
          content.slice(run.start, run.end) !==
            b.text.slice(run.source.start, run.source.end)
        )
          continue;
        if (run.start > cursor)
          pieces.push(
            <RichText
              key={`gap-${cursor}`}
              text={content.slice(cursor, run.start)}
              query={query}
              ctx={links}
            />,
          );
        pieces.push(
          <Fragment key={`source-${run.start}`}>
            {span({ ...run.source, join: "" }, false)}
          </Fragment>,
        );
        cursor = run.end;
      }
      if (cursor < content.length)
        pieces.push(
          <RichText
            key={`gap-${cursor}`}
            text={content.slice(cursor)}
            query={query}
            ctx={links}
          />,
        );
      return pieces;
    }
    return item.transcription ? (
      limit === undefined ? (
        <TranscriptionView transcription={item.transcription} query={query} ctx={links} />
      ) : (
        <RichText text={readingText(item, blocks).slice(0, limit)} query={query} ctx={links} />
      )
    ) : null;
  };
  const pagesOf = (item: ReadingItem) => {
    const pages = item.source_pages?.length
      ? item.source_pages
      : item.parts.map((p) => blocks.get(p.block_id)?.page || item.page);
    return [...new Set(pages.length ? pages : [item.page])];
  };
  const openPage = (item: ReadingItem, page: number) =>
    openSource({
      page,
      itemId: item.id,
      box:
        page === item.page
          ? item.bbox
          : item.parts
              .filter((p) => blocks.get(p.block_id)?.page === page)
              .map((p) => p.bbox ?? blocks.get(p.block_id)?.bbox)[0],
    });
  const sourceButton = (item: ReadingItem) => (
    <span className="source-links">
      {pagesOf(item).map((page) => (
        <button
          key={page}
          className="text-button"
          title="查看原页及对应位置"
          onClick={() => openPage(item, page)}
        >
          {sourceLabel(report, [page])}
        </button>
      ))}
    </span>
  );
  const pageMark = (item: ReadingItem) => (
    <button
      className="page-mark"
      title={`${sourceLabel(report, [item.page])} · 查看原页`}
      aria-label={`查看原页 PDF ${item.page}`}
      onClick={() => openPage(item, item.page)}
    >
      p.{item.page}
    </button>
  );
  const copy = (id: string, text: string) => {
    void navigator.clipboard?.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied((old) => (old === id ? "" : old)), 1400);
  };
  const actions = (item: ReadingItem) => {
    const zh = displayedTranslations[item.id];
    const first = item.parts[0];
    const firstBlock = first && blocks.get(first.block_id);
    const canTranslateItem =
      canTranslate && !chinese && TRANSLATABLE.has(item.kind) && !zh;
    return (
      <div className="para-actions" role="toolbar" aria-label="段落操作">
        {onCreateNote && (
          <>
            <button onClick={() => onCreateNote(item, "note")}>笔记</button>
          </>
        )}
        {pagesOf(item).map((page) => (
          <button key={page} onClick={() => openPage(item, page)}>
            原页 {pagesOf(item).length > 1 ? `p.${page}` : ""}
          </button>
        ))}
        <button onClick={() => copy(item.id, readingText(item, blocks))}>
          {copied === item.id ? "已复制" : "复制原文"}
        </button>
        {zh && (
          <button onClick={() => copy(`${item.id}-zh`, zh)}>
            {copied === `${item.id}-zh` ? "已复制" : "复制译文"}
          </button>
        )}
        {canTranslateItem && (
          <button
            disabled={translating}
            onClick={() => {
              if (language === "original") changeLanguage("bilingual");
              void translations.translate([item.id]);
            }}
          >
            {translations.pending.has(item.id) ? "翻译中…" : "翻译此段"}
          </button>
        )}
        {language === "zh" && zh && (
          <button
            aria-pressed={!!shownOriginal[item.id]}
            onClick={() =>
              setShownOriginal((old) => ({ ...old, [item.id]: !old[item.id] }))
            }
          >
            {shownOriginal[item.id] ? "收起原文" : "展开原文"}
          </button>
        )}
        {onAskItem ? (
          <button onClick={() => onAskItem(item)}>问AI</button>
        ) : (
          onAskPassage &&
          firstBlock && (
            <button
              onClick={() =>
                onAskPassage({
                  block_id: firstBlock.id,
                  start: first.start,
                  end: Math.min(first.end, first.start + 1200),
                  text: firstBlock.text.slice(
                    first.start,
                    Math.min(first.end, first.start + 1200),
                  ),
                })
              }
            >
              问AI
            </button>
          )
        )}
        {onCheckItem && (
          <button onClick={() => onCheckItem(item)} title="请 核对原文上下文，会调用模型">
            核对原文
          </button>
        )}
        <button
          className="para-actions-close"
          aria-label="收起段落操作"
          onClick={() => setActive(null)}
        >
          <Icon name="close" size={13} />
        </button>
      </div>
    );
  };
  const inlineNotes = (item: ReadingItem) =>
    !noteMargin && !!noteCount?.(item.id) ? (
      <details className="reading-note-inline" key={`notes-${item.id}`}>
        <summary>笔记 · {noteCount(item.id)}</summary>
        <div>{renderNotes?.(item.id)}</div>
      </details>
    ) : null;
  const results = (item: ReadingItem) => {
    const values = (report.quantitative_results || []).filter((q) =>
      item.result_ids.includes(q.id),
    );
    if (!values.length) return null;
    return (
      <details className="object-data">
        <summary>查看数据 · {values.length} 项</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>指标 / 方法</th>
                <th>结果</th>
                <th>条件来源</th>
              </tr>
            </thead>
            <tbody>
              {values.map((q) => (
                <tr key={q.id}>
                  <td>
                    <RichText text={q.metric} />
                    <small>
                      <RichText
                        text={[q.method, q.dataset].filter(Boolean).join(" · ")}
                      />
                    </small>
                  </td>
                  <td>
                    {q.precision === "unavailable"
                      ? "数值仅存在于图中，未可靠读出"
                      : `${q.value_text || (q.value ?? "")}${q.unit}`}
                    <small>
                      {q.precision === "approximate"
                        ? "图像估读 · 近似值"
                        : q.precision === "unavailable"
                          ? "未知"
                          : q.source === "text" || q.source === "table_text"
                            ? "原文报告值"
                            : "图中标注值"}
                    </small>
                  </td>
                  <td>
                    <ResultConditions
                      result={q}
                      report={report}
                      onEvidence={(evidence, page = evidence.page) =>
                        openSource({
                          page,
                          box: (evidence.source_pages?.length || 0) > 1 ? undefined : evidence.bbox,
                          itemId: item.id,
                        })
                      }
                    />
                    {q.notes && (
                      <small>
                        <RichText text={q.notes} />
                      </small>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    );
  };
  /** "表 2：" in bold, then the caption; the other language below in grey. */
  const caption = (item: ReadingItem) => {
    if (item.kind === "formula" || (item.kind === "algorithm" && item.content != null))
      return null;
    const semanticCaption = ["figure", "table"].includes(item.kind)
      ? item.content?.trim()
      : "";
    const text = semanticCaption
      ? splitCaption(semanticCaption).tag
        ? semanticCaption
        : [item.label, semanticCaption].filter(Boolean).join(" ")
      : item.label;
    if (!text) return null;
    const zh = translationOf(item.id);
    const own = splitCaption(text);
    if (zh) {
      const translated = splitCaption(zh);
      return (
        <figcaption lang="zh-CN">
          <span className="caption-main">
            {translated.tag && <b className="caption-tag">{translated.tag}：</b>}
            <RichText text={translated.rest} ctx={links} />
          </span>
          {language === "bilingual" && (
            <span className="caption-en" lang="en">
              <RichText text={text} ctx={links} />
            </span>
          )}
        </figcaption>
      );
    }
    return (
      <figcaption>
        <span className="caption-main">
          {own.tag && <b className="caption-tag">{own.tag}</b>}
          {own.tag && own.rest ? " " : ""}
          <RichText text={own.rest} ctx={links} />
        </span>
        {own.other && (
          <span className="caption-en" lang="en">
            <RichText text={own.other} ctx={links} />
          </span>
        )}
      </figcaption>
    );
  };
  const object = (item: ReadingItem) => {
    const first = blocks.get(item.parts[0]?.block_id);
    const table = item.table ?? first?.table;
    const originalPage = report.document.pages.find((page) => page.number === item.page);
    const imageBox = item.asset_crop?.box ?? item.bbox;
    // Reserve the crop's space before lazy decoding, so citation jumps stay put.
    const imageSize =
      imageBox && originalPage?.width && originalPage.height
        ? {
            width: Math.max(
              1,
              Math.round((imageBox.x1 - imageBox.x0) * originalPage.width * 2),
            ),
            height: Math.max(
              1,
              Math.round((imageBox.y1 - imageBox.y0) * originalPage.height * 2),
            ),
          }
        : undefined;
    const descriptions = (report.visuals || []).filter(
      (v) => item.visual_ids.includes(v.id) && v.description,
    );
    const equation =
      item.kind === "formula" ? itemXrefKey(item, blocks)?.replace(/^eq:/, "") : "";
    const sourceImage = item.asset ? (
      <>
        <button
          className="object-image"
          aria-label={`放大图片：${item.label || item.kind}`}
          onClick={() => setImage(item)}
        >
          <AssetImage
            src={item.asset}
            retryable={false}
            alt={item.label || "原文公式或图表"}
            loading="lazy"
            {...imageSize}
            style={{ height: "auto" }}
          />
        </button>
        {item.asset_crop?.source === "page" ? (
          <small>未能可靠定位图像，已保留完整原页。</small>
        ) : (
          !item.asset_crop &&
          unverifiedModelSource(report.document.parser) && (
            <small>图片区域由模型估计，请对照原页核实。</small>
          )
        )}
      </>
    ) : null;
    // Tables carry their caption above, figures below, as journals print them.
    const captionFirst = item.kind === "table" || item.kind === "algorithm";
    // A recognized equation reads as TeX; its crop is one click away.
    const contentFormula =
      item.kind === "formula" && item.content != null
        ? {
            latex: item.content,
            mathml: texToMathML(item.content, true) || "",
            status: "renderable" as const,
            source: "vision" as const,
            verified: false,
            warnings: [],
          }
        : null;
    const tex =
      contentFormula ||
      (item.kind === "formula" &&
      !item.transcription &&
      first?.formula?.status === "renderable"
        ? first.formula
        : null);
    const algorithmText =
      item.kind === "algorithm" && (item.content != null || item.transcription);
    return (
      <figure className={`reading-object object-${item.kind} ${tex ? "is-tex" : ""}`}>
        {captionFirst && caption(item)}
        {tex && (
          <TexFormula
            formula={tex}
            image={sourceImage}
            number={equation || ""}
            onSource={() =>
              openSource({ page: item.page, box: item.bbox, itemId: item.id })
            }
          />
        )}
        {!table && !tex && !algorithmText && sourceImage}
        {!tex && equation && (
          <span className="equation-number" aria-label={`公式 ${equation}`}>
            ({equation})
          </span>
        )}
        {table && (
          <FitTable>
            <table>
              <tbody>
                {Array.from({ length: table.rows }, (_, r) => (
                  <tr key={r}>
                    {table.cells
                      .filter((c) => c.row_start === r)
                      .sort((a, b) => a.col_start - b.col_start)
                      .map((c, n) => {
                        const Tag = c.column_header ? "th" : "td";
                        return (
                          <Tag
                            key={n}
                            rowSpan={Math.max(1, c.row_end - c.row_start)}
                            colSpan={Math.max(1, c.col_end - c.col_start)}
                          >
                            <RichText text={c.text} ctx={links} />
                          </Tag>
                        );
                      })}
                  </tr>
                ))}
              </tbody>
            </table>
          </FitTable>
        )}
        {!captionFirst && caption(item)}
        {item.asset && !tex && (
          <div className="object-source-tools">
            <button className="text-button" onClick={() => openPage(item, item.page)}>
              查看原页
            </button>
          </div>
        )}
        {(table || algorithmText) && sourceImage && (
          <details className="object-original">
            <summary>{table ? "原表图片" : "算法原图"}</summary>
            {sourceImage}
          </details>
        )}
        {!tex &&
          !table &&
          item.kind !== "figure" &&
          item.kind !== "table" &&
          item.content != null && (
            <div className="transcription-content">{originalText(item)}</div>
          )}
        {item.content == null && item.transcription && (
          <TranscriptionView
            transcription={item.transcription}
            query={query}
            ctx={links}
          />
        )}
        {!item.transcription &&
          !item.asset &&
          !table &&
          !tex &&
          first?.formula?.status === "renderable" && (
            <FormulaView formula={first.formula} fallback={first.text} />
          )}
        {["formula", "algorithm"].includes(item.kind) &&
          !item.transcription &&
          item.content == null &&
          first?.formula?.status !== "renderable" && (
            <p className="object-fallback">请查看原页。</p>
          )}
        {results(item)}
        {descriptions.length > 0 && (
          <details className="object-ai">
            <summary>图表解读</summary>
            {descriptions.map((v) => (
              <div key={v.id}>
                <p>
                  <RichText text={v.description} ctx={links} />
                </p>
                {v.warnings.map((w, k) => (
                  <small key={k}>
                    <RichText text={w} />
                  </small>
                ))}
              </div>
            ))}
          </details>
        )}
      </figure>
    );
  };
  const heading = (item: ReadingItem) => {
    const zh = translationOf(item.id);
    const original = originalText(item);
    return (
      <h3 className={`article-section level-${item.level}`}>
        {zh ? (
          <>
            <span lang="zh-CN">
              <RichText text={zh} ctx={links} />
            </span>
            {language === "bilingual" || shownOriginal[item.id] ? (
              <span className="heading-en" lang="en">
                {original}
              </span>
            ) : (
              anchors(item)
            )}
          </>
        ) : (
          original
        )}
      </h3>
    );
  };
  const paragraph = (item: ReadingItem) => {
    const zh = translationOf(item.id);
    const showOriginal = !zh || language === "bilingual" || shownOriginal[item.id];
    const original = (
      <p
        className={`reading-paragraph${zh ? " reading-en" : ""}`}
        data-reading-surface="original"
        lang={zh || !chinese ? "en" : undefined}
      >
        {originalText(item)}
      </p>
    );
    return (
      <>
        {zh && (
          <p className="reading-zh" lang="zh-CN" data-reading-surface="translation">
            <RichText text={zh} ctx={links} />
          </p>
        )}
        {showOriginal ? original : usesSourceOffsets(item, blocks) && anchors(item)}
        {results(item)}
      </>
    );
  };
  const tocView = (
    <nav className="reading-outline" aria-label="论文章节">
      <div className="outline-label">本文目录</div>
      {toc.map((x) => {
        const parent = parentIds.get(x.id);
        if (parent && !isOpen(parent)) return null;
        const hasChildren = toc.some((c) => parentIds.get(c.id) === x.id);
        const zh = translationOf(x.id);
        return (
          <div key={x.id} className={`reading-toc-row level-${x.level}`}>
            <button
              className="toc-title"
              aria-current={current === x.id ? "location" : undefined}
              onClick={() => jump(x.id)}
              title={x.label}
            >
              <RichText text={zh && x.kind === "heading" ? zh : x.label} />
            </button>
            {hasChildren && (
              <button
                className="toc-fold"
                aria-label={`${isOpen(x.id) ? "折叠" : "展开"}${x.label}`}
                aria-expanded={isOpen(x.id)}
                onClick={() => setExpanded((old) => ({ ...old, [x.id]: !isOpen(x.id) }))}
              >
                {isOpen(x.id) ? "−" : "+"}
              </button>
            )}
          </div>
        );
      })}
    </nav>
  );
  const referenceOptions = reading.references.map((item) => ({
    id: item.id,
    label: referenceLabel(item),
    text: readingText(item, blocks),
  }));
  const matchingReferences = referenceOptions.filter((option) =>
    matchesReference(option, referenceQuery),
  );
  const matchingReferenceIds = new Set(matchingReferences.map((option) => option.id));
  const filteredRefs = reading.references.filter((item) =>
    matchingReferenceIds.has(item.id),
  );
  const infoParts = informationParts(reading, blocks);
  const infoKeys = new Set([
    ...Object.keys(infoParts),
    ...Object.keys(reading.information_items || {}),
  ]);
  const infoEntries = [...infoKeys].filter(
    (key) =>
      (reading.information_items?.[key]?.length ?? infoParts[key]?.length ?? 0) > 0,
  );
  const sliceText = (parts: ReadingPart[] | undefined) =>
    (parts || [])
      .map((p) => blocks.get(p.block_id)?.text.slice(p.start, p.end).trim())
      .filter(Boolean)
      .join("；");
  const title =
    reading.information_items?.titles?.[0]?.content ||
    sliceText(infoParts.titles?.slice(0, 1)) ||
    report.analysis.title ||
    report.document.title;
  const byline = [
    ...(reading.information_items?.authors || []).map((entry) =>
      readingText(entry, blocks),
    ),
    sliceText(infoParts.authors),
  ]
    .filter(Boolean)
    .join("；");
  const zhTitle = translationOf(TITLE_ID);
  const currentLabel = (() => {
    const entry = toc.find((t) => t.id === current);
    return (entry && translationOf(entry.id)) || entry?.label || "连续阅读";
  })();
  const previewBody = preview?.keys.map((key) => {
    if (preview.kind === "cite") {
      const ref = refs.get(key);
      return ref ? (
        <div className="xref-preview-item" key={key}>
          <b>[{key}]</b>{" "}
          <RichText text={referencePreview(displayReferenceText(readingText(ref, blocks))) || displayReferenceText(readingText(ref, blocks))} />
        </div>
      ) : null;
    }
    const item = targets.get(key);
    if (!item) return null;
    const zh = displayedTranslations[item.id];
    if (item.kind === "heading")
      return (
        <div className="xref-preview-item" key={key}>
          <b>
            <RichText text={zh || item.label || readingText(item, blocks)} />
          </b>
          <small>
            <RichText
              text={readingText(
                reading.items[reading.items.indexOf(item) + 1] || item,
                blocks,
              ).slice(0, 180)}
            />
            …
          </small>
        </div>
      );
    const formula =
      item.kind === "formula" ? (
        item.content != null ? (
          <RichText text={`$$${item.content}$$`} />
        ) : (
          (() => {
            const original = item.parts
              .map((part) => blocks.get(part.block_id)?.formula)
              .find((f) => f?.mathml || f?.latex);
            if (original?.latex) return <RichText text={`$$${original.latex}$$`} />;
            if (original?.mathml) {
              const html = sanitizeMathML(original.mathml);
              if (html)
                return (
                  <span
                    className="tex-display"
                    dangerouslySetInnerHTML={{ __html: html }}
                  />
                );
            }
            return item.transcription ? (
              <TranscriptionView transcription={item.transcription} ctx={links} />
            ) : null;
          })()
        )
      ) : null;
    return (
      <div className="xref-preview-item" key={key}>
        <b>
          <RichText
            text={
              zh || item.label || (item.kind === "formula" ? `公式 ${key.slice(3)}` : "")
            }
          />
        </b>
        {formula || (item.asset && <AssetImage src={item.asset} alt="" />)}
      </div>
    );
  });
  return (
    <section
      className={`continuous-reader ${source ? "has-source" : ""} ${noteMargin ? "has-note-margin" : ""}`}
      data-selectable="true"
      data-reading-font={preferences.font}
      data-language={language}
      style={
        {
          "--reading-size": `${preferences.size}px`,
          "--reading-measure": `${measure.value}em`,
          "--reading-leading": preferences.leading,
        } as CSSProperties
      }
    >
      {termSidebarSlot &&
        onOpenTerms &&
        onCloseTerms &&
        createPortal(
          <TermTranslationSidebar
            open={termsOpen}
            onOpen={onOpenTerms}
            rules={termReplacements.rules}
            candidates={termCandidates}
            onApply={(from, to) => {
              pendingLayout.current = capturePosition();
              termReplacements.apply(from, to);
            }}
            onRemove={(id) => {
              pendingLayout.current = capturePosition();
              termReplacements.remove(id);
            }}
            onClear={() => {
              pendingLayout.current = capturePosition();
              termReplacements.clear();
            }}
            onClose={onCloseTerms}
            error={termReplacements.error}
          />,
          termSidebarSlot,
        )}
      {outlineSlot ? createPortal(tocView, outlineSlot) : tocView}
      <div className="continuous-column">
        <div className="continuous-toolbar" ref={toolbar}>
          <div className="reading-progress" ref={progress} aria-hidden="true" />
          {offerLanguages && (
            <ReadingLanguageSwitch language={language} onChange={changeLanguage} />
          )}
          <span className="reading-breadcrumb" title={currentLabel}>
            {currentLabel}
          </span>
          {query && (
            <span className="reader-search-controls">
              {hits.length ? `${hit + 1}/${hits.length}` : "未找到"}
              <button
                className="text-button"
                disabled={!hits.length}
                onClick={() => {
                  const previous = stepSearchIndex(hit, hits.length, -1);
                  setHit(previous);
                  jump(hits[previous].id);
                }}
              >
                上一处
              </button>
              <button
                className="text-button"
                disabled={!hits.length}
                onClick={() => {
                  const next = stepSearchIndex(hit, hits.length, 1);
                  setHit(next);
                  jump(hits[next].id);
                }}
              >
                下一处
              </button>
              <button className="text-button" onClick={onClearQuery}>
                清除
              </button>
            </span>
          )}
          {returnTo && (
            <button
              className="text-button"
              onClick={() => {
                restorePosition(returnTo);
                setReturnTo(null);
                setLastCitation(null);
              }}
            >
              返回原处
            </button>
          )}
          {resume && resume.id !== "article-opening" && (
            <button
              className="text-button reading-resume"
              onClick={() => {
                restorePosition(resume);
                setResume(null);
              }}
            >
              继续阅读
            </button>
          )}
          {offerLanguages && canTranslate && language !== "original" && (
            <button
              className="text-button translate-section"
              disabled={translating || !untranslated.length}
              title="只翻译当前这一节，会调用你设置的对话模型"
              onClick={() => void translations.translate(untranslated)}
            >
              {translating
                ? `翻译中 ${translations.done}/${translations.total}`
                : untranslated.length
                  ? `翻译本节 · ${untranslated.length} 段`
                  : "本节已译"}
            </button>
          )}
          {toolbarExtra}
          <button
            className="button reading-source-toggle"
            aria-pressed={!!source}
            aria-label="核对原版 PDF"
            title="对照原版 PDF 页面，随阅读位置跟随"
            onClick={() => {
              pendingLayout.current = capturePosition();
              if (source) setSource(null);
              else {
                const item = reading.items.find((i) => i.id === readingAnchor?.id);
                setFollowing(true);
                setSource(
                  item
                    ? {
                        ...itemSource(item, blocks, readingAnchor?.part),
                        itemId: item.id,
                      }
                    : { page: 1 },
                );
              }
            }}
          >
            <Icon name="file" size={14} /> 原页
          </button>
          <button
            className="text-button reading-type-toggle"
            aria-label="排版"
            title="字号、行宽、行距与字体"
            aria-expanded={typeOpen}
            aria-controls="reading-typesetting"
            onClick={() => {
              pendingLayout.current = capturePosition();
              setTypeOpen((old) => !old);
            }}
          >
            Aa
          </button>
          <button className="icon-button" aria-label="导出全文" onClick={onExport}>
            <Icon name="download" size={16} />
          </button>
          {typeOpen && (
            <div
              className="reading-typesetting"
              id="reading-typesetting"
              aria-label="正文排版设置"
            >
              <label>
                字号{" "}
                <input
                  aria-label="正文字号"
                  type="range"
                  min="15"
                  max="24"
                  step="1"
                  value={preferences.size}
                  onChange={(e) => changePreferences({ size: Number(e.target.value) })}
                />
                <output>{preferences.size}</output>
              </label>
              <label>
                行宽{" "}
                <input
                  aria-label="正文行宽"
                  type="range"
                  min={measure.min}
                  max={measure.max}
                  step="1"
                  value={measure.value}
                  disabled={measure.disabled}
                  onChange={(e) => changePreferences({ measure: Number(e.target.value) })}
                />
                <output
                  title={
                    measure.fitted
                      ? `保留 ${preferences.measure} 字的宽度偏好`
                      : undefined
                  }
                >
                  {measure.value} 字{measure.fitted ? " · 适宽" : ""}
                </output>
              </label>
              <label>
                行距{" "}
                <input
                  aria-label="正文行距"
                  type="range"
                  min="1.5"
                  max="2.3"
                  step="0.1"
                  value={preferences.leading}
                  onChange={(e) => changePreferences({ leading: Number(e.target.value) })}
                />
                <output>{preferences.leading.toFixed(1)}</output>
              </label>
              <label>
                字体{" "}
                <select
                  aria-label="正文字体"
                  value={preferences.font}
                  onChange={(e) =>
                    changePreferences({
                      font: e.target.value === "sans" ? "sans" : "serif",
                    })
                  }
                >
                  <option value="serif">宋体</option>
                  <option value="sans">黑体</option>
                </select>
              </label>
            </div>
          )}
        </div>
        <div className="continuous-reading-body" ref={column}>
          {translations.error && (
            <div className="translation-error" role="alert">
              翻译没有完成：{translations.error}
              <button className="text-button" onClick={translations.clearError}>
                知道了
              </button>
            </div>
          )}
          <article
            className="continuous-prose"
            ref={prose}
            onClick={onProseClick}
            onPointerOver={(e) => {
              const link = linkTarget(e);
              if (link) showPreview(link);
            }}
            onPointerOut={(e) => {
              if (linkTarget(e)) scheduleHidePreview();
            }}
            onFocus={(e) => {
              const link = linkTarget(e);
              if (link) showPreview(link);
            }}
            onBlur={scheduleHidePreview}
          >
            <header className="article-heading" id="article-opening">
              <p className="article-kicker">
                全文 · {report.document.pages.length} 页
                {reading.references.length > 0 &&
                  ` · 参考文献 ${reading.references.length}`}
              </p>
              <h2 lang={zhTitle ? "zh-CN" : undefined}>
                <RichText text={zhTitle || title} />
              </h2>
              {zhTitle && (
                <p className="article-title-en" lang="en">
                  <RichText text={title} />
                </p>
              )}
              {byline && (
                <p className="article-byline">
                  <RichText text={byline} />
                </p>
              )}
            </header>
            {runs.map((run, index) => {
              const item = run[0];
              const pageStart = index === 0 || runs[index - 1].at(-1)?.page !== item.page;
              if (item.kind === "code") {
                const selected = run.find((part) => part.id === active);
                return (
                  <section key={item.id} className="reading-code-listing">
                    {pageStart && pageMark(item)}
                    <CodeBlock
                      {...codeListings.get(item.id)!}
                      label={item.label}
                      activeId={active}
                      currentId={readingAnchor?.id}
                    />
                    {selected && actions(selected)}
                    {run.map(inlineNotes)}
                  </section>
                );
              }
              return (
                <DeferredReadingSection
                  id={item.id}
                  key={item.id}
                  eager={
                    reading.items.length < 120 || index < 12 || item.kind === "heading"
                  }
                  estimatedHeight={Math.max(
                    90,
                    Math.ceil(readingText(item, blocks).length / 75) * 30 + 32,
                  )}
                  placeholderAnchors={anchors(item)}
                  className={`reading-item reading-${item.kind}`}
                  data-reading-current={
                    readingAnchor?.id === item.id ? "true" : undefined
                  }
                  data-active={active === item.id ? "true" : undefined}
                  data-translated={translationOf(item.id) ? "true" : undefined}
                  data-page-start={pageStart ? "true" : undefined}
                  render={() => (
                    <>
                      {pageStart && pageMark(item)}
                      {(!usesSourceOffsets(item, blocks) ||
                        ["figure", "table", "formula", "algorithm"].includes(
                          item.kind,
                        )) &&
                        anchors(item)}
                      {item.kind === "heading"
                        ? heading(item)
                        : ["figure", "table", "formula", "algorithm"].includes(item.kind)
                          ? object(item)
                          : paragraph(item)}
                      {active === item.id && actions(item)}
                      {inlineNotes(item)}
                    </>
                  )}
                />
              );
            })}
            {reading.references.length > 0 && (
              <section id="article-references" className="article-endnotes">
                <div className="endnotes-head">
                  <div>
                    <p className="eyebrow">延伸阅读</p>
                    <h3>
                      参考文献 <small>{reading.references.length}</small>
                    </h3>
                  </div>
                  <ReferenceSearch
                    query={referenceQuery}
                    onChange={setReferenceQuery}
                    matches={matchingReferences}
                    total={reading.references.length}
                    onSelect={(id) => {
                      setReferenceQuery("");
                      setPendingJump(id);
                    }}
                  />
                </div>
                {reading.references.length <= 20 &&
                  reading.references.some((r) => referenceNumber(r)) && (
                    <nav className="reference-index" aria-label="参考文献索引">
                      {reading.references
                        .filter((r) => referenceNumber(r))
                        .map((r) => (
                          <button
                            key={r.id}
                            title={displayReferenceText(readingText(r, blocks))}
                            onClick={() => {
                              setReferenceQuery("");
                              requestAnimationFrame(() => jump(r.id));
                            }}
                          >
                            {referenceNumber(r) ? `[${referenceNumber(r)}]` : r.label}
                          </button>
                        ))}
                    </nav>
                  )}
                <div className="reference-list">
                  {filteredRefs.map((r) => {
                    const text = readingText(r, blocks);
                    const display = displayReferenceText(text);
                    const preview = referencePreview(display);
                    const citation = text.startsWith(display) ? (
                      originalText(r, display.length, false)
                    ) : (
                      <RichText text={display} query={query} ctx={links} />
                    );
                    const label = referenceLabel(r);
                    const origin =
                      lastCitation?.referenceId === r.id ? lastCitation.position : null;
                    const mentions = backlinks.get(r.id) || [];
                    return (
                      <article
                        className="reference-card"
                        id={r.id}
                        key={r.id}
                        data-long-label={label.length > 12 ? "true" : undefined}
                      >
                        <div className="reference-number">{label}</div>
                        <div>
                          {anchors(r)}
                          {preview ? (
                            <details className="reference-citation reference-citation-fold">
                              <summary>
                                <span className="reference-citation-preview">{preview}</span>
                                <span className="reference-citation-expand">
                                  <span className="reference-expand-label">展开完整引文</span>
                                  <span className="reference-collapse-label">收起完整引文</span>
                                </span>
                              </summary>
                              <p>{citation}</p>
                            </details>
                          ) : (
                            <p className="reference-citation">{citation}</p>
                          )}
                          <div className="reference-actions">
                            <div
                              className="reference-action-group"
                              role="group"
                              aria-label="引用操作"
                            >
                              <ReferenceSourceActions text={text} href={r.href} preference={sourcePreference} />
                              <CitationCopyButton text={text} href={r.href} />
                              {canReferenceTree && paperId && (
                                <ReferenceTree
                                  paperId={paperId}
                                  referenceId={r.id}
                                  label={display}
                                />
                              )}
                              {origin ? (
                                <button
                                  className="text-button"
                                  onClick={() => {
                                    restorePosition(origin);
                                    setLastCitation(null);
                                    setReturnTo(null);
                                  }}
                                >
                                  返回引用
                                </button>
                              ) : mentions.length ? (
                                <button
                                  className="text-button"
                                  aria-expanded={
                                    mentions.length > 1
                                      ? backlinkChoice === r.id
                                      : undefined
                                  }
                                  onClick={() =>
                                    mentions.length === 1
                                      ? goToBacklink(mentions[0])
                                      : setBacklinkChoice((old) =>
                                          old === r.id ? null : r.id,
                                        )
                                  }
                                >
                                  正文引用 · {mentions.length} 处
                                </button>
                              ) : (
                                <span className="reference-no-citations">
                                  未找到正文引用
                                </span>
                              )}
                            </div>
                            <div
                              className="reference-action-group"
                              role="group"
                              aria-label="阅读操作"
                            >
                              {sourceButton(r)}
                              {onCreateNote && (
                                <>
                                  <button
                                    className="text-button"
                                    onClick={() => onCreateNote(r, "note")}
                                  >
                                    笔记
                                  </button>
                                </>
                              )}
                              {onCheckItem && (
                                <button
                                  className="text-button"
                                  onClick={() => onCheckItem(r)}
                                >
                                  核对原文
                                </button>
                              )}
                            </div>
                          </div>
                          {backlinkChoice === r.id && mentions.length > 1 && (
                            <div
                              className="reference-backlinks"
                              role="group"
                              aria-label="正文引用位置"
                            >
                              <p>选择引用位置（定位到对应段落）</p>
                              {mentions.map((mention, index) => (
                                <button
                                  key={`${mention.itemId}-${mention.start}`}
                                  type="button"
                                  className="reference-backlink-item"
                                  onClick={() => goToBacklink(mention)}
                                >
                                  <strong>
                                    PDF {mention.page} · 第 {index + 1} 处
                                  </strong>
                                  <span>{mention.excerpt}</span>
                                </button>
                              ))}
                            </div>
                          )}
                          {inlineNotes(r)}
                        </div>
                      </article>
                    );
                  })}
                </div>
              </section>
            )}
            <section id="article-information" className="article-endnotes">
              <div className="endnotes-head">
                <div>
                  <p className="eyebrow">保留原文记录</p>
                  <h3>论文信息</h3>
                </div>
              </div>
              {infoEntries.length
                ? infoEntries.map((key) => (
                    <details key={key} className="paper-information">
                      <summary>{labels[key] || key}</summary>
                      {reading.information_items?.[key]
                        ? reading.information_items[key].map((item) => (
                            <div key={item.id} id={item.id}>
                              {!usesSourceOffsets(item, blocks) && anchors(item)}
                              <p>{originalText(item)}</p>
                              {sourceButton(item)}
                              {onCreateNote && (
                                <button
                                  className="text-button"
                                  onClick={() => onCreateNote(item, "note")}
                                >
                                  笔记
                                </button>
                              )}
                              {inlineNotes(item)}
                            </div>
                          ))
                        : infoParts[key].map((part) => {
                            const b = blocks.get(part.block_id);
                            return b ? (
                              <div key={`${part.block_id}-${part.start}`}>
                                <p>{span(part)}</p>
                                <button
                                  className="text-button"
                                  onClick={() =>
                                    openSource({ page: b.page, box: part.bbox ?? b.bbox })
                                  }
                                >
                                  {sourceLabel(report, [b.page])}
                                </button>
                              </div>
                            ) : null;
                          })}
                    </details>
                  ))
                : null}
            </section>
          </article>
          {noteMargin && <ReadingMargin column={column} groups={noteGroups} />}
        </div>
      </div>
      {preview && previewBody && (
        <div
          className={`xref-preview ${preview.below ? "below" : "above"}`}
          role="tooltip"
          onPointerEnter={() => clearTimeout(previewTimer.current)}
          onPointerLeave={scheduleHidePreview}
          style={{ left: preview.left, top: preview.top }}
        >
          {previewBody}
        </div>
      )}
      {citationChoice && (
        <div
          className="xref-preview citation-choice"
          role="dialog"
          aria-label="选择参考文献"
          style={{ left: citationChoice.left, top: citationChoice.top }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              closeCitationChoice();
            }
          }}
        >
          <button ref={choiceClose} type="button" onClick={closeCitationChoice}>
            关闭
          </button>
          {citationChoice.keys.map((key) => {
            const ref = refs.get(key);
            return (
              ref && (
                <button
                  key={key}
                  type="button"
                  className="citation-choice-item"
                  onClick={() => cite(key, citationChoice.from, citationChoice.position)}
                >
                  <b>[{key}]</b> {displayReferenceText(readingText(ref, blocks)).slice(0, 200)}
                </button>
              )
            );
          })}
        </div>
      )}
      {source && (
        <aside className="source-inspector" aria-label="原版 PDF 核对">
          <PdfSource
            report={report}
            page={source.page}
            box={source.box}
            following={following}
            onFollowChange={setFollowing}
            onNavigate={(page) => {
              setFollowing(false);
              setSource({ page });
            }}
            onManualScroll={() => setFollowing(false)}
            onClose={() => {
              pendingLayout.current = capturePosition();
              setSource(null);
            }}
          />
        </aside>
      )}
      {selectionTool}
      {image && (
        <ImageViewer
          src={image.asset}
          alt={image.label || "原文公式或图表"}
          onClose={() => setImage(null)}
          onSource={() => openPage(image, image.page)}
        />
      )}
    </section>
  );
}
