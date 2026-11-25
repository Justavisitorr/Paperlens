import { texRanges } from "./tex";

/** Browser-wide display preferences. The model's saved text remains untouched. */
export interface TermReplacementRule {
  id: string;
  source: string;
  replacement: string;
}

/** Storage is untrusted; keep the first valid rule for each source and id. */
export function normalizeRules(input: unknown): TermReplacementRule[] {
  if (!Array.isArray(input)) return [];
  const sources = new Set<string>();
  const ids = new Set<string>();
  const rules: TermReplacementRule[] = [];
  for (const entry of input) {
    if (!entry || typeof entry !== "object") continue;
    const { id, source, replacement } = entry;
    if (
      typeof id !== "string" ||
      typeof source !== "string" ||
      typeof replacement !== "string"
    )
      continue;
    const rule = {
      id: id.trim(),
      source: source.trim(),
      replacement: replacement.trim(),
    };
    if (
      !rule.id ||
      !rule.source ||
      !rule.replacement ||
      rule.source === rule.replacement ||
      ids.has(rule.id) ||
      sources.has(rule.source)
    )
      continue;
    ids.add(rule.id);
    sources.add(rule.source);
    rules.push(rule);
  }
  return rules;
}

interface Range {
  start: number;
  end: number;
}

function mergeRanges(ranges: Range[]): Range[] {
  const merged: Range[] = [];
  for (const range of ranges.sort((a, b) => a.start - b.start || b.end - a.end)) {
    const last = merged.at(-1);
    if (last && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else merged.push({ ...range });
  }
  return merged;
}

function outsideRanges(text: string, ranges: Range[]): Range[] {
  const gaps: Range[] = [];
  let start = 0;
  for (const range of ranges) {
    if (range.start > start) gaps.push({ start, end: range.start });
    start = range.end;
  }
  if (start < text.length) gaps.push({ start, end: text.length });
  return gaps;
}

function escaped(text: string, position: number): boolean {
  let slashes = 0;
  while (position > 0 && text[--position] === "\\") slashes++;
  return slashes % 2 === 1;
}

/** Protect syntax and literal code before applying any user-supplied text. */
function protectedRanges(text: string): Range[] {
  const ranges: Range[] = [];
  let fence: { start: number; marker: string } | undefined;
  for (const match of text.matchAll(/^ {0,3}(`{3,}|~{3,})([^\n]*)(?:\n|$)/gm)) {
    const marker = match[1];
    if (!fence) {
      if (marker[0] === "`" && match[2].includes("`")) continue;
      fence = { start: match.index, marker };
    } else if (
      marker[0] === fence.marker[0] &&
      marker.length >= fence.marker.length &&
      !match[2].trim()
    ) {
      ranges.push({ start: fence.start, end: match.index + match[0].length });
      fence = undefined;
    }
  }
  if (fence) ranges.push({ start: fence.start, end: text.length });

  // Backtick spans close only with a run of the same length.
  for (const gap of outsideRanges(text, ranges)) {
    const runs = [...text.slice(gap.start, gap.end).matchAll(/`+/g)];
    for (let i = 0; i < runs.length; i++) {
      if (escaped(text, gap.start + runs[i].index)) continue;
      const closing = runs.findIndex(
        (run, j) => j > i && run[0].length === runs[i][0].length,
      );
      if (closing < 0) continue;
      ranges.push({
        start: gap.start + runs[i].index,
        end: gap.start + runs[closing].index + runs[closing][0].length,
      });
      i = closing;
    }
  }

  // Parse formulas only outside code so code dollars cannot consume adjacent prose.
  for (const gap of outsideRanges(text, mergeRanges(ranges))) {
    for (const range of texRanges(text.slice(gap.start, gap.end)))
      ranges.push({ start: gap.start + range.start, end: gap.start + range.end });
  }

  // Keep inline link destinations/title syntax intact, including balanced URLs.
  for (let i = 0; i < text.length - 1; i++) {
    if (text[i] !== "]" || escaped(text, i)) continue;
    if (text[i + 1] === "[") {
      const end = text.indexOf("]", i + 2);
      if (end >= 0 && !text.slice(i + 2, end).includes("\n"))
        ranges.push({ start: i + 1, end: end + 1 });
      continue;
    }
    if (text[i + 1] !== "(") continue;
    let depth = 1;
    let quote = "";
    let j = i + 2;
    for (; j < text.length && depth; j++) {
      if (escaped(text, j)) continue;
      if (quote) {
        if (text[j] === quote) quote = "";
        continue;
      }
      if ((text[j] === '"' || text[j] === "'") && /\s/.test(text[j - 1])) {
        quote = text[j];
        continue;
      }
      if (text[j] === "<" && !text.slice(i + 2, j).trim()) {
        quote = ">";
        continue;
      }
      if (text[j] === "(") depth++;
      if (text[j] === ")") depth--;
    }
    if (!depth) ranges.push({ start: i + 1, end: j });
  }
  for (const match of text.matchAll(/^ {0,3}\[[^\]\n]+\]:[^\n]*/gm))
    ranges.push({ start: match.index, end: match.index + match[0].length });
  for (const match of text.matchAll(/<(?:https?:\/\/|mailto:)[^>\n]+>/g))
    ranges.push({ start: match.index, end: match.index + match[0].length });
  return mergeRanges(ranges);
}

const latinWord = /[\p{Script=Latin}\p{M}\p{N}_]/u;
function fitsWord(text: string, at: number, source: string): boolean {
  const before = text[at - 1] || "";
  const after = text[at + source.length] || "";
  return !(
    (latinWord.test(source[0]) && latinWord.test(before)) ||
    (latinWord.test(source.at(-1)!) && latinWord.test(after))
  );
}

/** Literal, case-sensitive, longest-first matches against the original text only. */
export function replaceTranslationTerms(
  text: string,
  rules: readonly TermReplacementRule[],
): string {
  if (!text || !rules.length) return text;
  const ordered = normalizeRules(rules).sort((a, b) => b.source.length - a.source.length);
  if (!ordered.length) return text;
  const protectedParts = protectedRanges(text);
  let protectedIndex = 0;
  let position = 0;
  const output: string[] = [];
  while (position < text.length) {
    const protectedPart = protectedParts[protectedIndex];
    if (protectedPart && position === protectedPart.start) {
      output.push(text.slice(position, protectedPart.end));
      position = protectedPart.end;
      protectedIndex++;
      continue;
    }
    const limit = protectedPart?.start ?? text.length;
    const rule = ordered.find(
      (candidate) =>
        position + candidate.source.length <= limit &&
        text.startsWith(candidate.source, position) &&
        fitsWord(text, position, candidate.source),
    );
    if (rule) {
      output.push(rule.replacement);
      position += rule.source.length;
    } else output.push(text[position++]);
  }
  return output.join("");
}

export interface TermCandidate {
  source: string;
  term?: string;
  /** Whether this literal source occurs in currently available translated prose. */
  matched: boolean;
  /** Full original glossary labels, retained even when aliases share a source. */
  glossaryTerms: string[];
}

/** Suggestions use explicit bilingual pairs, never an inferred translation. */
export function discoverTermCandidates(
  glossary: readonly { term: string }[],
  translations: Record<string, { text: string }>,
): TermCandidate[] {
  const texts = Object.values(translations).map((entry) => entry.text);
  const candidates: TermCandidate[] = [];
  const bySource = new Map<string, TermCandidate>();
  const add = (
    source: string,
    glossaryTerms: readonly string[],
    term?: string,
    matchedOnly = false,
  ) => {
    source = source.trim();
    if (!source) return;
    let candidate = bySource.get(source);
    const rule = { id: "candidate", source, replacement: source + "\u0000" };
    const matched =
      candidate?.matched ??
      texts.some((text) => replaceTranslationTerms(text, [rule]) !== text);
    if (matchedOnly && !matched) return;
    if (!candidate) {
      candidate = { source, ...(term ? { term } : {}), matched, glossaryTerms: [] };
      bySource.set(source, candidate);
      candidates.push(candidate);
    } else if (!candidate.term && term) candidate.term = term;
    for (const label of glossaryTerms)
      if (!candidate.glossaryTerms.includes(label)) candidate.glossaryTerms.push(label);
  };
  const hasHan = (value: string) => /\p{Script=Han}/u.test(value);
  const latinTerm = (value: string) =>
    /^[\p{Script=Latin}\d][\p{Script=Latin}\d ._+\-/–—]{0,79}$/u.test(value);
  const paired = new Map<string, { term: string; glossaryTerms: string[] }>();
  const rememberEnglish = (term: string, label: string) => {
    const key = term.toLowerCase();
    const existing = paired.get(key);
    if (existing) {
      if (!existing.glossaryTerms.includes(label)) existing.glossaryTerms.push(label);
    } else paired.set(key, { term, glossaryTerms: [label] });
  };
  for (const entry of glossary) {
    if (typeof entry?.term !== "string" || !entry.term.trim()) continue;
    // A spaced slash explicitly lists aliases; preserve slashes within a term.
    const aliases = entry.term
      .split(/ [\/／] /)
      .map((alias) => alias.trim())
      .filter(Boolean);
    for (const term of aliases.length ? aliases : [entry.term.trim()]) {
      const pair = term.match(/^([^()（）]{1,80}?)\s*[(（]([^()（）]{1,80})[)）]$/);
      if (pair) {
        const first = pair[1].trim(),
          second = pair[2].trim();
        const zh =
          hasHan(first) && latinTerm(second)
            ? first
            : hasHan(second) && latinTerm(first)
              ? second
              : "";
        const en = zh === first ? second : first;
        if (zh) {
          add(zh, [entry.term], en);
          rememberEnglish(en, entry.term);
          add(en, [entry.term], undefined, true);
          continue;
        }
      }
      // A missing translation or an unfamiliar alphabet never hides a glossary row.
      add(term, [entry.term]);
      if (latinTerm(term)) rememberEnglish(term, entry.term);
    }
  }
  // English followed by a short Chinese parenthesis has an unambiguous boundary.
  // Chinese-before-English in running prose can contain verbs/a whole clause;
  // only the explicitly paired glossary above supplies that direction.
  for (const text of texts) {
    for (const match of text.matchAll(
      /([A-Za-z][A-Za-z\d ._+\-/–—]{0,79}?)\s*[(（]([\p{Script=Han}]{1,16})[)）]/gu,
    )) {
      const english = match[1].trim();
      const known = paired.get(english.toLowerCase());
      if (known) add(match[2], known.glossaryTerms, known.term, true);
    }
  }
  return candidates;
}
