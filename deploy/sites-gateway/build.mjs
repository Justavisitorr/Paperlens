import { copyFile, mkdir, rm } from "node:fs/promises";

await mkdir("dist/server", { recursive: true });
await copyFile("worker/index.js", "dist/server/index.js");
try {
  await mkdir("dist/.openai", { recursive: true });
  await copyFile(".openai/hosting.json", "dist/.openai/hosting.json");
} catch (error) {
  if (error.code !== "ENOENT") throw error;
  await rm("dist/.openai/hosting.json", { force: true });
}
