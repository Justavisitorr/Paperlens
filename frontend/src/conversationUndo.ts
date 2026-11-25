import { useSyncExternalStore } from "react";
import type { Conversation } from "./chat";

const KEY = "paperlens.deleted-conversations";
let records: Conversation[] | undefined;
const listeners = new Set<() => void>();

export function deletedConversations(): Conversation[] {
  if (!records) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(KEY) || "[]");
      records = Array.isArray(saved)
        ? saved.filter(
            (doc) =>
              doc &&
              typeof doc.id === "string" &&
              typeof doc.title === "string" &&
              Array.isArray(doc.messages) &&
              Array.isArray(doc.refs),
          )
        : [];
    } catch {
      records = [];
    }
  }
  return records;
}

function publish(next: Conversation[]) {
  records = next;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // Keep undo available across in-app navigation even if storage is full.
  }
  listeners.forEach((notify) => notify());
}

export function rememberDeletedConversation(doc: Conversation) {
  publish([doc, ...deletedConversations().filter((item) => item.id !== doc.id)]);
}

export function forgetDeletedConversation(id: string) {
  publish(deletedConversations().filter((item) => item.id !== id));
}

export function useDeletedConversations() {
  return useSyncExternalStore(
    (notify) => {
      listeners.add(notify);
      return () => listeners.delete(notify);
    },
    deletedConversations,
    deletedConversations,
  );
}
