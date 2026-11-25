import type { ImportCandidate, PaperSummary } from "./api";

function normalizedUrl(value: string) {
  try {
    const url = new URL(value);
    return `${url.hostname.toLowerCase()}${url.pathname.replace(/\/$/, "")}${url.search}`;
  } catch {
    return value.trim();
  }
}

export function importIdentity(
  candidate: Pick<ImportCandidate, "id" | "doi" | "url" | "pdf_url">,
) {
  const doi = candidate.doi
    ?.trim()
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "")
    .toLowerCase();
  return doi
    ? `doi:${doi}`
    : candidate.url || candidate.pdf_url
      ? `url:${normalizedUrl(candidate.url || candidate.pdf_url || "")}`
      : `id:${candidate.id}`;
}

export function existingImport(candidate: ImportCandidate, papers: PaperSummary[]) {
  const key = importIdentity(candidate);
  return papers.find(
    (paper) =>
      importIdentity({ id: paper.id, doi: paper.doi, url: paper.source_url }) === key ||
      Boolean(
        candidate.url &&
        paper.source_url &&
        normalizedUrl(candidate.url) === normalizedUrl(paper.source_url),
      ),
  );
}

const KEY = "paperlens.imported-sources";
export function readImportReceipts(): Record<string, string> {
  try {
    const value = JSON.parse(sessionStorage.getItem(KEY) || "{}");
    return value && !Array.isArray(value) && typeof value === "object"
      ? (Object.fromEntries(
          Object.entries(value).filter(([, id]) => typeof id === "string"),
        ) as Record<string, string>)
      : {};
  } catch {
    return {};
  }
}
export function rememberImport(
  key: string,
  id: string,
  current: Record<string, string> = {},
) {
  const next = { ...readImportReceipts(), ...current, [key]: id };
  try {
    sessionStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* current component still retains it */
  }
  return next;
}
