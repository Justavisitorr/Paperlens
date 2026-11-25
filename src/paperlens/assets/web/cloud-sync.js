/* Loaded after the existing encrypted, account-scoped browser storage adapter. */
(() => {
  "use strict";
  const local = window.localStorage;
  const read = local.getItem.bind(local), write = local.setItem.bind(local), remove = local.removeItem.bind(local);
  const META = "paperlens.cloud-sync.v1";
  let account = null, timer = null, busy = false;
  let status = "正在连接账户资料…";
  const fetchOriginal = window.fetch.bind(window);
  const state = () => {
    try { return JSON.parse(read(META)) || {versions:{}, pending:{}, conflicts:{}}; }
    catch { return {versions:{}, pending:{}, conflicts:{}}; }
  };
  const save = data => write(META, JSON.stringify(data));
  const keys = ["paperlens.prefs", "paperlens.terms", "paperlens:reading-type", "paperlens:reading-language", "paperlens:term-replacements", "paperlens:citation-copy-format", "paperlens:reference-source-preference"];
  const prefixes = ["paperlens:reading-position:", "paperlens:annotation-draft:", "paperlens:reading-note-followup:", "paperlens.draft:"];
  const eligible = key => keys.includes(key) || prefixes.some(prefix => key.startsWith(prefix));
  function announce(message) {
    status = message;
    window.dispatchEvent(new CustomEvent("paperlens:sync", {detail: snapshot()}));
  }
  function snapshot() { return {status, conflicts: state().conflicts, pending: Object.keys(state().pending).length}; }
  window.fetch = (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (account && url.origin === location.origin && (url.pathname.startsWith("/auth/") || url.pathname.startsWith("/api/"))) {
      const headers = new Headers(options?.headers ?? (input instanceof Request ? input.headers : undefined));
      headers.set("X-PaperLens-CSRF", account.csrf);
      headers.set("X-PaperLens-Workspace", account.workspace);
      return fetchOriginal(input, {...options, headers});
    }
    return fetchOriginal(input, options);
  };
  async function json(url, options) {
    const result = await window.fetch(url, {...options, signal: AbortSignal.timeout(15000)});
    const data = await result.json();
    if (!result.ok) throw Object.assign(new Error(data.detail || "资料同步暂不可用"), {status: result.status});
    return data;
  }
  function changed(key, value) {
    if (!eligible(key)) return;
    const data = state();
    data.pending[key] = {key, value, version: data.versions[key] || 0, id: crypto.randomUUID()};
    save(data);
    announce("有修改待同步");
    clearTimeout(timer);
    timer = setTimeout(flush, 500);
  }
  function mutate(key, value) {
    key = String(key);
    if (!eligible(key)) { if (value === null) remove(key); else write(key, value); return; }
    // Persist the outbox before changing the cache. A full browser disk must not
    // leave a new value with no durable record that it still needs to be sent.
    const previousMeta = read(META);
    changed(key, value);
    try { if (value === null) remove(key); else write(key, value); }
    catch (error) {
      if (previousMeta === null) remove(META); else write(META, previousMeta);
      announce("此设备存储空间不足，修改未保存");
      throw error;
    }
  }
  local.setItem = (key, value) => mutate(key, String(value));
  local.removeItem = key => mutate(key, null);
  local.clear = () => {
    const keys = Array.from({length:local.length}, (_, i) => local.key(i)).filter(key => key !== META);
    for (const key of keys) local.removeItem(key);
  };
  function apply(key, value) {
    const oldValue = read(key);
    if (value === null) remove(key); else write(key, value);
    if (oldValue !== value) window.dispatchEvent(new StorageEvent("storage", {key, oldValue, newValue:value, url:location.href}));
  }
  async function flush(pull = true) {
    if (!account || busy) return;
    busy = true;
    try {
      let current = state();
      const sent = [];
      let bytes = 0;
      for (const item of Object.values(current.pending).filter(item => !current.conflicts[item.key])) {
        const size = new TextEncoder().encode(JSON.stringify(item)).length;
        if (sent.length && (sent.length === 100 || bytes + size > 1.5 * 1024 * 1024)) break;
        sent.push(item); bytes += size;
      }
      if (sent.length) {
        const result = await json("/auth/data", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({changes:sent})});
        current = state();
        for (const item of sent) {
          const accepted = result.accepted[item.key], conflict = result.conflicts[item.key];
          if (accepted) {
            current.versions[item.key] = accepted.version;
            if (current.incoming) delete current.incoming[item.key];
            if (current.pending[item.key]?.id === item.id) delete current.pending[item.key];
            else if (current.pending[item.key]) current.pending[item.key].version = accepted.version;
          } else if (conflict) current.conflicts[item.key] = conflict;
        }
        save(current);
      }
      const remote = pull ? (await json("/auth/data")).items : {};
      current = state();
      current.incoming ||= {};
      for (const [key, item] of Object.entries(remote)) {
        if (current.pending[key]) continue;
        // React editors may still hold the previous value in state. Do not advance
        // their base revision behind their backs; reload applies the remote value.
        if (current.versions[key] !== item.version) current.incoming[key] = item;
      }
      save(current);
      const conflicts = Object.keys(current.conflicts).length;
      announce(conflicts ? `${conflicts} 项资料有版本冲突，请在账户页选择保留内容` : Object.keys(current.pending).length ? "有修改待同步" : Object.keys(current.incoming).length ? "其他设备更新了资料，刷新页面即可应用" : "资料已同步");
      if (!conflicts && Object.keys(current.pending).length) timer = setTimeout(flush, 500);
    } catch (error) {
      announce(error.status === 401 || error.status === 403 || error.status === 409
        ? "登录状态已变化，请刷新页面；未同步修改保留在此设备"
        : `尚未同步：${error.message}。修改仍保留在此设备`);
    } finally { busy = false; }
  }
  window.paperLensSync = {
    snapshot, flush,
    assertNotesSynced() {
      for (let i = 0; i < local.length; i++) {
        const key = local.key(i);
        if (key.startsWith("paperlens:reading-notes:") && JSON.parse(read(key) || "{}").outbox?.length)
          throw new Error("有阅读笔记尚未同步。请先打开对应论文完成同步，再导出整包。");
      }
    },
    resolve(key, useLocal) {
      const current = state(), remote = current.conflicts[key];
      if (!remote) return;
      current.versions[key] = remote.version;
      if (useLocal && current.pending[key]) current.pending[key].version = remote.version;
      else { delete current.pending[key]; apply(key, remote.value); }
      delete current.conflicts[key];
      save(current); flush();
    },
    exportData() {
      const result = {};
      for (let i = 0; i < local.length; i++) {
        const key = local.key(i);
        if (eligible(key)) result[key] = read(key);
      }
      return result;
    },
    importData(items) {
      if (!items || typeof items !== "object" || Array.isArray(items)) throw new Error("资料格式无效");
      for (const [key, value] of Object.entries(items)) {
        if (!eligible(key) || typeof value !== "string") continue;
        // Import through the ordinary outbox; existing cloud versions remain visible.
        if (read(key) !== null && read(key) !== value) {
          const current = state();
          current.conflicts[key] = {value: read(key), version: current.versions[key] || 0};
          save(current);
        }
        local.setItem(key, value);
      }
      flush();
    },
  };
  async function connect() {
    try {
      account = await json("/auth/session");
      if (account.workspace !== window.paperLensWorkspaceId) throw Object.assign(new Error("账户已切换，请刷新"), {status:409});
      window.paperLensAccount = account;
      const remote = (await json("/auth/data")).items;
      const current = state();
      // First visit after upgrading: only previously unsynced local keys become an outbox.
      for (let i = 0; i < local.length; i++) {
        const key = local.key(i);
        if (eligible(key) && !current.versions[key] && !current.pending[key] && (!(key in remote) || read(key) !== remote[key].value))
          current.pending[key] = {key, value:read(key), version:0, id:crypto.randomUUID()};
      }
      for (const [key, item] of Object.entries(remote)) {
        if (current.pending[key]) continue;
        current.versions[key] = item.version;
        apply(key, item.value);
      }
      current.incoming = {};
      save(current);
      await flush(false);
    } catch (error) {
      announce(`账户资料连接失败：${error.message}`);
      if ([401, 403, 409].includes(error.status)) {
        document.body.textContent = "账户已变化，请刷新或重新登录。";
        throw error;
      }
    }
  }
  window.paperLensCloudReady = connect();
  const refresh = () => account ? flush() : connect();
  window.addEventListener("online", refresh);
  window.addEventListener("focus", refresh);
  window.addEventListener("storage", event => { if (event.key === META) announce(status); });
  window.addEventListener("beforeunload", event => {
    if (Object.keys(state().pending).length) { event.preventDefault(); event.returnValue = ""; }
  });
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
