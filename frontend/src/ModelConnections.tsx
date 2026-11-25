import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import {
  deleteModelAccount,
  getConnections,
  refreshAccountModels,
  saveModelAccount,
  saveModelRouting,
  type AccountInput,
  type ModelAccount,
  type ModelAssignment,
  type ModelConnectionsState,
  type ModelProvider,
  type ModelRole,
  type ModelRouting,
  type RemoteModels,
} from "./api";
import "./styles/17-connections.css";

const auxiliaryRoles: ModelRole[] = [
  "reading",
  "verification",
  "vision",
  "chat",
  "conversation",
];
const roleNames: Record<ModelRole, string> = {
  main: "主模型",
  reading: "正文整理",
  verification: "核对原文",
  vision: "识别图表",
  chat: "阅读问答",
  conversation: "对话",
};
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

/** Credentials stay in this form until submitted, never in browser storage. */
export function ModelConnections({
  onDirtyChange,
}: { onDirtyChange?: (dirty: boolean) => void } = {}) {
  const [data, setData] = useState<ModelConnectionsState | null>(null);
  const [routing, setRouting] = useState<ModelRouting | null>(null);
  const [loadError, setLoadError] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [fetching, setFetching] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<ModelAccount | "new" | null>(null);
  const [accountDraft, setAccountDraft] = useState(false);
  const attempted = useRef(new Set<string>());
  const dirty = Boolean(
    data && routing && JSON.stringify(routing) !== JSON.stringify(data.routing),
  );
  useEffect(() => {
    onDirtyChange?.(dirty || accountDraft);
  }, [dirty, accountDraft, onDirtyChange]);

  useEffect(() => {
    getConnections().then(
      (value) => {
        setData(value);
        setRouting(value.routing);
        if (
          !value.accounts.some(
            (a) =>
              a.configured ||
              value.providers.find((p) => p.id === a.provider_id)?.requires_key === false,
          )
        )
          setEditing("new");
      },
      (error) => setLoadError(errorText(error)),
    );
  }, []);

  const refresh = useCallback(async (id: string): Promise<RemoteModels | undefined> => {
    attempted.current.add(id);
    setFetching((old) => new Set(old).add(id));
    try {
      const models = await refreshAccountModels(id);
      setData((old) =>
        old
          ? {
              ...old,
              accounts: old.accounts.map((a) => (a.id === id ? { ...a, models } : a)),
            }
          : old,
      );
      return models;
    } catch (error) {
      const message = errorText(error);
      setData((old) =>
        old
          ? {
              ...old,
              accounts: old.accounts.map((a) =>
                a.id === id
                  ? { ...a, models: { ...a.models, status: "failed", error: message } }
                  : a,
              ),
            }
          : old,
      );
      return undefined;
    } finally {
      setFetching((old) => {
        const next = new Set(old);
        next.delete(id);
        return next;
      });
    }
  }, []);

  // Fetch selected connections once when no list exists. A failure is retried by the user.
  useEffect(() => {
    if (!data || !routing) return;
    for (const assignment of Object.values(routing)) {
      const account = data.accounts.find((a) => a.id === assignment?.account_id);
      if (account && !account.models.items.length && !attempted.current.has(account.id))
        void refresh(account.id);
    }
  }, [data, routing, refresh]);

  const updateRole = (role: ModelRole, assignment: ModelAssignment | null) => {
    setRouting((old) => (old ? { ...old, [role]: assignment } : old));
    setStatus("");
  };
  const saveAccount = async (body: AccountInput, id?: string) => {
    setBusy(true);
    setStatus("");
    try {
      const { account } = await saveModelAccount(body, id);
      setData((old) =>
        old
          ? {
              ...old,
              accounts: [...old.accounts.filter((a) => a.id !== account.id), account],
            }
          : old,
      );
      setEditing(null);
      const models = await refresh(account.id);
      if (!id) {
        setRouting((old) => {
          if (!old) return old;
          const current = data?.accounts.find((a) => a.id === old.main?.account_id);
          if (current?.configured && old.main?.model) return old;
          return {
            ...old,
            main: { account_id: account.id, model: models?.items[0]?.id || "" },
          };
        });
        setStatus("连接已保存，请选择模型。");
      } else setStatus("连接已更新，已重新获取模型列表。");
    } catch (error) {
      setStatus(`连接未保存：${errorText(error)}`);
    } finally {
      setBusy(false);
    }
  };
  const removeAccount = async (account: ModelAccount) => {
    if (
      !window.confirm(
        `删除连接“${account.name}”及其本机保存的密钥？正在使用的连接需要先重新分配模型。`,
      )
    )
      return;
    setBusy(true);
    try {
      await deleteModelAccount(account.id);
      setData((old) =>
        old ? { ...old, accounts: old.accounts.filter((a) => a.id !== account.id) } : old,
      );
      setRouting((old) => {
        if (!old) return old;
        return Object.fromEntries(
          Object.entries(old).map(([role, assignment]) => [
            role,
            assignment?.account_id === account.id ? null : assignment,
          ]),
        ) as ModelRouting;
      });
      setStatus("连接已删除。");
    } catch (error) {
      setStatus(errorText(error));
    } finally {
      setBusy(false);
    }
  };
  const saveRouting = async () => {
    if (!routing) return;
    setBusy(true);
    setStatus("");
    try {
      const result = await saveModelRouting(routing);
      setRouting(result.routing);
      setData((old) => (old ? { ...old, routing: result.routing } : old));
      setStatus("已保存，下次处理或提问时生效。");
    } catch (error) {
      setStatus(`分配未保存：${errorText(error)}`);
    } finally {
      setBusy(false);
    }
  };

  if (loadError)
    return (
      <section className="settings-group">
        <h2>模型连接</h2>
        <p className="error-line" role="alert">
          {loadError}
        </p>
      </section>
    );
  if (!data || !routing)
    return (
      <section className="settings-group">
        <h2>模型连接</h2>
        <p className="muted">正在读取连接…</p>
      </section>
    );
  const valid =
    Boolean(routing.main?.model && routing.main.account_id) &&
    Object.values(routing).every((a) => a === null || Boolean(a.model && a.account_id));
  const supportedRoles = auxiliaryRoles.filter((role) =>
    data.roles.some((item) => item.id === role),
  );
  return (
    <div className="model-connections">
      <section className="settings-group">
        <div className="connection-heading">
          <div>
            <h2>模型连接</h2>
            <p className="group-desc">添加服务商后，选择要用的模型。</p>
          </div>
          <button className="button" disabled={busy} onClick={() => setEditing("new")}>
            <Icon name="plus" size={14} />
            添加连接
          </button>
        </div>
        <div className="connection-list">
          {data.accounts.map((account) => (
            <div key={account.id} className="connection-card">
              <div className="connection-card-info">
                <strong>{account.name}</strong>
                <small>
                  {data.providers.find((p) => p.id === account.provider_id)?.name ||
                    account.provider_id}{" "}
                  ·{" "}
                  {account.readonly
                    ? ".env 配置"
                    : account.configured
                      ? "密钥已保存"
                      : "未保存密钥"}
                </small>
                <small className="connection-url">{account.base_url}</small>
              </div>
              {!account.readonly && (
                <>
                  <button
                    className="button small"
                    disabled={busy}
                    onClick={() => setEditing(account)}
                  >
                    编辑<span className="sr-only"> {account.name}</span>
                  </button>
                  <button
                    className="icon-button small"
                    disabled={busy}
                    aria-label={`删除连接 ${account.name}`}
                    onClick={() => void removeAccount(account)}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
        {editing && (
          <AccountEditor
            key={editing === "new" ? "new" : editing.id}
            providers={data.providers}
            account={editing === "new" ? undefined : editing}
            busy={busy}
            onCancel={() => setEditing(null)}
            onSave={saveAccount}
            onDirtyChange={setAccountDraft}
          />
        )}
        <p className="tiny-note">密钥只保存在本机。</p>
      </section>
      <section className="settings-group">
        <h2>主模型</h2>
        <p className="group-desc">负责整理论文，也用于其他任务。</p>
        <AssignmentPicker
          role="main"
          assignment={routing.main}
          accounts={data.accounts}
          main={routing.main}
          onChange={(value) => updateRole("main", value)}
          onRefresh={refresh}
          fetching={fetching}
          disabled={busy}
        />
      </section>
      <details
        className="settings-group connection-auxiliary"
        open={supportedRoles.some((role) => routing[role] !== null) || undefined}
      >
        <summary>
          分别设置 <small>默认同主模型</small>
        </summary>
        <section>
          <div className="connection-heading">
            <div>
              <h2>辅助模型</h2>
              <p className="group-desc">按用途选择模型，也可共用。</p>
            </div>
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                setRouting({
                  ...routing,
                  ...Object.fromEntries(supportedRoles.map((role) => [role, null])),
                })
              }
            >
              同主模型
            </button>
          </div>
          {supportedRoles.map((role) => (
            <div className="model-role" key={role}>
              <h3>{roleNames[role]}</h3>
              <p className="tiny-note">
                {data.roles.find((item) => item.id === role)?.description}
              </p>
              <AssignmentPicker
                role={role}
                assignment={routing[role]}
                accounts={data.accounts}
                main={routing.main}
                onChange={(value) => updateRole(role, value)}
                onRefresh={refresh}
                fetching={fetching}
                disabled={busy}
              />
            </div>
          ))}
        </section>
      </details>
      <div className="connection-save">
        <p role="status">
          {status || (dirty ? "模型分配有改动，保存后生效。" : "模型分配已保存")}
        </p>
        <button
          className="button"
          disabled={!dirty || busy}
          onClick={() => {
            setRouting(data.routing);
            setStatus("");
          }}
        >
          还原分配
        </button>
        <button
          className="button primary"
          disabled={!dirty || !valid || busy}
          onClick={() => void saveRouting()}
        >
          {busy && <span className="spinner" />}保存分配
        </button>
      </div>
    </div>
  );
}

function AccountEditor({
  providers,
  account,
  busy,
  onCancel,
  onSave,
  onDirtyChange,
}: {
  providers: ModelProvider[];
  account?: ModelAccount;
  busy: boolean;
  onCancel: () => void;
  onSave: (body: AccountInput, id?: string) => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const first =
    providers.find((p) => p.id === (account?.provider_id || "deepseek")) || providers[0];
  const [providerId, setProviderId] = useState(first?.id || "");
  const [name, setName] = useState(account?.name || "");
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(account?.base_url || first?.base_url || "");
  const [protocol, setProtocol] = useState(
    account?.protocol || first?.protocol || "openai",
  );
  const provider = providers.find((p) => p.id === providerId);
  const custom = providerId === "custom" || providerId === "local";
  const draft =
    Boolean(key) ||
    name !== (account?.name || "") ||
    providerId !== first?.id ||
    baseUrl !== (account?.base_url || first?.base_url || "") ||
    protocol !== (account?.protocol || first?.protocol || "openai");
  useEffect(() => {
    onDirtyChange(draft);
  }, [draft, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  return (
    <form
      className="connection-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void onSave(
          {
            provider_id: providerId,
            name: name.trim() || provider?.name || "模型连接",
            api_key: key,
            base_url: baseUrl.trim(),
            protocol,
          },
          account?.id,
        );
      }}
    >
      <h3>{account ? "编辑连接" : "添加连接"}</h3>
      {draft && (
        <p className="tiny-note" role="status">
          连接改动尚未保存。
        </p>
      )}
      <div className="field">
        <label className="field-label" htmlFor="connection-provider">
          服务商
        </label>
        <select
          id="connection-provider"
          value={providerId}
          disabled={Boolean(account) || busy}
          onChange={(event) => {
            const next = providers.find((p) => p.id === event.target.value)!;
            setProviderId(next.id);
            setBaseUrl(next.base_url);
            setProtocol(next.protocol);
            setKey("");
          }}
        >
          {providers.map((p) => (
            <option value={p.id} key={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="connection-name">
          连接名称
        </label>
        <input
          id="connection-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={`例如：${provider?.name || "DeepSeek"} 个人账户`}
          maxLength={80}
          disabled={busy}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="connection-key">
          访问密钥{provider?.requires_key === false ? "（可选）" : ""}
        </label>
        <input
          id="connection-key"
          type="password"
          autoComplete="new-password"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder={account?.configured ? "已配置，留空保留" : "粘贴服务商提供的密钥"}
          disabled={busy}
        />
        <small className="field-help">
          {provider?.key_url && (
            <a href={provider.key_url} target="_blank" rel="noreferrer">
              获取密钥 ↗
            </a>
          )}{" "}
          {account?.configured ? "留空不修改已有密钥。" : "保存在运行 PaperLens 的本机。"}
        </small>
      </div>
      <details open={custom || undefined}>
        <summary>更多设置</summary>
        <div className="field">
          <label className="field-label" htmlFor="connection-url">
            服务地址
          </label>
          <input
            id="connection-url"
            type="url"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
            required
            disabled={busy}
          />
          <small className="field-help">更换地址时，请同时更换密钥。</small>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="connection-protocol">
            接口协议
          </label>
          <select
            id="connection-protocol"
            value={protocol}
            onChange={(event) =>
              setProtocol(event.target.value as "openai" | "anthropic")
            }
            disabled={busy}
          >
            <option value="openai">OpenAI 兼容接口</option>
            <option value="anthropic">Anthropic Messages</option>
          </select>
        </div>
      </details>
      <div className="connection-form-actions">
        <button type="button" className="button" disabled={busy} onClick={onCancel}>
          取消
        </button>
        <button
          type="submit"
          className="button primary"
          disabled={
            busy ||
            !provider ||
            !baseUrl ||
            (!account?.configured && provider.requires_key && !key.trim())
          }
        >
          {busy && <span className="spinner" />}保存连接
        </button>
      </div>
    </form>
  );
}

function AssignmentPicker({
  role,
  assignment,
  accounts,
  main,
  onChange,
  onRefresh,
  fetching,
  disabled,
}: {
  role: ModelRole;
  assignment: ModelAssignment | null;
  accounts: ModelAccount[];
  main: ModelAssignment | null;
  onChange: (value: ModelAssignment | null) => void;
  onRefresh: (id: string) => Promise<RemoteModels | undefined>;
  fetching: Set<string>;
  disabled: boolean;
}) {
  const [search, setSearch] = useState("");
  const account = accounts.find((a) => a.id === assignment?.account_id);
  const inherited = role !== "main" && assignment === null;
  const effective = assignment || main;
  const effectiveAccount = accounts.find((a) => a.id === effective?.account_id);
  const vision =
    effectiveAccount?.models.items.find((m) => m.id === effective?.model)?.vision ||
    "unknown";
  const models = account?.models.items || [];
  const filtered = models.filter((m) =>
    `${m.id} ${m.name || ""}`.toLowerCase().includes(search.toLowerCase()),
  );
  const name = roleNames[role];
  const fetchingThis = Boolean(account && fetching.has(account.id));
  return (
    <div className="assignment-picker">
      <div className="field">
        <label className="field-label" htmlFor={`model-account-${role}`}>
          {name}连接
        </label>
        <select
          id={`model-account-${role}`}
          value={assignment?.account_id || ""}
          disabled={disabled}
          onChange={(event) => {
            const next = accounts.find((a) => a.id === event.target.value);
            setSearch("");
            onChange(
              next
                ? { account_id: next.id, model: next.models.items[0]?.id || "" }
                : null,
            );
          }}
        >
          <option value="">{role === "main" ? "选择连接" : "同主模型"}</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        {inherited && (
          <small className="field-help">
            {main?.model || "请先选择主模型"}
            {effectiveAccount ? ` · ${effectiveAccount.name}` : ""}
          </small>
        )}
      </div>
      {account && assignment && (
        <>
          <div className="field">
            <label className="field-label" htmlFor={`model-search-${role}`}>
              搜索模型
            </label>
            <input
              id={`model-search-${role}`}
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="输入模型名称筛选"
              disabled={disabled || !models.length}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`model-choice-${role}`}>
              {name}模型
            </label>
            <div className="connection-model-select">
              <select
                id={`model-choice-${role}`}
                value={assignment.model}
                disabled={disabled || (!models.length && !assignment.model)}
                onChange={(event) =>
                  onChange({ account_id: account.id, model: event.target.value })
                }
              >
                <option value="">
                  {fetchingThis
                    ? "正在获取模型…"
                    : models.length
                      ? "选择模型"
                      : "尚未获取到模型"}
                </option>
                {assignment.model && !filtered.some((m) => m.id === assignment.model) && (
                  <option value={assignment.model}>{assignment.model}（当前选择）</option>
                )}
                {filtered.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name && m.name !== m.id ? `${m.name} · ${m.id}` : m.id}
                  </option>
                ))}
              </select>
              <button
                className="button small"
                type="button"
                disabled={disabled || fetchingThis}
                aria-label={`刷新${name}模型`}
                onClick={() => void onRefresh(account.id)}
              >
                {fetchingThis ? (
                  <span className="spinner" />
                ) : (
                  <Icon name="refresh" size={14} />
                )}
                刷新
              </button>
            </div>
            <small className="field-help">
              {fetchingThis
                ? "正在向该服务商获取可用模型…"
                : account.models.status === "live"
                  ? `来自服务商 · ${models.length} 个模型`
                  : account.models.status === "cached"
                    ? `上次获取的列表 · ${models.length} 个模型`
                    : models.length
                      ? `保留上次模型列表 · ${models.length} 个`
                      : "保存连接后自动获取模型。"}
              {account.models.fetched_at &&
                ` · ${new Date(account.models.fetched_at).toLocaleString()}`}
            </small>
            {account.models.error && (
              <small className="field-error">
                {account.models.error} 可重试刷新，或在高级选项中填写模型 ID。
              </small>
            )}
          </div>
          {search && !filtered.length && (
            <p className="tiny-note">没有匹配“{search}”的模型。</p>
          )}
          <details className="connection-manual">
            <summary>手填模型</summary>
            <div className="field">
              <label className="field-label" htmlFor={`model-manual-${role}`}>
                模型 ID
              </label>
              <input
                id={`model-manual-${role}`}
                value={assignment.model}
                onChange={(event) =>
                  onChange({ account_id: account.id, model: event.target.value.trim() })
                }
                disabled={disabled}
                placeholder="服务商不提供模型列表时使用"
              />
            </div>
          </details>
        </>
      )}
      {role === "vision" && effective?.model && vision !== "supported" && (
        <div className="vision-confirmation">
          {vision === "unsupported" ? (
            <p className="tiny-note">请更换为能看图的模型。</p>
          ) : (
            <label>
              <input
                type="checkbox"
                checked={Boolean(assignment?.vision_confirmed)}
                disabled={disabled}
                onChange={(event) =>
                  onChange({ ...effective, vision_confirmed: event.target.checked })
                }
              />
              确认能看图
              <small>请先向服务商确认看图能力。</small>
            </label>
          )}
        </div>
      )}
    </div>
  );
}
