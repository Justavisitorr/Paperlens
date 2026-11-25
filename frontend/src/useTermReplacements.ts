import { useCallback, useEffect, useRef, useState } from "react";
import { readLocal } from "./readingExperience";
import { normalizeRules, type TermReplacementRule } from "./termReplacements";

export const TERM_REPLACEMENTS_KEY = "paperlens:term-replacements";

/** Browser-wide display preferences, separate from the model's saved translations. */
export function useTermReplacements() {
  const [rules, setRules] = useState(() =>
    normalizeRules(readLocal(TERM_REPLACEMENTS_KEY)),
  );
  const current = useRef(rules);
  const [error, setError] = useState("");
  const commit = useCallback((next: TermReplacementRule[]) => {
    current.current = next;
    setRules(next);
    try {
      localStorage.setItem(TERM_REPLACEMENTS_KEY, JSON.stringify(next));
      setError("");
    } catch {
      setError("替换已在本页生效，但浏览器未能保存规则；离开本页或刷新后可能丢失。");
    }
  }, []);
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key !== null && event.key !== TERM_REPLACEMENTS_KEY) return;
      const next = normalizeRules(readLocal(TERM_REPLACEMENTS_KEY));
      current.current = next;
      setRules(next);
      setError("");
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  const apply = useCallback(
    (source: string, replacement: string) => {
      source = source.trim();
      replacement = replacement.trim();
      if (!source || !replacement) return;
      const existing = current.current.find((rule) => rule.source === source);
      const rest = current.current.filter((rule) => rule.source !== source);
      commit(
        normalizeRules([
          { id: existing?.id || crypto.randomUUID(), source, replacement },
          ...rest,
        ]),
      );
    },
    [commit],
  );
  return {
    rules,
    error,
    apply,
    remove: (id: string) => commit(current.current.filter((rule) => rule.id !== id)),
    clear: () => commit([]),
  };
}
