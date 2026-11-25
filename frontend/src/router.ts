import { useSyncExternalStore } from "react";

/**
 * Hash routes keep the workspace a single static file that works both from
 * the local service and from disk:
 *   #/library   #/terms   #/upload   #/settings   #/read/<paperId>[/<mode>]
 *   #/chat[/<conversationId>]
 *   #/processing/<jobId> (original PDF while processing continues)
 */
export type ReaderMode = "overview" | "map" | "text" | "glossary" | "results" | "quality";
export type SettingsTab =
  "appearance" | "model" | "notes" | "processing" | "experimental" | "catalog" | "about";
export type SettingsSection = "tiered" | "limits" | "auto_notes" | "experimental";
export const SETTINGS_TARGET_EVENT = "paperlens:settings-target";
const settingsTabs: SettingsTab[] = [
  "appearance",
  "model",
  "notes",
  "processing",
  "experimental",
  "catalog",
  "about",
];
export type Route =
  | { page: "library" }
  | { page: "terms" }
  | { page: "upload" }
  | { page: "settings"; tab?: SettingsTab; section?: SettingsSection }
  | { page: "chat"; id?: string }
  | { page: "processing"; jobId: string }
  | { page: "read"; paperId: string; mode: ReaderMode };

const modes: ReaderMode[] = ["overview", "map", "text", "glossary", "results", "quality"];

export function parseRoute(hash: string): Route | null {
  const parts = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "settings") {
    const tab = settingsTabs.includes(parts[1] as SettingsTab)
      ? (parts[1] as SettingsTab)
      : undefined;
    const section =
      (tab === "processing" && ["tiered", "limits"].includes(parts[2])) ||
      (tab === "notes" && parts[2] === "auto_notes") ||
      (tab === "experimental" && parts[2] === "experimental")
        ? (parts[2] as SettingsSection)
        : undefined;
    return { page: "settings", ...(tab && { tab }), ...(section && { section }) };
  }
  if (["library", "terms", "upload"].includes(parts[0]))
    return { page: parts[0] as "library" | "terms" | "upload" };
  if (parts[0] === "chat")
    return /^[0-9a-f]{32}$/.test(parts[1] || "")
      ? { page: "chat", id: parts[1] }
      : { page: "chat" };
  if (parts[0] === "processing")
    return /^[0-9a-f]{32}$/.test(parts[1] || "")
      ? { page: "processing", jobId: parts[1] }
      : null;
  if (parts[0] === "read" && parts[1])
    return {
      page: "read",
      paperId: parts[1],
      mode: modes.includes(parts[2] as ReaderMode)
        ? (parts[2] as ReaderMode)
        : "overview",
    };
  return null;
}

export function href(route: Route): string {
  if (route.page === "settings")
    return `#/settings${route.tab ? `/${route.tab}${route.section ? `/${route.section}` : ""}` : ""}`;
  if (route.page === "chat") return route.id ? `#/chat/${route.id}` : "#/chat";
  if (route.page === "processing") return `#/processing/${route.jobId}`;
  if (route.page !== "read") return `#/${route.page}`;
  return `#/read/${encodeURIComponent(route.paperId)}${route.mode === "overview" ? "" : `/${route.mode}`}`;
}

export function go(route: Route, replace = false) {
  const next = href(route);
  if (location.hash === next) {
    if (route.page === "settings" && route.section)
      window.dispatchEvent(new Event(SETTINGS_TARGET_EVENT));
    return;
  }
  if (replace) history.replaceState(null, "", next);
  else history.pushState(null, "", next);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function useHash(): string {
  return useSyncExternalStore(
    (notify) => {
      window.addEventListener("hashchange", notify);
      return () => window.removeEventListener("hashchange", notify);
    },
    () => location.hash,
  );
}
