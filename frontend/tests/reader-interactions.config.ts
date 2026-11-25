import { defineConfig } from "vite";
import base from "./pdf-source.config.ts";
export default defineConfig({
  ...base,
  build: {
    ...base.build,
    outDir: "tests/reader-interactions-dist",
    rollupOptions: { input: "tests/reader-interactions.html" },
  },
});
