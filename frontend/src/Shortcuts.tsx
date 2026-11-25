import { useEffect, useRef } from "react";
import { useFocusTrap } from "./dialog";
import { Icon } from "./icons";
import { READING_MODES } from "./readerNavigation";

/** True while the user is typing, so single-key shortcuts stay out of the way. */
export const isTyping = (event: KeyboardEvent) => {
  const el = event.target as HTMLElement | null;
  return Boolean(
    el?.closest?.(
      "input, textarea, select, [contenteditable=''], [contenteditable='true']",
    ),
  );
};

const groups: [string, [string[], string][]][] = [
  [
    "通用",
    [
      [["Ctrl", "B"], "显示或收起侧栏"],
      [["Ctrl", "K"], "搜索论点与原文"],
      ...READING_MODES.map(([, , title], index): [string[], string] => [
        [String(index + 1)],
        title,
      ]),
      [["Esc"], "关闭弹层，取消选中"],
      [["?"], "打开本页"],
    ],
  ],
  [
    "导图",
    [
      [["↑", "↓", "←", "→"], "平移画布"],
      [["拖动"], "平移画布（从节点上拖也可以）"],
      [["+", "−", "0"], "放大、缩小、适应窗口"],
      [["Tab"], "逐个聚焦论点"],
      [["←", "→"], "聚焦论点时：上级 / 下级"],
      [["↑", "↓"], "聚焦论点时：上一个 / 下一个同级"],
      [["Enter"], "打开论点详情；再按一次进入专注阅读"],
      [["空格"], "只打开详情"],
    ],
  ],
  [
    "连续阅读",
    [
      [["J"], "下一段"],
      [["K"], "上一段（段落较长时先回到段首）"],
    ],
  ],
  [
    "专注阅读",
    [
      [["←", "→"], "按论证顺序上一个 / 下一个"],
      [["↑", "↓"], "上级论点 / 第一个下级论点"],
      [["Esc"], "回到导图，并停在刚读完的论点"],
    ],
  ],
  [
    "问一问与对话",
    [
      [["/"], "阅读论文时打开问一问"],
      [["@"], "在输入框中引用论文、术语或勾画"],
      [["Enter"], "发送；Shift Enter 换行"],
      [["Esc"], "关闭引用列表，再按一次关闭问一问"],
    ],
  ],
];

export function Shortcuts({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  useFocusTrap(dialog);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || e.isComposing) return;
      e.preventDefault();
      e.stopPropagation();
      onClose();
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="shortcut-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="键盘快捷键"
        tabIndex={-1}
        ref={dialog}
        onClick={(e) => e.stopPropagation()}
      >
        <header>
          <strong>键盘快捷键</strong>
          <button className="icon-button small" aria-label="关闭" onClick={onClose}>
            <Icon name="close" size={15} />
          </button>
        </header>
        {groups.map(([title, rows]) => (
          <section key={title}>
            <h3>{title}</h3>
            <dl>
              {rows.map(([keys, label]) => (
                <div key={label}>
                  <dt>
                    {keys.map((k, i) =>
                      k === "–" ? <span key={i}>–</span> : <kbd key={i}>{k}</kbd>,
                    )}
                  </dt>
                  <dd>{label}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </div>
  );
}
