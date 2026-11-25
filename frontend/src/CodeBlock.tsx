import { Fragment, useMemo, useState } from "react";
import { RichText } from "./readerLinks";
import { Icon } from "./icons";
import { highlightCodeParts } from "./codeHighlight";
import "./styles/26-reading-code.css";

const LANGUAGES: Record<string, string> = {
  cpp: "C++",
  c: "C",
  python: "Python",
  javascript: "JavaScript",
};

export function CodeBlock({
  text,
  language = "",
  label = "",
  fragments,
  activeId,
  currentId,
}: {
  text: string;
  language?: string;
  label?: string;
  fragments?: { id: string; text: string; blockIds: string[] }[];
  activeId?: string | null;
  currentId?: string;
}) {
  const [status, setStatus] = useState("");
  const highlighted = useMemo(
    () => highlightCodeParts(fragments?.map((part) => part.text) || [text], language),
    [text, fragments, language],
  );
  const content = (index: number) =>
    highlighted[index].map((token, n) =>
      token.classes ? (
        <span key={n} className={token.classes}>
          {token.text}
        </span>
      ) : (
        token.text
      ),
    );
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setStatus("已复制");
    } catch {
      setStatus("请选中复制");
    }
  };
  return (
    <figure className="reading-code-block">
      {label && (
        <figcaption>
          <RichText text={label} />
        </figcaption>
      )}
      <div className="code-frame">
        <div className="code-toolbar">
          <span className="code-language">
            <span aria-hidden="true">&lt;/&gt;</span> {LANGUAGES[language] || "文本"}
          </span>
          <button
            type="button"
            className="text-button"
            onClick={() => void copy()}
            aria-label="复制代码"
          >
            <Icon name="copy" size={15} />
            复制
          </button>
          <span role="status" className="code-copy-status">
            {status}
          </span>
        </div>
        <pre
          tabIndex={0}
          aria-label="代码内容"
          data-reading-surface={fragments ? undefined : "original"}
        >
          <code>
            {fragments
              ? fragments.map((part, index) => (
                  <Fragment key={part.id}>
                    {index > 0 && "\n"}
                    <span
                      id={part.id}
                      className="reading-item reading-code code-fragment"
                      data-active={activeId === part.id ? "true" : undefined}
                      data-reading-current={currentId === part.id ? "true" : undefined}
                    >
                      {part.blockIds.map((id) => (
                        <span id={`block-${id}`} key={id} aria-hidden="true" />
                      ))}
                      <span data-reading-surface="original">{content(index)}</span>
                    </span>
                  </Fragment>
                ))
              : content(0)}
          </code>
        </pre>
      </div>
    </figure>
  );
}
