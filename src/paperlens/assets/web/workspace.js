/* Served only for authenticated online page navigation. The two tokens below are
   replaced server-side with this visitor's storage namespace and browser-only key. */
(() => {
  "use strict";
  const workspace = "__WORKSPACE_ID__";
  window.paperLensWorkspaceId = workspace;
  const key = fromBase64("__BROWSER_KEY__");
  const prefix = "pl-web/" + workspace + "/";
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const box = window.nacl.secretbox;

  function fromBase64(value) {
    return Uint8Array.from(atob(value), char => char.charCodeAt(0));
  }
  function toBase64(bytes) {
    let result = "";
    for (let offset = 0; offset < bytes.length; offset += 16384) {
      result += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
    }
    return btoa(result);
  }
  function seal(value) {
    const nonce = crypto.getRandomValues(new Uint8Array(box.nonceLength));
    const ciphertext = box(encoder.encode(value), nonce, key);
    return toBase64(nonce) + "." + toBase64(ciphertext);
  }
  function open(value) {
    if (value === null) return null;
    const [nonce, ciphertext] = value.split(".");
    const plain = box.open(fromBase64(ciphertext), fromBase64(nonce), key);
    if (!plain) throw new Error("工作区缓存校验失败。");
    return decoder.decode(plain);
  }
  function accountStorage(raw) {
    const keys = () => Object.keys(raw).filter(name => name.startsWith(prefix));
    return {
      get length() { return keys().length; },
      key(index) { return keys()[index]?.slice(prefix.length) ?? null; },
      getItem(name) { return open(raw.getItem(prefix + name)); },
      setItem(name, value) { raw.setItem(prefix + name, seal(String(value))); },
      removeItem(name) { raw.removeItem(prefix + name); },
      clear() { for (const name of keys()) raw.removeItem(name); }
    };
  }
  for (const name of ["localStorage", "sessionStorage"]) {
    Object.defineProperty(window, name, {value: accountStorage(window[name])});
  }
  window.addEventListener("storage", event => {
    if (!event.key?.startsWith(prefix)) return;
    window.dispatchEvent(new StorageEvent("storage", {
      key: event.key.slice(prefix.length),
      oldValue: open(event.oldValue),
      newValue: open(event.newValue),
      url: event.url
    }));
  });

  // Bind writes and reads from an already-open tab to the identity that loaded it.
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || !(url.pathname.startsWith("/api/") || url.pathname === "/")) {
      return originalFetch(input, options);
    }
    const headers = new Headers(options?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set("X-PaperLens-Workspace", workspace);
    return originalFetch(input, {...options, headers});
  };
  Object.defineProperty(window, "paperLensWorkspaceReady", {value:true});
})();
