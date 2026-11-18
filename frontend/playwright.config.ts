import { defineConfig, devices } from "@playwright/test";

// The e2e suite runs against DEMO mode: recorded answers, frozen data, a fake calendar. No model,
// no network and none of your real data are involved, so it is deterministic and safe anywhere.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:5184", trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    {
      command: "DEMO=1 DEMO_SPEED=0.05 uv run uvicorn app.main:app --port 8011 --log-level warning",
      cwd: "../backend",
      url: "http://localhost:8011/health",
      reuseExistingServer: false,
      timeout: 90_000,
    },
    {
      command: "API_URL=http://localhost:8011 npx vite --port 5184 --strictPort",
      url: "http://localhost:5184",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
