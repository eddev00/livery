import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The renderer is loaded from file:// in a sandboxed window: relative asset paths,
// everything bundled, nothing fetched at runtime.
export default defineConfig({
  root: "src",
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../dist/renderer",
    emptyOutDir: true,
    target: "es2023",
    assetsInlineLimit: 0,
  },
});
