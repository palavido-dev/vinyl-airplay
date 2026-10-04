import path from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@vinyl-jukebox/shared": path.join(root, "packages/shared/src/index.ts"),
    },
  },
  server: {
    fs: {
      allow: [root],
    },
    port: 5173,
  },
});
