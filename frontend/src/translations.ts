import { useCallback, useEffect, useRef, useState } from "react";
import { listTranslations, translateItems, type Translation } from "./api";

/** The reading translation of one paper, loaded once and filled in on request. */
export interface Translations {
  items: Record<string, Translation>;
  /** Item ids being translated right now. */
  pending: Set<string>;
  done: number;
  total: number;
  error: string;
  translate: (ids: string[]) => Promise<void>;
  clearError: () => void;
}

const ROUNDS = 8;
const EMPTY: Record<string, Translation> = {};

export function useTranslations(
  paperId: string,
  enabled: boolean,
  embedded: Record<string, Translation> = EMPTY,
): Translations {
  const [items, setItems] = useState<Record<string, Translation>>(embedded);
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [count, setCount] = useState({ done: 0, total: 0 });
  const [error, setError] = useState("");
  const busy = useRef(false);
  useEffect(() => {
    setItems(embedded);
    if (!enabled) return;
    let live = true;
    listTranslations(paperId)
      .then((loaded) => live && setItems({ ...embedded, ...loaded }))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [paperId, enabled, embedded]);
  const translate = useCallback(
    async (ids: string[]) => {
      if (!enabled || busy.current) return;
      let wanted = ids.filter((id) => !items[id]);
      if (!wanted.length) return;
      busy.current = true;
      setError("");
      setPending(new Set(wanted));
      setCount({ done: 0, total: wanted.length });
      try {
        // The service translates one batch per request and says what is left.
        for (let round = 0; round < ROUNDS && wanted.length; round++) {
          const result = await translateItems(paperId, wanted.slice(0, 64));
          const got = Object.keys(result.items).length;
          setItems((old) => ({ ...old, ...result.items }));
          const left = new Set([...result.remaining, ...wanted.slice(64)]);
          wanted = wanted.filter((id) => left.has(id) && !result.items[id]);
          setPending(new Set(wanted));
          setCount((old) => ({ ...old, done: old.total - wanted.length }));
          if (!got) {
            if (wanted.length) setError(result.warnings[0] || "这一段暂时没有得到译文。");
            break;
          }
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        busy.current = false;
        setPending(new Set());
      }
    },
    [paperId, enabled, items],
  );
  return {
    items,
    pending,
    ...count,
    error,
    translate,
    clearError: () => setError(""),
  };
}
