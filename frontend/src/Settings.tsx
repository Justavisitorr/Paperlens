import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";
import { Icon } from "./icons";
import { CURRENCIES, normalizeCurrency, exchangeNote, useMoney } from "./currency";
import { ModelConnections } from "./ModelConnections";
import {
  getSettings,
  saveSettings,
  type Health,
  type ProcessingProfileId,
  type SettingsField,
  type SettingsSchema,
  type SettingsGroup,
} from "./api";
import { PALETTES, interfaceFontSize, useMedia, type Prefs } from "./prefs";
import { tierPreview } from "./tiers";
import {
  go,
  parseRoute,
  useHash,
  SETTINGS_TARGET_EVENT,
  type SettingsTab,
  type SettingsSection,
} from "./router";
import { revealSettingsSection } from "./settingsNavigation";
import {
  CITATION_FORMATS,
  normalizeCitationFormat,
  saveCitationFormat,
  useCitationCopyFormat,
} from "./citationFormats";
import {
  SOURCE_PREFERENCES,
  normalizeSourcePreference,
  saveSourcePreference,
  useSourcePreference,
} from "./referenceSources";
import {
  defaultReadingPreferences,
  readLocal,
  readingPreferences,
  readingMeasure,
  useAvailableReadingWidth,
  writeLocal,
  type ReadingPreferences,
} from "./readingExperience";
import "./styles/20-settings.css";

type Tab = SettingsTab;
const TABS: [Tab, string, string][] = [
  ["appearance", "外观", "自动保存到当前浏览器。"],
  ["model", "模型连接", "连接服务商，选择要用的模型。"],
  ["notes", "自动笔记", "选择笔记类型、数量和参考范围。"],
  ["processing", "论文处理", "设置新论文的处理方式。"],
  ["catalog", "参考价格", "导入前估算费用、处理记录折算花费，并判断模型能否看图。"],
  ["about", "本地服务", "查看运行状态与文件位置。"],
  ["experimental", "实验功能", "未来的新功能将在这里开放试用。"],
];
/** Which server groups each tab shows. */
const TAB_GROUPS: Partial<Record<Tab, string[]>> = {
  model: ["model"],
  notes: ["auto_notes"],
  processing: ["translation", "limits", "files", "history"],
  experimental: ["experimental"],
  catalog: ["catalog"],
};
const TAB_ICONS = {
  appearance: "sun",
  model: "bolt",
  notes: "bookmark",
  processing: "file",
  experimental: "sparkle",
  catalog: "book",
  about: "info",
} as const;
const BASIC_PROCESSING_FIELDS = new Set([
  "PAPERLENS_AUTO_TRANSLATE_ENABLED",
  "PAPERLENS_JOB_HISTORY_LIMIT",
  "PAPERLENS_MATH_ENABLED",
  "PAPERLENS_TEXT_REVIEW_ENABLED",
  "LLM_MAX_REQUESTS",
  "LLM_MAX_OUTPUT_TOKENS",
  "PAPERLENS_REFERENCE_NETWORK",
]);
/** Unknown server groups remain editable under processing rather than disappearing. */
export function settingsGroupsForTab(groups: SettingsGroup[], tab: Tab) {
  const known = Object.values(TAB_GROUPS).flat();
  return groups.filter(
    (group) =>
      group.id !== "experimental" && group.id !== "tiered" && (TAB_GROUPS[tab]?.includes(group.id) ||
      (tab === "processing" && !known.includes(group.id))),
  );
}

const TAB_KEY = "paperlens.settingsTab";
const TIER_NAMES: [ProcessingProfileId, string][] = [
  ["economy", "经济"],
  ["standard", "标准"],
  ["detailed", "精细"],
];

type Choice<T extends string> = [T, string][];
function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Choice<T>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map(([id, text]) => (
        <button
          key={id}
          role="radio"
          aria-checked={value === id}
          onClick={() => onChange(id)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

/** One settings line: what it is on the left, the control on the right. */
function Row({
  title,
  hint,
  children,
  htmlFor,
}: {
  title: string;
  hint?: ReactNode;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="setting-row">
      <div className="setting-text">
        {htmlFor ? (
          <label className="setting-title" htmlFor={htmlFor}>
            {title}
          </label>
        ) : (
          <span className="setting-title">{title}</span>
        )}
        {hint && <small className="setting-hint">{hint}</small>}
      </div>
      <div className="setting-control">{children}</div>
    </div>
  );
}

export function Settings({
  prefs,
  onPrefs,
  server,
  health,
  storagePath,
  active = true,
}: {
  prefs: Prefs;
  onPrefs: (patch: Partial<Prefs>) => void;
  server: boolean;
  health: Health | null;
  storagePath?: string;
  /** App keeps this mounted to retain drafts, including secrets, in memory only. */
  active?: boolean;
}) {
  const hash = useHash();
  const compactTabs = useMedia("(max-width: 900px)");
  const pageRef = useRef<HTMLDivElement>(null);
  const [target, setTarget] = useState<{ section: SettingsSection } | null>(null);
  const tabs = TABS.filter(([id]) => server || id === "appearance" || id === "notes");
  const [tab, setTab] = useState<Tab>(() => {
    const route = parseRoute(hash);
    if (route?.page === "settings" && route.tab && tabs.some(([id]) => id === route.tab))
      return route.tab;
    try {
      const saved = sessionStorage.getItem(TAB_KEY) as Tab | null;
      return tabs.some(([id]) => id === saved) ? saved! : "appearance";
    } catch {
      return "appearance";
    }
  });
  const choose = (next: Tab) => {
    setTab(next);
    if (next === "model") setModelVisited(true);
    try {
      sessionStorage.setItem(TAB_KEY, next);
    } catch {
      /* the tab simply isn't remembered */
    }
    setTarget(null);
    go({ page: "settings", tab: next });
  };
  const [modelVisited, setModelVisited] = useState(tab === "model");
  useEffect(() => {
    if (!active) return;
    const navigate = () => {
      const route = parseRoute(location.hash);
      if (route?.page !== "settings") return;
      if (route.tab && tabs.some(([id]) => id === route.tab)) {
        setTab(route.tab);
        if (route.tab === "model") setModelVisited(true);
      }
      setTarget(route.section ? { section: route.section } : null);
    };
    navigate();
    window.addEventListener(SETTINGS_TARGET_EVENT, navigate);
    return () => window.removeEventListener(SETTINGS_TARGET_EVENT, navigate);
  }, [hash, active, server]);
  useEffect(() => {
    if (!active || !compactTabs) return;
    pageRef.current
      ?.querySelector<HTMLButtonElement>(`#settings-tab-${tab}`)
      ?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "auto" });
  }, [tab, compactTabs, active]);
  useEffect(() => {
    if (!active || !target || !pageRef.current) return;
    return revealSettingsSection(pageRef.current, target.section);
  }, [target, tab, active]);
  const [serviceDirty, setServiceDirty] = useState(0);
  const [modelDirty, setModelDirty] = useState(false);
  useEffect(() => {
    if (!serviceDirty && !modelDirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [serviceDirty, modelDirty]);
  const connections = Boolean(health?.features?.includes("connections"));
  const intro = TABS.find(([id]) => id === tab)?.[2];
  return (
    <div className="page settings-page" ref={pageRef}>
      {/* The title and tabs stay put while a long tab scrolls under them. */}
      <div className="settings-top">
        <header className="page-head">
          <div>
            <h1>设置</h1>
            <p>把阅读和处理方式调成适合你的样子。</p>
          </div>
          {(serviceDirty > 0 || modelDirty) && (
            <span className="status-tag settings-unsaved">有未保存的改动</span>
          )}
        </header>
      </div>
      <div className="settings-layout">
        <div
          className="settings-tabs"
          role="tablist"
          aria-label="设置分类"
          aria-orientation={compactTabs ? "horizontal" : "vertical"}
        >
          {tabs.map(([id, label]) => (
            <button
              key={id}
              role="tab"
              id={`settings-tab-${id}`}
              aria-selected={tab === id}
              aria-controls="settings-panel"
              tabIndex={tab === id ? 0 : -1}
              onClick={() => choose(id)}
              onKeyDown={(event) => {
                const index = tabs.findIndex(([key]) => key === id);
                const step = ["ArrowRight", "ArrowDown"].includes(event.key)
                  ? 1
                  : ["ArrowLeft", "ArrowUp"].includes(event.key)
                    ? -1
                    : 0;
                const next =
                  event.key === "Home"
                    ? tabs[0][0]
                    : event.key === "End"
                      ? tabs[tabs.length - 1][0]
                      : step
                        ? tabs[(index + step + tabs.length) % tabs.length][0]
                        : undefined;
                if (!next) return;
                event.preventDefault();
                choose(next);
                pageRef.current
                  ?.querySelector<HTMLButtonElement>(`#settings-tab-${next}`)
                  ?.focus();
              }}
            >
              <Icon name={TAB_ICONS[id]} size={17} />
              <span>{label}</span>
              {id === "model" && modelDirty && (
                <span
                  className="settings-dirty-dot"
                  aria-hidden="true"
                  title="模型分配未保存"
                />
              )}
            </button>
          ))}
        </div>
        <div
          className="settings-panel"
          id="settings-panel"
          role="tabpanel"
          aria-labelledby={`settings-tab-${tab}`}
        >
          <p className="settings-intro">{tab === "appearance" && window.paperLensCloudReady ? "自动保存，并同步到你的账户。" : intro}</p>
          {tab === "appearance" && <Appearance prefs={prefs} onPrefs={onPrefs} />}
          {tab === "experimental" && <p className="settings-empty">目前暂无实验功能</p>}
          {tab === "notes" && (
            <section className="settings-group">
              <h2>我的笔记</h2>
              <p>
                所有笔记都可点击「AI
                回复」，使用当前配置的模型。保存笔记不会自动发送请求。
              </p>
            </section>
          )}
          {tab === "notes" && !server && (
            <section className="settings-group">
              <h2>自动笔记</h2>
              <p>摘要、解释、联系、提问与核对。</p>
              <p>请在本地服务中设置自动笔记。</p>
            </section>
          )}
          {connections && modelVisited && (
            <div hidden={tab !== "model"}>
              <ModelConnections onDirtyChange={setModelDirty} />
            </div>
          )}
          {/* Mounted once for every server tab so unsaved edits survive switching tabs. */}
          {server && (
            <ServiceSettings
              tab={tab}
              connections={connections}
              onDirtyChange={setServiceDirty}
            />
          )}
          {tab === "about" && server && (
            <section className="settings-group">
              <dl className="kv">
                <div>
                  <dt>状态</dt>
                  <dd>{health ? (health.ok ? "运行中" : "异常") : "未连接"}</dd>
                </div>
                {health?.version && (
                  <div>
                    <dt>版本</dt>
                    <dd>{health.version}</dd>
                  </div>
                )}
                <div>
                  <dt>处理队列</dt>
                  <dd>
                    {health
                      ? `正在处理 ${health.running_jobs} 篇，排队 ${health.queued_jobs} 篇`
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt>保存位置</dt>
                  <dd>{storagePath || "服务输出目录（outputs/）"}</dd>
                </div>
              </dl>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

export function CitationCopySettings() {
  const format = useCitationCopyFormat();
  const [saved, setSaved] = useState(true);
  const selected = CITATION_FORMATS.find((item) => item.value === format)!;
  return (
    <section className="settings-group">
      <h2>引用复制</h2>
      <Row title="复制引用的格式" htmlFor="citation-copy-format" hint={selected.hint}>
        <select
          id="citation-copy-format"
          value={format}
          onChange={(event) =>
            setSaved(saveCitationFormat(normalizeCitationFormat(event.target.value)))
          }
        >
          {CITATION_FORMATS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </Row>
      <p className="setting-hint">
        BibTeX 使用 @misc 条目保留原始引文，不推测缺失的作者、标题和年份。
        切换格式后，同一文献的引用键保持一致。
      </p>
      {!saved && (
        <p role="status">浏览器未允许保存设置；本次打开的页面仍使用所选格式。</p>
      )}
    </section>
  );
}

export function ReferenceSourceSettings() {
  const preference = useSourcePreference();
  const [saved, setSaved] = useState(true);
  return (
    <section className="settings-group">
      <h2>参考文献来源</h2>
      <Row
        title="首选来源"
        htmlFor="reference-source-preference"
        hint="其余来源按 DOI、可识别的出版网站、arXiv、文献提供的其他链接、学术搜索的固定次序排列。"
      >
        <select
          id="reference-source-preference"
          value={preference}
          onChange={(event) => setSaved(saveSourcePreference(normalizeSourcePreference(event.target.value)))}
        >
          {SOURCE_PREFERENCES.map((item) => (
            <option key={item.value} value={item.value}>{item.label}</option>
          ))}
        </select>
      </Row>
      {!saved && <p role="status">浏览器未允许保存设置；本次打开的页面仍使用所选来源。</p>}
    </section>
  );
}

function Appearance({
  prefs,
  onPrefs,
}: {
  prefs: Prefs;
  onPrefs: (patch: Partial<Prefs>) => void;
}) {
  const systemDark = useMedia("(prefers-color-scheme: dark)");
  const active =
    prefs.theme === "dark" || (prefs.theme === "system" && systemDark) ? "dark" : "light";
  return (
    <>
      <p className="settings-local-status">
        <Icon name="check" size={14} /> {window.paperLensCloudReady ? "改动已保存；同步状态见侧栏“账户与资料”。" : "改动已自动保存到当前浏览器。"}
      </p>
      <ReadingAppearance />
      <CitationCopySettings />
      <ReferenceSourceSettings />
      <CurrencySettings prefs={prefs} onPrefs={onPrefs} />
      <section className="settings-group">
        <Row title="主题" hint="明亮与暗色分别保存配色。">
          <Segmented
            label="主题"
            value={prefs.theme}
            options={[
              ["light", "明亮"],
              ["dark", "暗色"],
              ["system", "跟随系统"],
            ]}
            onChange={(theme) => onPrefs({ theme })}
          />
        </Row>
        {(["light", "dark"] as const).map((branch) => (
          <div className="palette-branch" key={branch}>
            <h3>
              {branch === "light" ? "明亮配色" : "暗色配色"}
              {active === branch && <span className="status-tag">正在使用</span>}
            </h3>
            <div
              className="palette-grid"
              role="radiogroup"
              aria-label={branch === "light" ? "明亮配色" : "暗色配色"}
            >
              {PALETTES[branch].map(([id, name, feel, [page, side, ink, accent]]) => {
                const selected =
                  (branch === "light" ? prefs.lightPalette : prefs.darkPalette) === id;
                return (
                  <button
                    key={id}
                    role="radio"
                    aria-checked={selected}
                    className="palette-card"
                    onClick={() =>
                      onPrefs(
                        branch === "light"
                          ? { lightPalette: id as Prefs["lightPalette"] }
                          : { darkPalette: id as Prefs["darkPalette"] },
                      )
                    }
                  >
                    <span
                      className="palette-preview"
                      style={{ background: page, borderColor: side }}
                      aria-hidden="true"
                    >
                      <span className="palette-side" style={{ background: side }} />
                      <span className="palette-line" style={{ background: ink }} />
                      <span
                        className="palette-line short"
                        style={{ background: ink, opacity: 0.45 }}
                      />
                      <span className="palette-pill" style={{ background: accent }} />
                    </span>
                    <strong>{name}</strong>
                    <small>{feel}</small>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </section>
      <section className="settings-group">
        <h2>界面</h2>
        <Row title="界面字号" hint="调整菜单、按钮与说明的大小。">
          <InterfaceFontSize
            value={prefs.uiFontSize}
            onChange={(uiFontSize) => onPrefs({ uiFontSize })}
          />
        </Row>
        <Row title="界面动效" hint="减少切换和展开时的动画。">
          <Segmented
            label="界面动效"
            value={prefs.motion}
            options={[
              ["full", "完整"],
              ["reduced", "减少"],
            ]}
            onChange={(motion) => onPrefs({ motion })}
          />
        </Row>
        <Row title="色弱配色" hint="用颜色和线型区分导图关系。">
          <Segmented
            label="色弱配色"
            value={prefs.colorVision}
            options={[
              ["standard", "关闭"],
              ["cvd", "开启"],
            ]}
            onChange={(colorVision) => onPrefs({ colorVision })}
          />
        </Row>
      </section>
      <section className="settings-group">
        <h2>阅读入口</h2>
        <Row
          title="阅读全文"
          hint="点击阅读全文时优先打开此视图；从侧栏或新论文进入时先看阅读总览。"
        >
          <Segmented
            label="阅读全文默认视图"
            value={prefs.fullReadMode}
            options={[
              ["map", "导图"],
              ["text", "全文"],
            ]}
            onChange={(fullReadMode) => onPrefs({ fullReadMode })}
          />
        </Row>
        <h2>导图</h2>
        <Row title="关联线" hint="论点之间的支持、限制、推导关系。">
          <Segmented
            label="导图关联线"
            value={prefs.showRelations ? "all" : "focus"}
            options={[
              ["focus", "选中内容"],
              ["all", "全部显示"],
            ]}
            onChange={(v) => onPrefs({ showRelations: v === "all" })}
          />
        </Row>
        <Row title="展开层级" hint="内容较多时，自动折叠导图。">
          <Segmented
            label="导图初始展开"
            value={prefs.mapDepth}
            options={[
              ["auto", "自动"],
              ["first", "第一层"],
              ["all", "全部"],
            ]}
            onChange={(mapDepth) => onPrefs({ mapDepth })}
          />
        </Row>
      </section>
    </>
  );
}

export function CurrencySettings({
  prefs,
  onPrefs,
}: {
  prefs: Prefs;
  onPrefs: (patch: Partial<Prefs>) => void;
}) {
  const money = useMoney();
  return (
    <section className="settings-group">
      <h2>费用显示</h2>
      <Row
        title="显示币种"
        htmlFor="display-currency"
        hint="用于费用预估与处理记录，自动保存。服务商结算币种不变。"
      >
        <select
          id="display-currency"
          value={prefs.currency}
          onChange={(event) =>
            onPrefs({ currency: normalizeCurrency(event.target.value) })
          }
        >
          {Object.entries(CURRENCIES).map(([code, name]) => (
            <option key={code} value={code}>
              {name}（{code}）
            </option>
          ))}
        </select>
      </Row>
      {exchangeNote(money) && <p className="footnote">{exchangeNote(money)}</p>}
    </section>
  );
}

export function InterfaceFontSize({
  value,
  onChange,
}: {
  value: number;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const next = draft.trim() === "" ? value : interfaceFontSize(Number(draft), value);
    setDraft(String(next));
    if (next !== value) onChange(next);
  };
  return (
    <label className="settings-font-size">
      <input
        aria-label="界面字号"
        type="number"
        min="12"
        max="24"
        step="1"
        value={draft}
        onChange={(event) => {
          const text = event.target.value;
          setDraft(text);
          const size = event.target.valueAsNumber;
          if (Number.isInteger(size) && size >= 12 && size <= 24) onChange(size);
        }}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
      />
      <span>px</span>
    </label>
  );
}

function ReadingAppearance() {
  const [reading, setReading] = useState(() =>
    readingPreferences(readLocal("paperlens:reading-type")),
  );
  const preview = useRef<HTMLDivElement>(null);
  const article = useRef<HTMLElement>(null);
  const availableWidth = useAvailableReadingWidth(preview, article);
  const measure = readingMeasure(reading, availableWidth);
  const update = (patch: Partial<ReadingPreferences>) => {
    const next = readingPreferences({ ...reading, ...patch });
    setReading(next);
    writeLocal("paperlens:reading-type", next);
  };
  return (
    <section className="settings-group reading-appearance">
      <div className="settings-group-heading">
        <div>
          <h2>正文排版</h2>
          <p className="group-desc">与阅读页共用排版设置。</p>
        </div>
        <button className="text-button" onClick={() => update(defaultReadingPreferences)}>
          恢复默认
        </button>
      </div>
      <div className="settings-reading-controls">
        {(
          [
            ["size", "字号", 15, 24, 1, "px"],
            ["measure", "每行宽度", 26, 46, 1, "字"],
            ["leading", "行距", 1.5, 2.3, 0.1, ""],
          ] as const
        ).map(([key, label, min, max, step, unit]) => (
          <label key={key} htmlFor={`reading-setting-${key}`}>
            <span>
              {label}
              <output>
                {key === "leading"
                  ? reading[key].toFixed(1)
                  : key === "measure"
                    ? measure.value
                    : reading[key]}{" "}
                {unit}
                {key === "measure" && measure.fitted && " · 适宽"}
              </output>
            </span>
            <input
              id={`reading-setting-${key}`}
              type="range"
              min={key === "measure" ? measure.min : min}
              max={key === "measure" ? measure.max : max}
              step={step}
              value={key === "measure" ? measure.value : reading[key]}
              disabled={key === "measure" && measure.disabled}
              onChange={(event) => update({ [key]: Number(event.target.value) })}
            />
          </label>
        ))}
        <label htmlFor="reading-setting-font">
          <span>字体</span>
          <select
            id="reading-setting-font"
            value={reading.font}
            onChange={(event) =>
              update({ font: event.target.value === "sans" ? "sans" : "serif" })
            }
          >
            <option value="serif">宋体</option>
            <option value="sans">黑体</option>
          </select>
        </label>
      </div>
      {measure.fitted && (
        <p className="settings-measure-hint">
          当前空间适宽显示，保留 {reading.measure} 字的宽度偏好。
        </p>
      )}
      <ReadingPreview
        reading={reading}
        measure={measure.value}
        containerRef={preview}
        articleRef={article}
      />
    </section>
  );
}

export function ReadingPreview({
  reading,
  measure = reading.measure,
  containerRef,
  articleRef,
}: {
  reading: ReadingPreferences;
  measure?: number;
  containerRef?: RefObject<HTMLDivElement | null>;
  articleRef?: RefObject<HTMLElement | null>;
}) {
  return (
    <div
      className="settings-reading-preview"
      ref={containerRef}
      style={
        {
          "--preview-size": `${reading.size}px`,
          "--preview-measure": `${measure}em`,
          "--preview-leading": reading.leading,
        } as CSSProperties
      }
      data-font={reading.font}
    >
      <div className="settings-preview-caption">
        <Icon name="book" size={14} />
        <span>效果预览</span>
        <small>示例文字</small>
      </div>
      <article ref={articleRef}>
        <h3>从问题到证据</h3>
        <p>
          阅读一篇论文，先找到作者要解决的问题，再沿着方法和实验理解结论。将关键论点与原文证据放在一起，能让推理过程更容易核对。
        </p>
        <p>背景提供问题的来由，方法说明如何回答，实验则给出可检验的结果。</p>
      </article>
    </div>
  );
}

function ServiceSettings({
  tab,
  connections,
  onDirtyChange,
}: {
  tab: Tab;
  connections: boolean;
  onDirtyChange: (count: number) => void;
}) {
  const [schema, setSchema] = useState<SettingsSchema | null | undefined>(undefined);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [status, setStatus] = useState("");
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    getSettings().then(setSchema, (e) => setLoadError(String(e.message || e)));
  }, []);
  const wanted = TAB_GROUPS[tab] || [];
  const dirty = Object.keys(values).length;
  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);
  if (!wanted.length && !dirty) return null;

  if (loadError) return <p className="error-line">{loadError}</p>;
  if (schema === undefined) return <p className="muted">正在读取…</p>;
  if (schema === null)
    return (
      <div className="notice static">
        <Icon name="info" size={15} />
        <span>
          模型、并发和价格等参数写在项目根目录的 <code>.env</code> 文件里。
        </span>
      </div>
    );

  const connectionFields = new Set([
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "PAPERLENS_VISION_MODEL",
    "PAPERLENS_VISION_ALLOW_UNVERIFIED",
  ]);
  const allFields = schema.groups.flatMap((g) => g.fields);
  const current = (field: SettingsField) =>
    field.key in values ? values[field.key] : field.value;
  const outputCap = allFields.find((f) => f.key === "LLM_MAX_OUTPUT_TOKENS");
  const groups = settingsGroupsForTab(schema.groups, tab).filter(
    (group) => group.fields.length,
  );
  const change = (field: SettingsField, value: unknown) => {
    setValues((old) => {
      const next = { ...old };
      if (value === field.value || (field.type === "secret" && value === ""))
        delete next[field.key];
      else next[field.key] = value;
      return next;
    });
    setStatus("");
    setErrors((old) => {
      const next = { ...old };
      delete next[field.key];
      return next;
    });
  };
  const renderField = (field: SettingsField) => (
    <FieldInput
      key={field.key}
      field={{
        ...field,
        readonly: field.readonly || saving,
      }}
      value={current(field)}
      error={errors[field.key]}
      outputCap={
        field.key === "PAPERLENS_READING_OUTPUT_TOKENS" && outputCap
          ? Number(current(outputCap))
          : undefined
      }
      onChange={(value) => change(field, value)}
    />
  );
  const save = async () => {
    setSaving(true);
    setStatus("");
    setErrors({});
    try {
      const result = await saveSettings(values);
      if (result.errors && Object.keys(result.errors).length) {
        setErrors(result.errors);
        setStatus("有设置没通过检查，请看标红的项目。");
      } else {
        setValues({});
        setStatus("已保存。");
        setSchema(result.schema || (await getSettings()));
      }
    } catch (e) {
      setStatus(`没能保存：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };
  return (
    <>
      {tab === "processing" && (
        <div className="settings-processing-note">
          <Icon name="info" size={16} />
          <p>
            所有论文均由 AI 读取原 PDF 或原页图像。读取模型在「模型连接」中配置。
          </p>
        </div>
      )}
      {groups.map((group) => {
        const basic = group.fields.filter((field) =>
          tab === "processing"
            ? BASIC_PROCESSING_FIELDS.has(field.key)
            : !connections || !connectionFields.has(field.key),
        );
        const advanced = group.fields.filter((field) => !basic.includes(field));
        return (
          <section
            className="settings-group"
            key={group.id}
            data-settings-section={group.id}
          >
            <h2>{group.title}</h2>
            {group.description && <p className="group-desc">{group.description}</p>}
            {basic.map(renderField)}
            {advanced.length > 0 && (
              <details
                className="settings-advanced"
                open={advanced.some((field) => Boolean(errors[field.key])) || undefined}
              >
                <summary>
                  {tab === "model" ? "默认连接" : "更多设置"}
                  <span>{advanced.length} 项</span>
                </summary>
                {tab === "model" && (
                  <p className="group-desc">未分配模型时，使用此连接。</p>
                )}
                {advanced.map(renderField)}
              </details>
            )}
          </section>
        );
      })}
      {(groups.length > 0 || dirty > 0) && (
        <div className="save-bar">
          <span role="status">
            {status ||
              (dirty
                ? `有 ${dirty} 项改动还没保存`
                : schema.meta?.stale
                  ? "部分设置要重启本地服务才生效。"
                  : "参数已保存")}
          </span>
          <button
            className="button"
            disabled={!dirty || saving}
            onClick={() => {
              setValues({});
              setErrors({});
              setStatus("");
            }}
          >
            撤销改动
          </button>
          <button className="button primary" disabled={!dirty || saving} onClick={save}>
            {saving && <span className="spinner" />}保存设置
          </button>
        </div>
      )}
    </>
  );
}

function FieldInput({
  field,
  value,
  error,
  disabledReason,
  outputCap,
  onChange,
}: {
  field: SettingsField;
  value: unknown;
  error?: string;
  disabledReason?: string;
  outputCap?: number;
  onChange: (value: unknown) => void;
}) {
  const id = `setting-${field.key}`;
  const common = {
    id,
    disabled: field.readonly || Boolean(disabledReason),
    "aria-invalid": Boolean(error),
    "aria-describedby":
      [
        field.help && `${id}-help`,
        disabledReason && `${id}-disabled`,
        error && `${id}-error`,
      ]
        .filter(Boolean)
        .join(" ") || undefined,
  };
  let control: ReactNode;
  if (field.type === "boolean")
    control = (
      <input
        {...common}
        type="checkbox"
        className="switch"
        checked={Boolean(value)}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  else if (field.type === "select")
    control = (
      <select
        {...common}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        {field.options?.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  else if (field.type === "textarea")
    control = (
      <textarea
        {...common}
        rows={3}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  else
    control = (
      <span className="input-wrap">
        <input
          {...common}
          type={
            field.type === "secret"
              ? "password"
              : field.type === "number"
                ? "number"
                : "text"
          }
          autoComplete="off"
          min={field.min}
          max={field.max}
          step={field.step}
          placeholder={
            field.type === "secret"
              ? field.configured
                ? "已保存，留空表示不改"
                : "还没填写"
              : field.placeholder
          }
          value={String(value ?? "")}
          onChange={(e) =>
            onChange(field.type === "number" ? Number(e.target.value) : e.target.value)
          }
        />
        {field.unit && <small>{field.unit}</small>}
      </span>
    );
  const number = Number(value);
  const tiers =
    field.tiers && Number.isFinite(number)
      ? tierPreview(number, {
          min: field.min,
          max: Math.min(field.max ?? Infinity, outputCap ?? Infinity),
        })
      : null;
  return (
    <div
      className={`setting-row field-row ${error ? "has-error" : ""} ${disabledReason ? "is-mode-disabled" : ""}`}
    >
      <div className="setting-text">
        <label className="setting-title" htmlFor={id}>
          {field.label}
        </label>
        {field.help && (
          <small className="setting-hint" id={`${id}-help`}>
            {field.help}
          </small>
        )}
        {disabledReason && (
          <small className="setting-disabled-reason" id={`${id}-disabled`}>
            {disabledReason}
          </small>
        )}
        {error && (
          <small className="field-error" id={`${id}-error`}>
            {error}
          </small>
        )}
      </div>
      <div className="setting-control">
        {control}
        {tiers && !disabledReason && (
          <small className="tier-values" aria-label={`${field.label}在各档位的数值`}>
            {TIER_NAMES.map(([tier, name]) => (
              <span key={tier} data-tier={tier}>
                {name} {tiers[tier] === 0 ? "不限" : tiers[tier].toLocaleString("zh-CN")}
              </span>
            ))}
          </small>
        )}
      </div>
    </div>
  );
}
