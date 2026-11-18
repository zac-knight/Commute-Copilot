import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The browser talks to the Vite origin; /api/* is proxied to the backend, so there is no CORS to
// configure in development. API_URL points the proxy at another backend (the e2e test uses :8001).
const target = process.env.API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
