import { useEffect, useId, useRef, useState } from "react";
import { displayReferenceText, referencePreview } from "./referenceSources";

export interface ReferenceOption {
  id: string;
  label: string;
  text: string;
}

const searchKey = (text: string) =>
  text.normalize("NFKD").replace(/\p{M}/gu, "").toLocaleLowerCase();

export function matchesReference(option: ReferenceOption, query: string): boolean {
  const text = searchKey(`${option.label} ${option.text}`);
  return searchKey(query)
    .trim()
    .split(/\s+/)
    .every((word) => text.includes(word));
}

/** One compact input filters the bibliography and offers keyboard navigation. */
export function ReferenceSearch({
  query,
  onChange,
  matches,
  total,
  onSelect,
}: {
  query: string;
  onChange: (value: string) => void;
  matches: ReferenceOption[];
  total: number;
  onSelect: (id: string) => void;
}) {
  const id = useId();
  const [expanded, setExpanded] = useState(false);
  const [active, setActive] = useState(-1);
  const list = useRef<HTMLDivElement>(null);
  const open = expanded && Boolean(query.trim()) && matches.length > 0;
  const activeId = open && matches[active] ? `${id}-${active}` : undefined;
  useEffect(() => {
    if (activeId)
      list.current
        ?.querySelector(`[id="${CSS.escape(activeId)}"]`)
        ?.scrollIntoView({ block: "nearest" });
  }, [activeId]);
  const select = (index: number) => {
    if (!matches[index]) return;
    setExpanded(false);
    setActive(-1);
    onSelect(matches[index].id);
  };

  return (
    <div
      className="reference-search"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setExpanded(false);
      }}
    >
      <input
        type="search"
        role="combobox"
        aria-label="搜索参考文献"
        placeholder="作者、标题或编号"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-activedescendant={activeId}
        value={query}
        onFocus={() => setExpanded(true)}
        onChange={(event) => {
          onChange(event.target.value);
          setActive(-1);
          setExpanded(true);
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (
            (event.key === "ArrowDown" || event.key === "ArrowUp") &&
            query.trim() &&
            matches.length
          ) {
            event.preventDefault();
            event.stopPropagation();
            setExpanded(true);
            setActive((index) =>
              event.key === "ArrowDown"
                ? (index + 1) % matches.length
                : (index <= 0 ? matches.length : index) - 1,
            );
          } else if (event.key === "Enter" && open) {
            event.preventDefault();
            event.stopPropagation();
            select(active < 0 ? 0 : active);
          } else if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            if (open) setExpanded(false);
            else onChange("");
            setActive(-1);
          }
        }}
      />
      {query.trim() && (
        <span className="reference-search-count" role="status">
          {matches.length ? `${matches.length} / ${total} 篇` : "没有匹配的参考文献"}
        </span>
      )}
      {open && (
        <div
          id={id}
          ref={list}
          className="reference-search-options"
          role="listbox"
          aria-label="匹配的参考文献"
        >
          {matches.map((option, index) => (
            <button
              key={option.id}
              id={`${id}-${index}`}
              type="button"
              role="option"
              aria-selected={index === active}
              tabIndex={-1}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => select(index)}
            >
              {option.label && <strong>{option.label}</strong>}
              <span>{referencePreview(displayReferenceText(option.text)) || displayReferenceText(option.text)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
