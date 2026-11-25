/* Cross-references and rich text for the continuous reader.
 *
 * In-text mentions — "[12]", "Figure 3", "式 (2)", "Section 3.2", "附录 A" —
 * become buttons that jump to their target and preview it on hover, the way
 * EasyRead's render.js links them (MIT). Linking never changes the words:
 * a raw-text span keeps exactly the source characters, so selection offsets
 * and annotations stay valid. TeX is rendered outside source-selection spans; adjacent prose keeps its
 * original UTF-16 coordinates.
 */
import { Fragment, type ReactNode } from "react";
import { splitTex, texRanges, texToMathML } from "./tex";
import type { Block, ReadingItem } from "./types";
import { readingText, referenceNumber } from "./reading";
import { orderedReferenceSources, preferredReferenceSource, referenceSources } from "./referenceSources";

/** Mentions of a numbered object, or a bracketed citation list. */
export const XREF_PATTERN =
  /((?:[\p{L}][\p{L}\p{M}'’-]+)(?:\s+et\s+al\.?|\s+(?:and|&)\s+[\p{L}][\p{L}\p{M}'’-]+)?\s*\((?:18|19|20)\d{2}[a-z]?(?:\s*,\s*(?:(?:18|19|20)\d{2}[a-z]?|[a-z]))*\)|\[[^\[\]]{1,500}\]|\([^()]{1,200}\)|(?:Figures?|Figs?\.|图|Tables?|Tab\.|表|Equations?|Eqs?\.|公式|式|Algorithms?|Alg\.|算法)[ \t]*[(（]?\d+(?:\.\d+)?[a-z]?[)）]?(?:[ \t]*[~～–—-][ \t]*[(（]?\d+[)）]?)?|(?:Sections?|Sec\.|§|第)[ \t]*\d+(?:\.\d+)*[ \t]*(?:节|章)?|(?:Appendix|附录)[ \t]*[A-Z](?:\.\d+)*)/giu;

const KINDS: [RegExp, string][] = [
  [/^(?:figures?|figs?\.|图)/i, "fig"],
  [/^(?:tables?|tab\.|表)/i, "tab"],
  [/^(?:equations?|eqs?\.|公式|式)/i, "eq"],
  [/^(?:algorithms?|alg\.|算法)/i, "alg"],
  [/^(?:sections?|sec\.|§|第)/i, "sec"],
  [/^(?:appendix|附录)/i, "app"],
];

/** "Fig. 3" → "fig:3", "式 (2)" → "eq:2", "第 3.2 节" → "sec:3.2"; null for anything else. */
export function xrefKey(mention: string): string | null {
  const text = mention.trim();
  const kind = KINDS.find(([re]) => re.test(text))?.[1];
  if (!kind) return null;
  if (kind === "app") {
    const id = text.match(/[A-Z](?:\.\d+)*$/)?.[0];
    return id ? `app:${id}` : null;
  }
  const id = text.match(/\d+(?:\.\d+)*[a-z]?/i)?.[0];
  // "第 3 章" counts as a section; a bare "第 3" (e.g. "第 3 种") does not.
  if (kind === "sec" && /^第/.test(text) && !/[节章]$/.test(text)) return null;
  return id ? `${kind}:${id.toLowerCase()}` : null;
}

/** Expand numbered ranges against existing targets, without allocating huge ranges. */
export function citationNumbers(mention: string, available?: Iterable<string>): string[] {
  const ranges = [...mention.matchAll(/(\d+)(?:\s*[–—~～-]\s*[(（]?(\d+)[)）]?)?/g)].map(
    (m) => [Number(m[1]), Number(m[2] || m[1])],
  );
  if (available)
    return [...available]
      .filter((id) => ranges.some(([a, b]) => Number(id) >= a && Number(id) <= b))
      .sort((a, b) => Number(a) - Number(b));
  const out: string[] = [];
  for (const [a, b] of ranges) {
    if (b >= a && b - a <= 10000) for (let n = a; n <= b; n++) out.push(String(n));
    else out.push(String(a));
  }
  return [...new Set(out)];
}

const normalizedReferenceKey = (value: string) =>
  value.normalize("NFKD").replace(/\p{M}/gu, "").replace(/\s+/g, "").toLowerCase();

interface CitationSpan {
  start: number;
  end: number;
  key: string;
}

/** Resolve author/year citations to individual original-text spans. */
function authorYearSpans(mention: string, available: string[]): CitationSpan[] {
  const byKey = new Map<string, string | null>();
  for (const key of available) {
    const normalized = normalizedReferenceKey(key);
    if (byKey.has(normalized) && byKey.get(normalized) !== key)
      byKey.set(normalized, null);
    else if (!byKey.has(normalized)) byKey.set(normalized, key);
  }
  const spans: CitationSpan[] = [];
  const narrative = mention.match(/^(.+?)\s*\(([^()]*)\)$/u);
  const inner = narrative ? narrative[2] : mention.slice(1, -1);
  const offset = narrative ? mention.indexOf("(") + 1 : 1;
  for (const group of inner.matchAll(/[^;；]+/gu)) {
    const prefix = narrative ? narrative[1] + ", " : "";
    const value = prefix + group[0];
    const author = value.match(
      /^\s*(?:(?:\p{L}\.\s*)+)?([\p{L}][\p{L}\p{M}'’-]*)(?:\s+(?:et\s+al\.?|(?:and|&)\s+([\p{L}][\p{L}\p{M}'’-]*)))?\s*[,，]?\s*((?:18|19|20)\d{2}[a-z]?)(?=\s|[,，]|$)/iu,
    );
    if (!author) continue;
    const firstYear = author[0].lastIndexOf(author[3]);
    const remaining = value.slice(firstYear);
    const groupOffset = offset + (group.index || 0) - prefix.length + firstYear;
    for (const year of remaining.matchAll(/\b((?:18|19|20)\d{2})([a-z]?)\b/giu)) {
      const start = groupOffset + (year.index || 0);
      const lookup = (suffix: string) =>
        byKey.get(
          normalizedReferenceKey(
            `${author[1]}${author[2] ? `&${author[2]}` : ""}${year[1]}${suffix}`,
          ),
        );
      const key = lookup(year[2]);
      if (key && start >= 0) spans.push({ start, end: start + year[0].length, key });
      let tail = remaining.slice((year.index || 0) + year[0].length);
      let consumed = 0;
      for (;;) {
        const suffix = tail.match(/^\s*[,，]\s*([a-z])(?=\s*(?:[,，;；]|$))/iu);
        if (!suffix) break;
        const suffixKey = lookup(suffix[1]);
        if (suffixKey) {
          const suffixStart =
            start + year[0].length + consumed + suffix[0].lastIndexOf(suffix[1]);
          spans.push({
            start: suffixStart,
            end: suffixStart + suffix[1].length,
            key: suffixKey,
          });
        }
        consumed += suffix[0].length;
        tail = tail.slice(suffix[0].length);
      }
    }
  }
  return spans;
}

/** Brackets are candidates only. Resolve exact labels before scanning local objects. */
export function citationTargets(mention: string, available: Iterable<string>): string[] {
  const keys = [...available];
  if (/^\[[\d\s,，–—~～-]+\]$/.test(mention)) return citationNumbers(mention, keys);
  const authored = authorYearSpans(mention, keys);
  if (authored.length) return [...new Set(authored.map((span) => span.key))];
  if (!mention.startsWith("[")) return [];
  const tokens = mention
    .slice(1, -1)
    .split(/[;,，；]/)
    .map((token) => token.replace(/\s+/g, ""));
  return [...new Set(tokens.filter((token) => keys.includes(token)))];
}

/** An ambiguous label/author-year pair is deliberately not a destination. */
export function referenceIndex(
  items: ReadingItem[],
  blocks: Map<string, Block>,
): Map<string, ReadingItem> {
  const found = new Map<string, { key: string; item: ReadingItem } | null>();
  const add = (key: string, item: ReadingItem) => {
    const normalized = normalizedReferenceKey(key);
    if (found.has(normalized) && found.get(normalized)?.item.id !== item.id)
      found.set(normalized, null);
    else if (!found.has(normalized)) found.set(normalized, { key, item });
  };
  for (const item of items) {
    const text = readingText(item, blocks).trim();
    const inlineLabel = text.match(/^\s*\[(\d{1,5}|[A-Z][A-Za-z0-9]{0,23}\+?)\]/)?.[1];
    const label = (referenceNumber(item) || inlineLabel || "").replace(/\s+/g, "");
    if (/^(?:\d{1,5}|[A-Z][A-Za-z0-9]{0,23}\+?)$/.test(label)) add(label, item);
    const unlabelled = text.replace(/^\s*\[[^\]]+\]\s*/, "");
    const first =
      unlabelled.match(/^(?:\p{L}\.\s*(?:[-‐‑]\s*)?)+([\p{L}][\p{L}\p{M}'’-]*)/u) ||
      unlabelled.match(/^([\p{L}][\p{L}\p{M}'’-]*)(?=,\s|\.\s)/u);
    const author = first?.[1];
    const second =
      first &&
      unlabelled
        .slice(first[0].length)
        .match(/^\s+(?:and|&)\s+(?:(?:\p{L}\.\s*)+)?([\p{L}][\p{L}\p{M}'’-]*)/iu)?.[1];
    const publication = unlabelled
      .replace(/https?:\/\/\S+/giu, "")
      .replace(/\b(?:accessed|retrieved)\b.*$/iu, "");
    const year = [...publication.matchAll(/\b(?:18|19|20)\d{2}[a-z]?\b/giu)].at(-1)?.[0];
    if (author && year) {
      add(author + year, item);
      if (second) add(`${author}&${second}${year}`, item);
    }
  }
  return new Map(
    [...found.values()]
      .filter((entry): entry is { key: string; item: ReadingItem } => entry !== null)
      .map(({ key, item }) => [key, item]),
  );
}

/** Only an explicit identifier or URL becomes a direct destination. */
export function referenceDestination(text: string, existing = "") {
  const selected = preferredReferenceSource(orderedReferenceSources(referenceSources(text, existing)));
  return { href: selected.href, direct: selected.kind !== "search" };
}

/** Range syntax requires an explicit object prefix; ordinary a~b stays prose. */
export function xrefKeys(mention: string, available: Iterable<string>): string[] {
  const first = xrefKey(mention);
  if (!first) return [];
  const range = mention.match(/[(（]?(\d+)[)）]?\s*[~～–—-]\s*[(（]?(\d+)[)）]?$/);
  if (!range) return [...available].includes(first) ? [first] : [];
  const prefix = first.slice(0, first.indexOf(":") + 1);
  const a = Number(range[1]),
    b = Number(range[2]);
  return [...available]
    .filter(
      (key) =>
        key.startsWith(prefix) &&
        /^\d+$/.test(key.slice(prefix.length)) &&
        Number(key.slice(prefix.length)) >= a &&
        Number(key.slice(prefix.length)) <= b,
    )
    .sort((x, y) => Number(x.slice(prefix.length)) - Number(y.slice(prefix.length)));
}

/** The key under which another passage may mention this item. */
export function itemXrefKey(
  item: ReadingItem,
  blocks: Map<string, Block>,
): string | null {
  const label = item.label.trim();
  if (item.kind === "heading") {
    const text = label || readingText(item, blocks).trim();
    const appendix = text.match(/^(?:Appendix|附录)\s*([A-Z](?:\.\d+)*)/i);
    if (appendix) return `app:${appendix[1]}`;
    const number = text.match(/^(?:第\s*)?(\d+(?:\.\d+)*)(?:\s*[节章])?[.\s、]/);
    return number ? `sec:${number[1]}` : null;
  }
  if (item.kind === "formula") {
    const number = (label || readingText(item, blocks)).match(
      /[(（](\d+(?:\.\d+)?[a-z]?)[)）]\s*$/i,
    );
    return number ? `eq:${number[1].toLowerCase()}` : null;
  }
  if (["figure", "table", "algorithm"].includes(item.kind)) {
    const key = xrefKey(label.match(XREF_SINGLE)?.[0] || "");
    if (key) return key;
    const number = label.match(/^\D{0,12}?(\d+)/)?.[1];
    const prefix = (
      { figure: "fig", table: "tab", algorithm: "alg" } as Record<string, string>
    )[item.kind];
    return number ? `${prefix}:${number}` : null;
  }
  return null;
}
const XREF_SINGLE = new RegExp(`^${XREF_PATTERN.source}`, "i");

/** key → target item; the first item wins, so a later re-mention never steals it. */
export function xrefIndex(items: ReadingItem[], blocks: Map<string, Block>) {
  const out = new Map<string, ReadingItem>();
  for (const item of items) {
    const key = itemXrefKey(item, blocks);
    if (key && !out.has(key)) out.set(key, item);
  }
  return out;
}

export interface LinkContext {
  refs: Map<string, unknown>;
  targets: Map<string, ReadingItem>;
}

function resolveCitation(value: string, before: string, after: string, keys: string[]) {
  const spans = authorYearSpans(value, keys);
  if (spans.length) return { spans, keys: [...new Set(spans.map((span) => span.key))] };
  if (!value.startsWith("[") && !value.startsWith("(")) return null;
  // Explicit mathematical context is not evidence of a bibliography citation.
  if (
    /^\[[\d\s,，–—~～-]+\]$/.test(value) &&
    (/^\[\s*(?:-|0\s*[,，–—~～-])/u.test(value) ||
      /(?:[=<>≤≥∈∉±×÷+*/^\[]|\b(?:array|matrix|interval|vector|range|index|shape))\s*$/iu.test(
        before,
      ) ||
      /^\s*[=+*/^]/u.test(after))
  )
    return null;
  const found = citationTargets(value, keys);
  return found.length ? { spans, keys: found } : null;
}

export interface CitationBacklink {
  itemId: string;
  page: number;
  start: number;
  excerpt: string;
}

/** Map each resolved body mention back to its item, including every member of a range. */
export function referenceBacklinks(
  items: ReadingItem[],
  blocks: Map<string, Block>,
  refs: Map<string, ReadingItem>,
): Map<string, CitationBacklink[]> {
  const out = new Map<string, CitationBacklink[]>();
  const keys = [...refs.keys()];
  for (const item of items) {
    // Listings and equations are not prose; bracketed indices there are not citations.
    if (["code", "formula", "algorithm"].includes(item.kind)) continue;
    const text = readingText(item, blocks);
    const ranges = [
      ...texRanges(text).map(({ start, end }) => ({ start, end })),
      ...[...text.matchAll(/`+[^`]*`+/g)].map((m) => ({
        start: m.index,
        end: m.index + m[0].length,
      })),
    ].sort((a, b) => a.start - b.start);
    const scan = (prose: string, offset: number) => {
      const pieces = prose.split(XREF_PATTERN);
      let cursor = offset;
      for (let i = 0; i < pieces.length; i++) {
        const value = pieces[i];
        if (i % 2 && value) {
          const match = resolveCitation(
            value,
            pieces[i - 1] || "",
            pieces[i + 1] || "",
            keys,
          );
          if (match) {
            const mentions = match.spans.length
              ? match.spans.map((span) => ({
                  start: span.start,
                  end: span.end,
                  keys: [span.key],
                }))
              : [{ start: 0, end: value.length, keys: match.keys }];
            for (const mention of mentions) {
              const start = cursor + mention.start;
              const end = cursor + mention.end;
              const excerpt = `${start > 60 ? "…" : ""}${text
                .slice(Math.max(0, start - 60), Math.min(text.length, end + 60))
                .replace(/\s+/g, " ")
                .trim()}${end + 60 < text.length ? "…" : ""}`;
              for (const id of new Set(mention.keys.map((key) => refs.get(key)?.id))) {
                if (!id) continue;
                const entries = out.get(id) || [];
                entries.push({ itemId: item.id, page: item.page, start, excerpt });
                out.set(id, entries);
              }
            }
          }
        }
        cursor += value.length;
      }
    };
    let cursor = 0;
    for (const range of ranges) {
      if (range.start > cursor) scan(text.slice(cursor, range.start), cursor);
      cursor = Math.max(cursor, range.end);
    }
    if (cursor < text.length) scan(text.slice(cursor), cursor);
  }
  return out;
}

/** Text with linkable mentions as buttons; the characters are unchanged. */
export function linkify(text: string, ctx: LinkContext): ReactNode[] {
  const pieces = text.split(XREF_PATTERN);
  return pieces.map((value, k) => {
    if (k % 2 === 0 || !value) return value;
    const found = resolveCitation(value, pieces[k - 1] || "", pieces[k + 1] || "", [
      ...ctx.refs.keys(),
    ]);
    const spans = found?.spans || [];
    if (spans.length) {
      const linked: ReactNode[] = [];
      let cursor = 0;
      for (const span of spans) {
        if (span.start < cursor || span.end > value.length) continue;
        linked.push(value.slice(cursor, span.start));
        linked.push(
          <button
            key={`${k}-${span.start}`}
            type="button"
            className="citation-link"
            data-cite={span.key}
          >
            {value.slice(span.start, span.end)}
          </button>,
        );
        cursor = span.end;
      }
      linked.push(value.slice(cursor));
      return <Fragment key={k}>{linked}</Fragment>;
    }
    if (value.startsWith("[") || value.startsWith("(")) {
      const numbers = found?.keys || [];
      if (!numbers.some((n) => ctx.refs.has(n))) return value;
      if (!/^\[[\d\s,，]+\]$/.test(value))
        return (
          <button
            key={k}
            type="button"
            className="citation-link"
            data-cite={numbers[0]}
            data-cites={numbers.join(" ")}
            title="定位文末参考文献；括号内章节或定理属于所引文献，未定位外文原页。"
          >
            {value}
          </button>
        );
      // Each number is its own target; brackets and commas stay plain text.
      return (
        <Fragment key={k}>
          {value.split(/(\d+)/).map((token, n) =>
            /^\d+$/.test(token) && ctx.refs.has(token) ? (
              <button key={n} type="button" className="citation-link" data-cite={token}>
                {token}
              </button>
            ) : (
              token
            ),
          )}
        </Fragment>
      );
    }
    const keys = xrefKeys(value, ctx.targets.keys());
    if (!keys.length) return value;
    return (
      <button
        key={k}
        type="button"
        className="citation-link xref-link"
        data-xref={keys[0]}
        data-xrefs={keys.join(" ")}
      >
        {value}
      </button>
    );
  });
}

/** Prose that may carry `$TeX$`: translations and corrected text. */
export function RichText({
  text,
  ctx,
  query = "",
}: {
  text: string;
  ctx?: LinkContext;
  query?: string;
}) {
  const prose = (value: string) => {
    const index = query.trim()
      ? value.toLocaleLowerCase().indexOf(query.trim().toLocaleLowerCase())
      : -1;
    const linked = (part: string) => (ctx ? linkify(part, ctx) : part);
    return index < 0 ? (
      linked(value)
    ) : (
      <>
        {linked(value.slice(0, index))}
        <mark>{linked(value.slice(index, index + query.trim().length))}</mark>
        {linked(value.slice(index + query.trim().length))}
      </>
    );
  };
  return (
    <>
      {splitTex(text).map((piece, k) => {
        if ("text" in piece) return <Fragment key={k}>{prose(piece.text)}</Fragment>;
        const math = texToMathML(piece.tex, piece.display);
        if (!math)
          return (
            <code key={k} className="tex-source">
              {piece.tex}
            </code>
          );
        return (
          <span
            key={k}
            className={piece.display ? "tex-display" : "tex-inline"}
            // Sanitised MathML only (tex.ts whitelists every element and attribute).
            dangerouslySetInnerHTML={{ __html: math }}
          />
        );
      })}
    </>
  );
}

/** "表 1 实验数据 Table 1 Experimental datasets" → number, rest, and a second-language line. */
export function splitCaption(label: string): {
  tag: string;
  rest: string;
  other: string;
} {
  const text = label.trim();
  const head = text.match(
    /^((?:Figure|Fig\.?|图|Table|Tab\.?|表|Algorithm|Alg\.?|算法)\s*\d+[a-z]?)\s*[.:：．、]?\s*/i,
  );
  if (!head) return { tag: "", rest: text, other: "" };
  let rest = text.slice(head[0].length);
  let other = "";
  // A Chinese caption followed by its English twin, as many Chinese journals print.
  const twin = rest.match(
    /\s((?:Figure|Fig\.?|Table|Tab\.?|Algorithm|Alg\.?)\s*\d+[\s\S]*)$/i,
  );
  if (twin && /[一-鿿]/.test(head[1])) {
    other = twin[1].trim();
    rest = rest.slice(0, twin.index).trim();
  }
  return { tag: head[1], rest, other };
}

/** True when most of the body is already Chinese, so translation adds nothing. */
export function mostlyChinese(text: string): boolean {
  const han = (text.match(/[一-鿿]/g) || []).length;
  const latin = (text.match(/[A-Za-z]/g) || []).length;
  return han > 40 && han * 2 > latin;
}
