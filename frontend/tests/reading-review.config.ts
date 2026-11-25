import { defineConfig } from "vite";
import base from "./pdf-source.config.ts";
export default defineConfig({
  ...base,
  build: {
    ...base.build,
    outDir: "tests/reading-review-dist",
    rollupOptions: { input: "tests/reading-review.html" },
  },
});
