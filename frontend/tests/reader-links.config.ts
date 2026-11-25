import { defineConfig } from "vite";
import pdfSourceConfig from "./pdf-source.config.ts";
export default defineConfig({
  ...pdfSourceConfig,
  build: {
    ...pdfSourceConfig.build,
    outDir: "tests/reader-links-dist",
    rollupOptions: { input: "tests/reader-links.html" },
  },
});
