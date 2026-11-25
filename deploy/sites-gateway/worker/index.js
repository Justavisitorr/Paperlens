// The compute agent initiates every connection. The Site can dispatch only
// PaperLens operations to its in-process application, never to a network address.
const utf8 = new TextEncoder();
const blockSize = 2 * 1024 * 1024;
const idPattern = /^\d{13}-[0-9a-f]{32}$/;
const allowedHeaders = [
  "accept",
  "content-type",
  "origin",
  "sec-fetch-site",
  "sec-fetch-mode",
  "sec-fetch-dest",
  "range",
  "if-range",
  "x-paperlens-workspace",
];
const allowedMethods = new Set([
  "GET",
  "HEAD",
  "PUT",
  "POST",
  "PATCH",
  "DELETE",
]);
const noStore = {
  "Cache-Control": "private, no-store",
  "X-Content-Type-Options": "nosniff",
  Vary: "Cookie",
};
const now = () => Date.now();
const hex = (bytes) =>
  Array.from(new Uint8Array(bytes), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const buffer64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
function base64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i += 16384)
    text += String.fromCharCode(...bytes.subarray(i, i + 16384));
  return btoa(text);
}
const json = (value, status = 200) =>
  Response.json(value, { status, headers: noStore });
const fail = (status, detail) => json({ detail }, status);
function offline(request) {
  if (!(request.headers.get("accept") || "").includes("text/html"))
    return fail(503, "PaperLens 计算后端暂时离线。");
  return new Response(
    '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PaperLens 服务状态</title><main style="max-width:38rem;margin:16vh auto;padding:1.5rem;font:16px/1.7 system-ui"><h1>PaperLens 正在等待计算后端</h1><p>请在后端电脑上启动 PaperLens 网页服务，连接恢复后刷新此页。</p><button onclick="location.reload()">重新连接</button></main></html>',
    {
      status: 503,
      headers: {
        ...noStore,
        "Content-Type": "text/html;charset=utf-8",
        "Retry-After": "5",
      },
    },
  );
}
function allowedPath(path) {
  return (
    (path === "/" ||
      path.startsWith("/api/") ||
      path === "/_paperlens/client.js" ||
      path === "/_paperlens/ready") &&
    !path.includes("\\") &&
    !path.includes("//") &&
    !/%2f|%5c|%00/i.test(path)
  );
}
async function removeTransfer(bucket, id) {
  await bucket.delete("queue/" + id);
  let cursor;
  do {
    const page = await bucket.list({
      prefix: "rpc/" + id + "/",
      limit: 400,
      cursor,
    });
    if (page.objects.length)
      await bucket.delete(page.objects.map((obj) => obj.key));
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
}
async function cleanupOld(bucket) {
  // Keys sort by creation time. Also reap transfers abandoned by closed browsers.
  for (const prefix of ["queue/", "rpc/"]) {
    const page = await bucket.list({ prefix, limit: 180 });
    const expired = page.objects.filter(
      (obj) =>
        Number(obj.key.slice(prefix.length, prefix.length + 13)) <
        now() - 3600000,
    );
    if (expired.length) await bucket.delete(expired.map((obj) => obj.key));
  }
}
async function* blocks(stream) {
  if (!stream) return;
  const reader = stream.getReader();
  let buffer = new Uint8Array(blockSize),
    used = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (let offset = 0; offset < value.length;) {
        const size = Math.min(buffer.length - used, value.length - offset);
        buffer.set(value.subarray(offset, offset + size), used);
        used += size;
        offset += size;
        if (used === buffer.length) {
          yield buffer;
          buffer = new Uint8Array(blockSize);
          used = 0;
        }
      }
    }
    if (used) yield buffer.subarray(0, used);
  } finally {
    reader.releaseLock();
  }
}
async function encryptedEnvelope(meta, rawKey, publicKey) {
  const aes = await crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, [
    "encrypt",
  ]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const cipher = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    aes,
    utf8.encode(JSON.stringify(meta)),
  );
  const rsa = await crypto.subtle.importKey(
    "spki",
    buffer64(publicKey),
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"],
  );
  const wrapped = await crypto.subtle.encrypt("RSA-OAEP", rsa, rawKey);
  return {
    id: meta.id,
    iv: base64(iv),
    wrapped: base64(new Uint8Array(wrapped)),
    cipher: base64(new Uint8Array(cipher)),
  };
}
async function waitPart(bucket, path, key, signal, deadline) {
  let delay = 170;
  while (now() < deadline && !signal.aborted) {
    const object = await bucket.get(path, { ssecKey: key });
    if (object) return object;
    await pause(delay);
    delay = Math.min(3500, Math.round(delay * 1.55));
  }
  return null;
}
async function dispatch(request, env, ctx, principal) {
  const bucket = env.ORIGIN_REGISTRY,
    url = new URL(request.url);
  const id = String(now()) + "-" + crypto.randomUUID().replaceAll("-", "");
  const root = "rpc/" + id + "/";
  const rawKey = crypto.getRandomValues(new Uint8Array(32)),
    key = hex(rawKey);
  const limit = ["/api/jobs", "/api/estimates"].includes(url.pathname)
    ? 64 * 1024 ** 2 + 65536
    : 8 * 1024 ** 2;
  if (Number(request.headers.get("content-length") || 0) > limit)
    return fail(413, "请求超过服务器大小上限。");
  let size = 0,
    parts = 0,
    inline = "";
  const cleanup = () => ctx.waitUntil(removeTransfer(bucket, id));
  try {
    for await (const part of blocks(request.body)) {
      size += part.length;
      if (size > limit) {
        cleanup();
        return fail(413, "请求超过服务器大小上限。");
      }
      if (!parts && part.length <= 65536) inline = base64(part);
      else {
        await bucket.put(root + "in/" + parts, part, { ssecKey: key });
        parts++;
      }
    }
    const headers = {};
    for (const name of allowedHeaders)
      if (request.headers.has(name)) headers[name] = request.headers.get(name);
    const meta = {
      id,
      principal,
      method: request.method,
      path: url.pathname,
      query: url.search.slice(1),
      headers,
      parts,
      inline,
      size,
    };
    const envelope = await encryptedEnvelope(
      meta,
      rawKey,
      env.PAPERLENS_AGENT_PUBLIC_KEY,
    );
    await bucket.put("queue/" + id, JSON.stringify(envelope));
    let index = 0;
    let part = await waitPart(
      bucket,
      root + "out/0",
      key,
      request.signal,
      now() + 150000,
    );
    if (!part) {
      cleanup();
      return fail(504, "后端未及时返回。已提交的分析请在任务列表中查看。");
    }
    const attrs = part.customMetadata || {},
      status = Number(attrs.status) || 200;
    const returned = new Headers(noStore);
    for (const [name, value] of Object.entries(
      JSON.parse(attrs.headers || "{}"),
    ))
      returned.set(name, value);
    returned.set("Cache-Control", "private, no-store");
    if (request.method === "HEAD" || status === 204 || status === 304) {
      cleanup();
      return new Response(null, { status, headers: returned });
    }
    const stream = new ReadableStream({
      async pull(controller) {
        try {
          if (!part)
            part = await waitPart(
              bucket,
              root + "out/" + index,
              key,
              request.signal,
              now() + 180000,
            );
          if (!part) throw new Error("PaperLens response interrupted");
          const last = part.customMetadata?.end === "1";
          const data = new Uint8Array(await part.arrayBuffer());
          if (data.length) controller.enqueue(data);
          const consumed = root + "out/" + index;
          index++;
          part = null;
          if (last) {
            controller.close();
            cleanup();
          } else ctx.waitUntil(bucket.delete(consumed));
        } catch (error) {
          controller.error(error);
          cleanup();
        }
      },
      cancel: cleanup,
    });
    return new Response(stream, { status, headers: returned });
  } catch (error) {
    cleanup();
    throw error;
  }
}
async function agentAuthorized(request, env) {
  const token = request.headers.get("x-paperlens-agent-token") || "";
  if (token.length !== 64) return false;
  const got = hex(await crypto.subtle.digest("SHA-256", utf8.encode(token)));
  return got === env.PAPERLENS_AGENT_TOKEN_SHA256;
}
async function agentRequest(request, env, ctx, url) {
  if (!(await agentAuthorized(request, env)))
    return fail(403, "计算客户端认证失败。");
  const bucket = env.ORIGIN_REGISTRY,
    path = url.pathname.slice("/_paperlens/host/".length);
  if (path === "template" && request.method === "HEAD") {
    const object = await bucket.head("shared/original.html");
    return new Response(null, {
      status: object ? 200 : 404,
      headers: {
        ...noStore,
        "x-template-sha": object?.customMetadata?.sha || "",
      },
    });
  }
  if (path === "template" && request.method === "PUT") {
    if (Number(request.headers.get("content-length") || 0) > 24000000)
      return fail(413, "模板过大。");
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > 24000000) return fail(413, "模板过大。");
    const sha = hex(await crypto.subtle.digest("SHA-256", bytes));
    if (sha !== request.headers.get("x-template-sha"))
      return fail(422, "前端文件校验失败。");
    await bucket.put("shared/original.html", bytes, {
      customMetadata: { sha },
    });
    return json({ saved: true });
  }
  if (path === "poll" && request.method === "GET") {
    if (request.headers.get("x-paperlens-pulse") === "1") {
      await bucket.put("heartbeat", String(now()));
      ctx.waitUntil(cleanupOld(bucket));
    }
    const slots = Math.max(
      0,
      Math.min(12, Number(url.searchParams.get("slots")) || 0),
    );
    const jobs = [];
    if (slots) {
      const page = await bucket.list({ prefix: "queue/", limit: slots });
      for (const obj of page.objects) {
        const id = obj.key.slice(6);
        if (!idPattern.test(id) || now() - Number(id.slice(0, 13)) > 60000) {
          ctx.waitUntil(removeTransfer(bucket, id));
          continue;
        }
        const item = await bucket.get(obj.key);
        if (!item) continue;
        const claim = await bucket.put("rpc/" + id + "/claim", "1", {
          onlyIf: new Headers({ "If-None-Match": "*" }),
        });
        if (claim) jobs.push(await item.json());
        await bucket.delete(obj.key);
      }
    }
    return json({ jobs });
  }
  const match = /^(in|out)\/(\d{13}-[a-f0-9]{32})\/(\d{1,5})$/.exec(path);
  if (!match) return fail(404, "无此操作。");
  const [, direction, id, part] = match,
    key = request.headers.get("x-paperlens-object-key") || "";
  if (
    !/^[0-9a-f]{64}$/.test(key) ||
    !(await bucket.head("rpc/" + id + "/claim"))
  )
    return fail(410, "该次请求已结束。");
  const name = "rpc/" + id + "/" + direction + "/" + part;
  if (direction === "in" && request.method === "GET") {
    const obj = await bucket.get(name, { ssecKey: key });
    return obj
      ? new Response(obj.body, {
          headers: { ...noStore, "Content-Type": "application/octet-stream" },
        })
      : fail(404, "输入分块不存在。");
  }
  if (direction === "out" && request.method === "PUT") {
    const size = Number(request.headers.get("content-length") || 0);
    if (size > blockSize + 1024) return fail(413, "响应分块过大。");
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > blockSize + 1024) return fail(413, "响应分块过大。");
    const meta = JSON.parse(
      new TextDecoder().decode(
        buffer64(request.headers.get("x-paperlens-response") || ""),
      ),
    );
    const retained = {};
    for (const name of [
      "content-type",
      "content-disposition",
      "content-range",
      "accept-ranges",
      "cross-origin-resource-policy",
    ]) {
      if (meta.headers?.[name])
        retained[name] = String(meta.headers[name]).slice(0, 500);
    }
    await bucket.put(name, bytes, {
      ssecKey: key,
      customMetadata: {
        status: String(meta.status),
        headers: JSON.stringify(retained),
        end: meta.end ? "1" : "0",
      },
    });
    return json({ stored: true });
  }
  return fail(405, "不支持此操作。");
}
async function rootPage(request, env, principal) {
  const obj = await env.ORIGIN_REGISTRY.get("shared/original.html");
  if (!obj) return offline(request);
  const headers = {
    ...noStore,
    "Content-Type": "text/html; charset=utf-8",
    "X-PaperLens-Template": obj.customMetadata?.sha || "",
  };
  const response = new Response(request.method === "HEAD" ? null : obj.body, {
    headers,
  });
  const navigation =
    request.headers.get("sec-fetch-mode") === "navigate" ||
    ["document", "iframe"].includes(request.headers.get("sec-fetch-dest")) ||
    (!request.headers.has("sec-fetch-dest") &&
      (request.headers.get("accept") || "").includes("text/html"));
  if (!navigation || request.method === "HEAD") return response;
  const workspace = hex(
    await crypto.subtle.digest(
      "SHA-256",
      utf8.encode("paperlens-sites-v1\0" + principal),
    ),
  );
  return new HTMLRewriter()
    .on("head", {
      element(el) {
        el.prepend(
          '<script src="/_paperlens/client.js?workspace=' +
            workspace +
            '"></script>',
          { html: true },
        );
      },
    })
    .on('script[type="module"]', {
      element(el) {
        el.prepend(
          "if(!window.paperLensWorkspaceReady){document.body.textContent='无法连接当前研究账户，请刷新页面。';throw new Error('Workspace unavailable');}",
          { html: true },
        );
      },
    })
    .transform(response);
}
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith("/_paperlens/host/"))
        return await agentRequest(request, env, ctx, url);
      if (!allowedMethods.has(request.method) || !allowedPath(url.pathname))
        return fail(404, "无此 PaperLens 操作。");
      const ready =
        url.pathname === "/_paperlens/ready" &&
        ["GET", "HEAD"].includes(request.method);
      const principal = ready
        ? ""
        : request.headers.get("oai-authenticated-user-id");
      if (!ready && !principal) {
        if (url.pathname === "/" && request.method === "GET") {
          return new Response(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 PaperLens</title><main style="max-width:36rem;margin:16vh auto;padding:1.5rem;font:16px/1.8 system-ui"><h1>登录 PaperLens</h1><p>进入你的独立论文研究工作区。</p><a href="/signin-with-chatgpt?return_to=%2F" target="_top">使用 ChatGPT 账户登录</a></main></html>',
            {
              headers: {
                ...noStore,
                "Content-Type": "text/html;charset=utf-8",
              },
            },
          );
        }
        return fail(401, "请先使用自己的账户登录 PaperLens。");
      }
      if (
        request.headers.get("origin") &&
        request.headers.get("origin") !== url.origin
      )
        return fail(403, "页面来源无效。");
      if (
        request.headers.get("sec-fetch-site") === "cross-site" &&
        !(
          url.pathname === "/" &&
          request.method === "GET" &&
          request.headers.get("sec-fetch-mode") === "navigate"
        )
      )
        return fail(403, "请在 PaperLens 页面内操作。");
      const heartbeat = await env.ORIGIN_REGISTRY.get("heartbeat");
      if (!heartbeat || now() - Number(await heartbeat.text()) > 85000)
        return offline(request);
      if (url.pathname === "/" && ["GET", "HEAD"].includes(request.method))
        return await rootPage(request, env, principal);
      return await dispatch(request, env, ctx, principal);
    } catch (error) {
      console.error(
        "PaperLens gateway:",
        error?.name || "error",
        String(error?.message || "").slice(0, 180),
      );
      return fail(502, "研究服务连接暂未完成，请稍后重试。");
    }
  },
};
