import { useSyncExternalStore } from "react";
import { referenceSources } from "./referenceSources";

export type CitationCopyFormat = "text" | "bibtex" | "latex" | "bibtex_latex";
export const CITATION_FORMAT_KEY = "paperlens:citation-copy-format";
const CHANGE_EVENT = "paperlens:citation-copy-format-changed";
export const CITATION_FORMATS: {
  value: CitationCopyFormat;
  label: string;
  hint: string;
}[] = [
  { value: "text", label: "普通文本", hint: "复制参考文献的原始书目文本。" },
  {
    value: "bibtex",
    label: "BibTeX 文献条目",
    hint: "粘贴到 .bib 文件，再使用相同引用键的 \\cite{…} 引用。",
  },
  {
    value: "latex",
    label: "LaTeX 正文引用",
    hint: "复制 \\cite{…}；对应的 BibTeX 条目需要先加入 .bib 文件。",
  },
  {
    value: "bibtex_latex",
    label: "BibTeX 与正文引用",
    hint: "同时复制两段：文献条目放入 .bib，\\cite{…} 放入正文。",
  },
];

export function normalizeCitationFormat(value: unknown): CitationCopyFormat {
  return CITATION_FORMATS.some((format) => format.value === value)
    ? (value as CitationCopyFormat)
    : "text";
}

let transientFormat: CitationCopyFormat | null = null;
export function readCitationFormat(): CitationCopyFormat {
  if (transientFormat !== null) return transientFormat;
  try {
    return normalizeCitationFormat(
      JSON.parse(localStorage.getItem(CITATION_FORMAT_KEY) || '"text"'),
    );
  } catch {
    return "text";
  }
}

/** Save locally; a blocked store still permits the choice for this open page. */
export function saveCitationFormat(value: CitationCopyFormat): boolean {
  const format = normalizeCitationFormat(value);
  let saved = true;
  try {
    localStorage.setItem(CITATION_FORMAT_KEY, JSON.stringify(format));
    transientFormat = null;
  } catch {
    transientFormat = format;
    saved = false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return saved;
}

function subscribe(notify: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === CITATION_FORMAT_KEY) {
      transientFormat = null;
      notify();
    }
  };
  window.addEventListener(CHANGE_EVENT, notify);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, notify);
    window.removeEventListener("storage", onStorage);
  };
}

export function useCitationCopyFormat() {
  return useSyncExternalStore(subscribe, readCitationFormat, () => "text" as const);
}

export interface CopyableCitation {
  text: string;
  href?: string;
}

/** Bracketed bibliography indices and separators alone are not references. */
export function hasCitationText(text: string): boolean {
  const body = text.trim().replace(/^\[\s*\d+\s*\]\s*/, "");
  return /\p{L}/u.test(body) || /10\.\d{4,9}\/\S+/i.test(body);
}

const plainCitation = (text: string) =>
  text
    .trim()
    .replace(/^\[\s*\d+\s*\]\s*/, "")
    .replace(/\s+/g, " ");

function trimDestination(value: string) {
  let result = value.replace(/[.,;:!?。；，]+$/u, "");
  for (const [open, close] of [
    ["(", ")"],
    ["[", "]"],
  ]) {
    while (
      result.endsWith(close) &&
      result.split(close).length > result.split(open).length
    )
      result = result.slice(0, -1);
  }
  return result;
}

function safeUrl(value: string): string | undefined {
  try {
    const url = new URL(trimDestination(value));
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password)
      return;
    const host = url.hostname.toLowerCase();
    if (
      host === "search.crossref.org" ||
      host === "scholar.google.com" ||
      ((host === "google.com" || host.endsWith(".google.com")) &&
        url.pathname === "/search") ||
      (["arxiv.org", "www.arxiv.org"].includes(host) &&
        url.pathname.startsWith("/search"))
    )
      return;
    return url.href.replace(/[\\{}\u0000-\u0020\u007f]/g, (character) =>
      encodeURIComponent(character),
    );
  } catch {
    return;
  }
}

function identifiers(citation: CopyableCitation) {
  const dois = referenceSources(citation.text, citation.href)
    .filter((source) => source.kind === "doi")
    .map((source) => decodeURIComponent(new URL(source.href).pathname.slice(1)));
  const doi = dois.length === 1 ? dois[0] : undefined;
  const explicitUrl = citation.href && safeUrl(citation.href);
  const urls = [
    ...new Set(
      (citation.text.match(/https?:\/\/[^\s<>"']+/gi) || [])
        .map(safeUrl)
        .filter((url): url is string => Boolean(url)),
    ),
  ];
  const url = doi
    ? `https://doi.org/${encodeURIComponent(doi).replace(/%2F/gi, "/")}`
    : explicitUrl || (urls.length === 1 ? urls[0] : undefined);
  return { doi, url };
}

/** Deterministic across papers, reloads and formats; never use display numbering. */
export function citationKey(citation: CopyableCitation): string {
  const { doi } = identifiers(citation);
  const value = doi || plainCitation(citation.text).normalize("NFKC").toLowerCase();
  let hash = 0xcbf29ce484222325n;
  for (const character of value) {
    hash ^= BigInt(character.codePointAt(0)!);
    hash = BigInt.asUintN(64, hash * 0x100000001b3n);
  }
  return `ref_${hash.toString(16).padStart(16, "0")}`;
}

/** Escape prose, including literal TeX, in one pass so new braces stay balanced. */
export function escapeBibtexText(text: string): string {
  const escapes: Record<string, string> = {
    "\\": "\\textbackslash{}",
    "{": "\\{",
    "}": "\\}",
    "&": "\\&",
    "%": "\\%",
    _: "\\_",
    "#": "\\#",
    $: "\\$",
    "^": "\\textasciicircum{}",
    "~": "\\textasciitilde{}",
  };
  return text
    .replace(/\s+/g, " ")
    .replace(/[\\{}&%_#$^~]/g, (character) => escapes[character]);
}

/** Reading records carry faithful prose, not verified structured BibTeX metadata. */
export function formatCitation(
  citation: CopyableCitation,
  format: CitationCopyFormat,
): string {
  if (!hasCitationText(citation.text))
    throw new Error("这条参考文献没有可复制的书目信息。");
  if (format === "text") return citation.text.trim();
  const key = citationKey(citation);
  const cite = `\\cite{${key}}`;
  if (format === "latex") return cite;
  const { doi, url } = identifiers(citation);
  const fields = [`  note = {${escapeBibtexText(plainCitation(citation.text))}}`];
  // DOI and URL are verbatim fields in biblatex, unlike the prose in note.
  // Only validated identifiers enter them; URL braces/backslashes are encoded.
  if (doi) fields.push(`  doi = {${doi}}`);
  if (url) fields.push(`  url = {${url}}`);
  const bibtex = `@misc{${key},\n${fields.join(",\n")}\n}`;
  return format === "bibtex_latex" ? `${bibtex}\n\n${cite}` : bibtex;
}

export async function copyCitation(
  citation: CopyableCitation,
  format: CitationCopyFormat,
): Promise<string> {
  const text = formatCitation(citation, format);
  if (!globalThis.navigator?.clipboard?.writeText)
    throw new Error("浏览器未开放剪贴板，请手动复制下方内容。");
  await navigator.clipboard.writeText(text);
  return text;
}
