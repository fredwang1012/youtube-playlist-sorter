import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../app/web/static",
    emptyOutDir: true,
  },
  test: {
    environment: "node",
  },
});
