import { useSyncExternalStore } from "react";

export type SourcePreference = "doi" | "arxiv" | "provided";
export type SourceKind = SourcePreference | "publisher" | "search";
export interface ReferenceSource {
  href: string;
  kind: SourceKind;
  label: string;
}

export const REFERENCE_SOURCE_KEY = "paperlens:reference-source-preference";
const CHANGE_EVENT = "paperlens:reference-source-preference-changed";
export const SOURCE_PREFERENCES: { value: SourcePreference; label: string }[] = [
  { value: "doi", label: "DOI / 出版网站优先" },
  { value: "arxiv", label: "arXiv 优先" },
  { value: "provided", label: "文献提供的其他链接优先" },
];

export function normalizeSourcePreference(value: unknown): SourcePreference {
  return value === "arxiv" || value === "provided" ? value : "doi";
}

let transientPreference: SourcePreference | null = null;
export function readSourcePreference(): SourcePreference {
  if (transientPreference !== null) return transientPreference;
  try {
    return normalizeSourcePreference(
      JSON.parse(localStorage.getItem(REFERENCE_SOURCE_KEY) || "null"),
    );
  } catch {
    return "doi";
  }
}

export function saveSourcePreference(value: SourcePreference): boolean {
  const preference = normalizeSourcePreference(value);
  let saved = true;
  try {
    localStorage.setItem(REFERENCE_SOURCE_KEY, JSON.stringify(preference));
    transientPreference = null;
  } catch {
    transientPreference = preference;
    saved = false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return saved;
}

function subscribe(notify: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === REFERENCE_SOURCE_KEY) {
      transientPreference = null;
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

export function useSourcePreference() {
  return useSyncExternalStore(subscribe, readSourcePreference, () => "doi" as const);
}

export function trimReferenceDestination(value: string): string {
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

/** Unwrap only unambiguous TeX URL continuations and standard escaped characters. */
function normalizeReferenceText(text: string): string {
  return text
    .replace(/\\(?:url|href)\s*\{([^{}]*)\}/gi, (whole) =>
      whole.replace(/\r?\n[ \t]*/g, ""),
    )
    .replace(/\\([_&#%])/g, "$1");
}

export function safeReferenceUrl(value: string): string | undefined {
  if (/[\s<>"'{}\\\u0000-\u001f\u007f]/u.test(value) || /(?:\.\.\.|…)$/u.test(value))
    return;
  try {
    const url = new URL(value);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      !url.hostname ||
      url.username ||
      url.password
    )
      return;
    return url.href;
  } catch {
    return;
  }
}

export function isReferenceSearch(value: string): boolean {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    return (
      host === "search.crossref.org" ||
      host === "scholar.google.com" ||
      ((host === "google.com" || host.endsWith(".google.com")) &&
        url.pathname === "/search") ||
      (["arxiv.org", "www.arxiv.org"].includes(host) &&
        url.pathname.startsWith("/search"))
    );
  } catch {
    return false;
  }
}

function normalizedDoi(value: string): string | undefined {
  if (/(?:\.{3}|…)[)\].,;!?]*$/u.test(value)) return;
  try {
    const doi = trimReferenceDestination(decodeURIComponent(value)).toLowerCase();
    if (!/^10\.\d{4,9}\/[^\s<>"'{}\\/][^\s<>"'{}\\]*$/iu.test(doi) || doi.endsWith("/"))
      return;
    return doi;
  } catch {
    return;
  }
}

const ARXIV_ID =
  "(?:\\d{2}(?:0[1-9]|1[0-2])\\.\\d{4,5}(?:v\\d+)?|[a-z][a-z0-9.-]*\\/\\d{7}(?:v\\d+)?)";

function possiblyWrappedPlainLink(text: string, end: number, raw: string): boolean {
  // Without a closing delimiter there is no safe way to tell a line-wrapped
  // identifier from a complete identifier followed by a new prose line.
  return !/[.,;!?)\]]$/u.test(raw) && /^\r?\n[ \t]*\S/u.test(text.slice(end));
}

/** Recognize a few article-page patterns, without claiming bibliographic verification. */
function publisherArticleUrl(url: URL): boolean {
  const host = url.hostname.toLowerCase();
  const path = url.pathname.toLowerCase();
  return (
    (host === "dl.acm.org" && path.startsWith("/doi/")) ||
    (host === "ieeexplore.ieee.org" && /^\/document\/\d+\/?$/.test(path)) ||
    (host === "link.springer.com" && /^\/(?:article|chapter|book)\//.test(path)) ||
    ((host === "nature.com" || host === "www.nature.com") &&
      path.startsWith("/articles/")) ||
    (host === "onlinelibrary.wiley.com" && path.startsWith("/doi/")) ||
    ((host === "sciencedirect.com" || host === "www.sciencedirect.com") &&
      path.startsWith("/science/article/")) ||
    ((host === "aclanthology.org" || host === "www.aclanthology.org") &&
      /^\/\d{4}\.[a-z0-9.-]+\/?$/i.test(path))
  );
}

/** Only explicit identifiers or URLs from this bibliography item are candidates. */
export function referenceSources(text: string, existing = ""): ReferenceSource[] {
  const source = normalizeReferenceText(text);
  const candidates = new Map<string, ReferenceSource>();
  let savedSearch = "";
  const add = (key: string, item: ReferenceSource) => {
    if (!candidates.has(key)) candidates.set(key, item);
  };
  const addDoi = (raw: string) => {
    const doi = normalizedDoi(raw);
    if (!doi) return;
    add(`doi:${doi}`, {
      href: `https://doi.org/${encodeURIComponent(doi).replace(/%2F/gi, "/")}`,
      kind: "doi",
      label: doi.startsWith("10.48550/arxiv.") ? "DOI · arXiv" : "DOI",
    });
  };
  const addUrl = (raw: string) => {
    if (/(?:\.{3}|…)[)\].,;!?]*$/u.test(raw)) return;
    const href = safeReferenceUrl(trimReferenceDestination(raw));
    if (!href) return;
    if (isReferenceSearch(href)) {
      savedSearch ||= href;
      return;
    }
    const url = new URL(href);
    const host = url.hostname.toLowerCase();
    if (["doi.org", "dx.doi.org"].includes(host)) {
      addDoi(url.pathname.slice(1));
      return;
    }
    const arxiv = url.pathname.match(
      new RegExp(`^/(?:abs|pdf)/(${ARXIV_ID})(?:\\.pdf)?/?$`, "i"),
    );
    if (["arxiv.org", "www.arxiv.org"].includes(host) && arxiv) {
      add(`url:${href}`, {
        href,
        kind: "arxiv",
        label: /\/pdf\//i.test(url.pathname) ? "arXiv PDF" : "arXiv",
      });
      return;
    }
    if (publisherArticleUrl(url)) {
      add(`url:${href}`, { href, kind: "publisher", label: `出版网站链接 · ${host}` });
      return;
    }
    add(`url:${href}`, { href, kind: "provided", label: `文献提供的链接 · ${host}` });
  };
  if (existing) addUrl(existing);
  // Remove URL tokens before scanning DOI text: a search query or another URL's
  // parameters are not evidence that its embedded DOI identifies this work.
  const withoutUrls = source.replace(
    /https?:\/\/[^\s<>"'{}\\]+/giu,
    (raw, offset: number) => {
      const end = offset + raw.length;
      if (source[end] !== "\\" && !possiblyWrappedPlainLink(source, end, raw))
        addUrl(raw);
      return " ".repeat(raw.length);
    },
  );
  for (const match of withoutUrls.matchAll(/\b10\.\d{4,9}\/[^\s<>"'{}\\]+/giu)) {
    const end = (match.index || 0) + match[0].length;
    if (
      withoutUrls[end] !== "\\" &&
      !possiblyWrappedPlainLink(withoutUrls, end, match[0])
    )
      addDoi(match[0]);
  }
  const arxivPattern = new RegExp(`\\barxiv\\s*:\\s*(${ARXIV_ID})(?!\\w|\\.\\d)`, "giu");
  for (const match of withoutUrls.matchAll(arxivPattern)) {
    const id = match[1].toLowerCase();
    add(`url:https://arxiv.org/abs/${id}`, {
      href: `https://arxiv.org/abs/${id}`,
      kind: "arxiv",
      label: "arXiv",
    });
  }
  const found = [...candidates.values()];
  const counts = new Map<string, number>();
  for (const item of found) counts.set(item.label, (counts.get(item.label) || 0) + 1);
  const numbers = new Map<string, number>();
  for (const item of found) {
    if ((counts.get(item.label) || 0) > 1) {
      const n = (numbers.get(item.label) || 0) + 1;
      numbers.set(item.label, n);
      item.label += ` · 来源 ${n}`;
    }
  }
  found.push({
    href:
      savedSearch ||
      `https://search.crossref.org/?q=${encodeURIComponent(text.slice(0, 2000))}`,
    kind: "search",
    label: "学术搜索",
  });
  return found;
}

export function orderedReferenceSources(
  sources: ReferenceSource[],
  preference: SourcePreference = "doi",
): ReferenceSource[] {
  const fixed: SourceKind[] = ["doi", "publisher", "arxiv", "provided", "search"];
  const preferred: SourceKind[] =
    preference === "doi" ? ["doi", "publisher"] : [preference];
  const order = [...preferred, ...fixed.filter((kind) => !preferred.includes(kind))];
  return [...sources].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
}

/** Conflicting candidates of the same preferred kind need a choice, not a guess. */
export function preferredReferenceSource(sources: ReferenceSource[]): ReferenceSource {
  const first = sources[0];
  const sameKind = sources.filter((source) => source.kind === first.kind);
  const arxivIdentities =
    first.kind === "arxiv"
      ? new Set(
          sameKind.map((source) =>
            new URL(source.href).pathname
              .replace(/^\/(?:abs|pdf)\//i, "")
              .replace(/\.pdf\/?$/i, "")
              .replace(/\/$/, "")
              .toLowerCase(),
          ),
        )
      : null;
  return sameKind.length === 1 || arxivIdentities?.size === 1
    ? first
    : sources.find((source) => source.kind === "search")!;
}

/** Hide explicit technical identifiers; the unmodified source remains available for copying. */
export function displayReferenceText(text: string): string {
  const result = text
    .replace(/\\href\s*\{https?:\/\/[^{}]+\}\s*\{([^{}]*)\}/giu, "$1")
    .replace(/\s*(?:(?:URL|doi)\s*[:：]?\s*)?\\url\s*\{[^{}]*\}[.,;。]*/giu, "")
    .replace(/\s*(?:(?:URL|doi)\s*[:：]?\s*)?https?:\/\/[^\s<>"'{}\\]+/giu, "")
    .replace(/\s*(?:doi\s*[:：]?\s*)?\b10\.\d{4,9}\/[^\s<>"'{}\\]+/giu, "")
    .trimEnd();
  return result.replace(/[()[\]\s]/g, "") ? result : "文献未提供其他书目信息";
}

/** An excerpt at punctuation boundaries; this does not identify bibliographic fields. */
export function referencePreview(text: string): string | null {
  if (text.length <= 420) return null;
  const commas = [...text.matchAll(/[,，]/gu)];
  const secondComma = commas[1]?.index;
  const beginning =
    secondComma !== undefined && secondComma < 80
      ? text.slice(0, secondComma + 1).trim()
      : (text.slice(0, 80).match(/^.*(?=\s)/u)?.[0] || "").trim();

  const boundaries = [...text.matchAll(/[.!?。！？]\s+/gu)];
  const beforeLastTwo = boundaries.at(-2);
  const suffix = beforeLastTwo
    ? text.slice(beforeLastTwo.index + beforeLastTwo[0].length).trim()
    : "";
  let ending = suffix.length <= 240 ? suffix : "";
  if (!ending) {
    const start = Math.max(0, text.length - 240);
    const tail = text.slice(start);
    ending =
      start && !/\s/u.test(text[start - 1]) && !/\s/u.test(text[start])
        ? tail.replace(/^\S+(?:\s+|$)/u, "").trim()
        : tail.trim();
  }
  return [beginning, "…", ending].filter(Boolean).join(" ");
}
