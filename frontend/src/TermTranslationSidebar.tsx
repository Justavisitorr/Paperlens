import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "./icons";
import type { TermCandidate, TermReplacementRule } from "./termReplacements";

interface TermTranslationSidebarProps {
  open: boolean;
  onOpen: () => void;
  rules: TermReplacementRule[];
  candidates: TermCandidate[];
  onApply: (source: string, replacement: string) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
  onClose: () => void;
  error?: string;
}

function TermRow({
  source,
  term,
  glossaryTerms = [],
  matched = true,
  enteredSource,
  onSourceChange,
  rules,
  onApply,
  onRemove,
}: {
  source: string;
  term?: string;
  glossaryTerms?: string[];
  matched?: boolean;
  enteredSource?: string;
  onSourceChange?: (source: string) => void;
  rules: TermReplacementRule[];
  onApply: (source: string, replacement: string) => void;
  onRemove: (id: string) => void;
}) {
  const [draft, setDraft] = useState<string>();
  const inputId = useId();
  const currentSource = matched ? source : (enteredSource ?? source).trim();
  const rule = rules.find((entry) => entry.source === currentSource);
  const replacement = draft ?? rule?.replacement ?? source;
  const changed =
    currentSource &&
    replacement.trim() &&
    replacement.trim() !== currentSource &&
    replacement.trim() !== rule?.replacement;

  return (
    <form
      className={`term-replacement-row${!matched ? " is-unmatched" : ""}`}
      data-term-source={source}
      data-matched={matched}
      onSubmit={(event) => {
        event.preventDefault();
        if (changed) {
          onApply(currentSource, replacement.trim());
          setDraft(undefined);
        }
      }}
    >
      <label className="term-replacement-source" htmlFor={inputId}>
        {glossaryTerms.length ? (
          glossaryTerms.map((label) => (
            <span className="term-replacement-glossary" key={label}>
              {label}
            </span>
          ))
        ) : (
          <span>{source}</span>
        )}
        {matched && glossaryTerms.some((label) => label !== source) && (
          <small>原译法：{source}</small>
        )}
        {term &&
          term !== source &&
          !glossaryTerms.some((label) => label.includes(term)) && <small>{term}</small>}
      </label>
      {!matched && (
        <>
          <p className="term-replacement-unmatched">尚未在当前译文中匹配</p>
          <label className="term-replacement-source-input" htmlFor={`${inputId}-source`}>
            <span>原译法</span>
            <input
              id={`${inputId}-source`}
              aria-label={`${source}的原译法`}
              value={enteredSource ?? source}
              onChange={(event) => {
                onSourceChange?.(event.target.value);
              }}
              placeholder="填写当前译文实际使用的词"
              autoComplete="off"
              spellCheck={false}
              required
            />
          </label>
        </>
      )}
      <div className="term-replacement-edit">
        {!matched && (
          <label className="term-replacement-edit-label" htmlFor={inputId}>
            替换为
          </label>
        )}
        <input
          id={inputId}
          aria-label={`${source}的替换译法`}
          value={replacement}
          onChange={(event) => setDraft(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          required
        />
        <button type="submit" disabled={!changed}>
          替换
        </button>
      </div>
      {rule && (
        <div className="term-replacement-saved">
          <span>已保存</span>
          <button
            className="term-replacement-restore"
            type="button"
            onClick={() => {
              onRemove(rule.id);
              setDraft(undefined);
            }}
            aria-label={`恢复“${currentSource}”的原译法`}
          >
            恢复
          </button>
        </div>
      )}
    </form>
  );
}

export function TermTranslationSidebar({
  open,
  onOpen,
  rules,
  candidates,
  onApply,
  onRemove,
  onClear,
  onClose,
  error,
}: TermTranslationSidebarProps) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [replacement, setReplacement] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [enteredSources, setEnteredSources] = useState<Record<string, string>>({});
  const [lastApplied, setLastApplied] = useState<{
    source: string;
    replacement: string;
  }>();
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const manualId = useId();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const matches = (...values: (string | undefined)[]) =>
    !normalizedQuery ||
    values.some((value) => value?.toLocaleLowerCase().includes(normalizedQuery));
  const visibleRules = rules.filter((rule) => matches(rule.source, rule.replacement));
  const visibleCandidates = candidates.filter((candidate) =>
    matches(
      candidate.source,
      enteredSources[candidate.source],
      candidate.term,
      ...candidate.glossaryTerms,
      rules.find(
        (rule) =>
          rule.source === (enteredSources[candidate.source] ?? candidate.source).trim(),
      )?.replacement,
    ),
  );
  const apply = (nextSource: string, nextReplacement: string) => {
    onApply(nextSource, nextReplacement);
    setLastApplied({ source: nextSource, replacement: nextReplacement });
  };
  const applied =
    lastApplied &&
    rules.some(
      (rule) =>
        rule.source === lastApplied.source &&
        rule.replacement === lastApplied.replacement,
    );
  const manualUnchanged = rules.some(
    (rule) => rule.source === source.trim() && rule.replacement === replacement.trim(),
  );

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement;
    panelRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      const focusedInside = target instanceof Node && panelRef.current?.contains(target);
      const focusedTab =
        target instanceof Element && !!target.closest(".term-sidebar-tab");
      if (event.key === "Escape" && !event.isComposing && (focusedInside || focusedTab)) {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
      }
      if (
        event.key === "Tab" &&
        focusedInside &&
        window.matchMedia("(max-width: 1180px)").matches
      ) {
        const controls = Array.from(
          panelRef.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled), summary",
          ) ?? [],
        ).filter((element) => element.offsetParent !== null);
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && target === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && target === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
        previousFocus.focus();
      }
    };
  }, [open]);

  return (
    <>
      {open && (
        <button
          className="term-sidebar-backdrop"
          aria-label="关闭术语替换侧栏"
          tabIndex={-1}
          onClick={onClose}
        />
      )}
      <div className="term-sidebar-viewport">
        <div className={`term-sidebar-drawer ${open ? "is-open" : ""}`}>
          <aside
            id="term-translation-sidebar"
            className="term-translation-sidebar"
            ref={panelRef}
            aria-labelledby="term-translation-title"
            aria-hidden={!open}
            inert={!open}
          >
            <header className="term-translation-header">
              <div>
                <h2 id="term-translation-title">术语替换</h2>
                <span>统一你的阅读用词</span>
              </div>
              <button
                className="term-translation-close"
                type="button"
                aria-label="收起术语替换"
                title="收起术语替换（Esc）"
                onClick={onClose}
              >
                <Icon name="close" size={18} />
              </button>
            </header>

            <div className="term-translation-scroll">
              <p className="term-translation-intro">
                修改译文里的用词，全文统一显示。规则保存在当前浏览器，对所有论文生效。
              </p>
              <form
                className="term-translation-manual"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (
                    source.trim() &&
                    replacement.trim() &&
                    source.trim() !== replacement.trim()
                  ) {
                    apply(source.trim(), replacement.trim());
                  }
                }}
              >
                <h3>添加替换</h3>
                <label htmlFor={`${manualId}-source`}>原译法</label>
                <input
                  id={`${manualId}-source`}
                  value={source}
                  onChange={(event) => setSource(event.target.value)}
                  placeholder="如：梦想"
                  autoComplete="off"
                  required
                />
                <label htmlFor={`${manualId}-replacement`}>替换为</label>
                <input
                  id={`${manualId}-replacement`}
                  value={replacement}
                  onChange={(event) => setReplacement(event.target.value)}
                  placeholder="如：梦境模拟"
                  autoComplete="off"
                  required
                />
                <button
                  className="term-translation-add"
                  type="submit"
                  disabled={
                    !source.trim() ||
                    !replacement.trim() ||
                    source.trim() === replacement.trim() ||
                    manualUnchanged
                  }
                >
                  <Icon name="plus" size={15} />
                  添加并替换
                </button>
              </form>

              {error && (
                <p className="term-translation-error" role="alert">
                  {error}
                </p>
              )}
              <p className="term-translation-status" role="status" aria-live="polite">
                {applied
                  ? `已保存“${lastApplied.source} → ${lastApplied.replacement}”的替换规则。`
                  : `已保存 ${rules.length} 条替换规则。`}
              </p>

              <label className="term-translation-search">
                <Icon name="search" size={16} />
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索原译法、替换词或英文"
                  aria-label="搜索术语替换"
                />
              </label>

              <section className="term-translation-section" aria-label="已保存的替换">
                <div className="term-translation-section-heading">
                  <h3>
                    已保存 <span>{rules.length}</span>
                  </h3>
                  {rules.length > 0 && (
                    <button
                      className="term-translation-text-button"
                      type="button"
                      onClick={() => setConfirmClear((value) => !value)}
                      aria-expanded={confirmClear}
                    >
                      全部恢复
                    </button>
                  )}
                </div>
                {confirmClear && rules.length > 0 && (
                  <div className="term-translation-clear-confirm">
                    <p>恢复全部原译法，并移除这 {rules.length} 条替换规则？</p>
                    <div>
                      <button type="button" onClick={() => setConfirmClear(false)}>
                        取消
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          onClear();
                          setLastApplied(undefined);
                          setConfirmClear(false);
                        }}
                      >
                        确认恢复
                      </button>
                    </div>
                  </div>
                )}
                {visibleRules.map((rule) => (
                  <TermRow
                    key={rule.id}
                    source={rule.source}
                    rules={rules}
                    onApply={apply}
                    onRemove={(id) => {
                      onRemove(id);
                      setLastApplied(undefined);
                    }}
                  />
                ))}
                {visibleRules.length === 0 && (
                  <p className="term-translation-empty">
                    {rules.length === 0
                      ? "还没有替换规则。可在上方添加，或修改下方译法。"
                      : "没有匹配的替换规则。"}
                  </p>
                )}
              </section>

              <section className="term-translation-section" aria-label="当前论文中的术语">
                <div className="term-translation-section-heading">
                  <h3>
                    当前论文 <span>{candidates.length}</span>
                  </h3>
                </div>
                <p className="term-translation-section-intro">
                  来自本论文的术语表；未匹配的条目也会保留。
                </p>
                {visibleCandidates.map((candidate) => (
                  <TermRow
                    key={candidate.source}
                    {...candidate}
                    enteredSource={enteredSources[candidate.source]}
                    onSourceChange={(value) =>
                      setEnteredSources((old) => ({ ...old, [candidate.source]: value }))
                    }
                    rules={rules}
                    onApply={apply}
                    onRemove={(id) => {
                      onRemove(id);
                      setLastApplied(undefined);
                    }}
                  />
                ))}
                {visibleCandidates.length === 0 && (
                  <p className="term-translation-empty">
                    {normalizedQuery
                      ? "没有匹配的论文术语。"
                      : "暂无可列出的术语。复制译文里的用词，在上方添加即可。"}
                  </p>
                )}
              </section>
            </div>
            <footer className="term-translation-footer">
              <details>
                <summary>替换说明</summary>
                <p>规则仅替换译文显示；不会改动原文、公式、代码和原译文。</p>
                <p>
                  译文段落改变后，已有勾画可能暂时隐藏；恢复相同译文后重新显示，笔记仍保留。
                </p>
              </details>
            </footer>
          </aside>
          <button
            className="term-sidebar-tab"
            aria-label={open ? "收起术语替换" : "展开术语替换"}
            aria-expanded={open}
            aria-controls="term-translation-sidebar"
            title={open ? "收起术语替换" : "术语替换"}
            onClick={open ? onClose : onOpen}
          >
            <Icon name="chevron" size={16} />
          </button>
        </div>
      </div>
    </>
  );
}
