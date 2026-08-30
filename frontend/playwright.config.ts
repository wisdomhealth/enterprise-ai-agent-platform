import { defineConfig } from "@playwright/test";

const databaseUrl =
  process.env.TASK16_E2E_DATABASE_URL ??
  "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/platform_task15_fix";

export default defineConfig({
  testDir: "./e2e",
  webServer: [
    {
      command:
        "../backend/.venv/bin/python -m uvicorn tests.task16_e2e_app:app --host 127.0.0.1 --port 3100",
      port: 3100,
      reuseExistingServer: false,
      env: { DATABASE_URL: databaseUrl, PYTHONPATH: "../backend" },
    },
    {
      command: "./node_modules/.bin/next dev --hostname 127.0.0.1 --port 3000",
      port: 3000,
      reuseExistingServer: false,
      env: { BACKEND_API_URL: "http://127.0.0.1:3100" },
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:3000",
  },
});
