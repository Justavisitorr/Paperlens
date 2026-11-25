import { useEffect, useId, useRef, useState } from "react";
import {
  CITATION_FORMATS,
  copyCitation,
  formatCitation,
  hasCitationText,
  useCitationCopyFormat,
  type CopyableCitation,
} from "./citationFormats";
import "./styles/citation-copy.css";

export function CitationCopyButton({ text, href }: CopyableCitation) {
  const format = useCitationCopyFormat();
  const selected = CITATION_FORMATS.find((item) => item.value === format)!;
  const [state, setState] = useState<"idle" | "copying" | "copied" | "error">("idle");
  const manual = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const id = useId();
  const valid = hasCitationText(text);
  useEffect(() => {
    setState("idle");
    return () => clearTimeout(timer.current);
  }, [text, href, format]);
  useEffect(() => {
    if (state === "error") {
      manual.current?.focus();
      manual.current?.select();
    }
  }, [state]);
  const copy = async () => {
    clearTimeout(timer.current);
    setState("copying");
    try {
      await copyCitation({ text, href }, format);
      setState("copied");
      timer.current = setTimeout(() => setState("idle"), 2200);
    } catch {
      setState("error");
    }
  };
  return (
    <div className="citation-copy">
      <button
        type="button"
        className="text-button"
        disabled={!valid || state === "copying"}
        title={
          valid
            ? `复制为${selected.label}；可在设置中更改`
            : "这条参考文献没有可复制的书目信息"
        }
        aria-describedby={state === "error" ? id : undefined}
        onClick={() => void copy()}
      >
        {state === "copied" ? "已复制" : state === "copying" ? "复制中…" : "复制引用"}
      </button>
      <span className="sr-only" role="status">
        {state === "copied" ? `已复制${selected.label}` : ""}
      </span>
      {state === "error" && valid && (
        <div className="citation-copy-manual">
          <span id={id} role="status">
            自动复制未成功，可选择下方内容手动复制。
          </span>
          <textarea
            ref={manual}
            aria-label={`待复制的${selected.label}`}
            readOnly
            value={formatCitation({ text, href }, format)}
          />
          <button type="button" className="text-button" onClick={() => setState("idle")}>
            收起
          </button>
        </div>
      )}
    </div>
  );
}
