import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const path = (relative) => fileURLToPath(new URL(relative, import.meta.url));
const outDir = path("../dist/pages/");
await build({ root: path("../"), mode: "pages", build: { outDir, emptyOutDir: true } });
const template = await readFile(path("../dist/pages/index.html"), "utf8");
const notices = await readFile(
  path("../../src/paperlens/assets/THIRD_PARTY_NOTICES.txt"),
  "utf8",
);
const attribution = JSON.stringify(notices).replaceAll("<", "\\u003c");
const root = template.replace(
  '<div id="root"></div>',
  '<div id="root"><p style="padding:24px;font:16px system-ui" role="status">正在打开 PaperLens 阅读工作区…</p></div>',
);
const end = root.lastIndexOf("</body>");
if (end < 0 || !root.includes('id="paperlens-data"'))
  throw new Error("Missing standalone reader or report slot");
await writeFile(
  path("../../docs/index.html"),
  root.slice(0, end) +
    '<script id="paperlens-open-source-notices" type="application/json">' +
    attribution +
    "</script>" +
    root.slice(end),
  "utf8",
);
