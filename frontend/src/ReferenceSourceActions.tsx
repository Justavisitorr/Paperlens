import { useMemo, useState } from "react";
import {
  orderedReferenceSources,
  preferredReferenceSource,
  referenceSources,
  type SourcePreference,
} from "./referenceSources";

/** The main link opens the preference; the adjacent chooser also works on touch. */
export function ReferenceSourceActions({
  text,
  href,
  preference,
}: {
  text: string;
  href?: string;
  preference: SourcePreference;
}) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const sources = useMemo(
    () => orderedReferenceSources(referenceSources(text, href), preference),
    [text, href, preference],
  );
  const primary = preferredReferenceSource(sources);
  const open = hovered || focused || pinned;
  return (
    <span
      className="reference-source-picker"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          setFocused(false);
          setPinned(false);
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          setHovered(false);
          setFocused(false);
          setPinned(false);
        }
      }}
    >
      <a href={primary.href} target="_blank" rel="noopener noreferrer">
        {primary.kind === "search"
          ? "搜索原文 ↗"
          : primary.kind === "provided"
            ? "文献链接 ↗"
            : "原文链接 ↗"}
      </a>
      <button
        type="button"
        className="reference-source-toggle"
        aria-label="选择文献来源"
        aria-expanded={open}
        onClick={() => {
          if (pinned) {
            setPinned(false);
            setFocused(false);
            setHovered(false);
          } else setPinned(true);
        }}
      >
        ▾
      </button>
      {open && (
        <span className="reference-source-menu" role="group" aria-label="文献来源">
          <span className="reference-source-heading">选择文献来源</span>
          {sources.map((source) => (
            <a
              key={source.href}
              href={source.href}
              target="_blank"
              rel="noopener noreferrer"
            >
              {source.label}
              {source.href === primary.href ? " · 默认" : ""} ↗
            </a>
          ))}
        </span>
      )}
    </span>
  );
}
