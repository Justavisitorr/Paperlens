import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  base: "./",
  build: {
    target: "es2022",
    outDir: "tests/pdf-source-dist",
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    rollupOptions: { input: "tests/pdf-source.html" },
  },
});
