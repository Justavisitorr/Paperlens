import { useRef, useState } from "react";
import { saveTerm, termKey, useTerms, type SavedTerm } from "./terms";

export function SaveSelectedTerm({ term }: { term: Omit<SavedTerm, "id" | "added"> }) {
  const terms = useTerms();
  const button = useRef<HTMLButtonElement>(null);
  const status = useRef<HTMLSpanElement>(null);
  const [error, setError] = useState("");
  const saved = terms.some((entry) => entry.id === termKey(term.paperId, term.term));
  const tooLong = !term.term.trim() || term.term.trim().length > 40;
  return (
    <>
      {saved ? (
        <span
          ref={status}
          tabIndex={-1}
          className="term-saved"
          role="status"
          title="已收藏到术语本"
        >
          <span aria-hidden="true">✓</span> 已收藏
        </span>
      ) : (
        <button
          ref={button}
          className="button small"
          disabled={tooLong}
          title={tooLong ? "请选择不超过 40 字的术语" : undefined}
          onClick={() => {
            try {
              const restoreFocus = document.activeElement === button.current;
              saveTerm(term);
              setError("");
              if (restoreFocus)
                requestAnimationFrame(() =>
                  status.current?.focus({ preventScroll: true }),
                );
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
        >
          收藏术语
        </button>
      )}
      {error && <p role="alert">{error}</p>}
    </>
  );
}
