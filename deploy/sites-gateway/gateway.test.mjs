import assert from "node:assert/strict";
import { test } from "node:test";
import gateway from "./worker/index.js";

const encoding = new TextEncoder();
const secret = "a".repeat(64);
const digest = (bytes) =>
  crypto.subtle
    .digest("SHA-256", bytes)
    .then((b) => Buffer.from(b).toString("hex"));

class MemoryBucket {
  items = new Map();
  async put(name, data, options = {}) {
    if (options.onlyIf?.get("If-None-Match") === "*" && this.items.has(name))
      return null;
    const body =
      typeof data === "string" ? Buffer.from(data) : Buffer.from(data);
    this.items.set(name, {
      body,
      customMetadata: options.customMetadata || {},
      key: options.ssecKey,
    });
    return { etag: "test" };
  }
  async get(name, options = {}) {
    const item = this.items.get(name);
    if (!item) return null;
    if (item.key && item.key !== options.ssecKey)
      throw new Error("Invalid object key");
    return {
      customMetadata: item.customMetadata,
      body: new Blob([item.body]).stream(),
      json: async () => JSON.parse(item.body),
      text: async () => item.body.toString(),
      arrayBuffer: async () => Uint8Array.from(item.body).buffer,
    };
  }
  async head(name) {
    return this.items.has(name)
      ? { customMetadata: this.items.get(name).customMetadata }
      : null;
  }
  async delete(names) {
    for (const name of Array.isArray(names) ? names : [names])
      this.items.delete(name);
  }
  async list({ prefix, limit = 1000 }) {
    const all = [...this.items.keys()]
      .filter((k) => k.startsWith(prefix))
      .sort();
    return {
      objects: all.slice(0, limit).map((key) => ({ key })),
      truncated: false,
    };
  }
}
async function setup() {
  const pair = await crypto.subtle.generateKey(
    {
      name: "RSA-OAEP",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["encrypt", "decrypt"],
  );
  const env = {
    ORIGIN_REGISTRY: new MemoryBucket(),
    PAPERLENS_AGENT_PUBLIC_KEY: Buffer.from(
      await crypto.subtle.exportKey("spki", pair.publicKey),
    ).toString("base64"),
    PAPERLENS_AGENT_TOKEN_SHA256: await digest(encoding.encode(secret)),
  };
  await env.ORIGIN_REGISTRY.put("heartbeat", String(Date.now()));
  const jobs = [];
  const ctx = {
    waitUntil(p) {
      jobs.push(p);
    },
  };
  const call = (path, options = {}) =>
    gateway.fetch(
      new Request("https://research.example" + path, options),
      env,
      ctx,
    );
  return { env, call, pair, jobs };
}
test("Only authenticated visitors and separately authenticated agents can access their surfaces", async () => {
  const { call } = await setup();
  assert.equal((await call("/api/library")).status, 401);
  assert.equal(
    (
      await call("/_paperlens/host/poll?slots=1", {
        headers: { "oai-authenticated-user-id": "alice" },
      })
    ).status,
    403,
  );
  assert.equal(
    (
      await call("/proxy/internal", {
        headers: { "oai-authenticated-user-id": "alice" },
      })
    ).status,
    404,
  );
  assert.equal(
    (
      await call("/api/settings", {
        method: "PUT",
        headers: {
          "oai-authenticated-user-id": "alice",
          origin: "https://evil.example",
        },
      })
    ).status,
    403,
  );
  assert.equal(
    (
      await call("/api/jobs", {
        method: "POST",
        headers: {
          "oai-authenticated-user-id": "alice",
          "content-length": String(70 * 1024 ** 2),
        },
      })
    ).status,
    413,
  );
});
test("The queued request is encrypted, authenticates its real visitor, and is delivered once", async () => {
  const { env, call, pair, jobs } = await setup();
  const pending = call("/api/settings", {
    method: "PUT",
    headers: {
      "oai-authenticated-user-id": "alice",
      "x-paperlens-principal": "bob",
      "content-type": "application/json",
    },
    body: JSON.stringify({ key: "private-api-key" }),
  });
  while (
    ![...env.ORIGIN_REGISTRY.items.keys()].some((k) => k.startsWith("queue/"))
  )
    await new Promise((r) => setTimeout(r, 5));
  const headers = { "x-paperlens-agent-token": secret };
  const envelope = (
    await (await call("/_paperlens/host/poll?slots=1", { headers })).json()
  ).jobs[0];
  assert.ok(!JSON.stringify(envelope).includes("private-api-key"));
  const raw = await crypto.subtle.decrypt(
    "RSA-OAEP",
    pair.privateKey,
    Buffer.from(envelope.wrapped, "base64"),
  );
  const aes = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, [
    "decrypt",
  ]);
  const decoded = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: Buffer.from(envelope.iv, "base64") },
    aes,
    Buffer.from(envelope.cipher, "base64"),
  );
  const meta = JSON.parse(new TextDecoder().decode(decoded));
  assert.equal(meta.principal, "alice");
  assert.equal(meta.headers["x-paperlens-principal"], undefined);
  assert.match(
    Buffer.from(meta.inline, "base64").toString(),
    /private-api-key/,
  );
  assert.equal(
    (await (await call("/_paperlens/host/poll?slots=1", { headers })).json())
      .jobs.length,
    0,
  );
  const responseMeta = Buffer.from(
    JSON.stringify({
      status: 200,
      headers: { "content-type": "application/json" },
      end: true,
    }),
  ).toString("base64");
  const stored = await call("/_paperlens/host/out/" + envelope.id + "/0", {
    method: "PUT",
    headers: {
      ...headers,
      "x-paperlens-object-key": Buffer.from(raw).toString("hex"),
      "x-paperlens-response": responseMeta,
    },
    body: '{"saved":true}',
  });
  assert.equal(stored.status, 200);
  const response = await pending;
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await response.json(), { saved: true });
  await Promise.all(jobs);
  assert.equal(
    [...env.ORIGIN_REGISTRY.items.keys()].filter((k) => k.startsWith("rpc/"))
      .length,
    0,
  );
});
