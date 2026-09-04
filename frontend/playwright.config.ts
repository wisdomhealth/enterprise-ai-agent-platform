import { defineConfig } from "@playwright/test";

const databaseUrl =
  process.env.TASK16_E2E_DATABASE_URL ??
  "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/platform_task15_fix";

export default defineConfig({
  testDir: "./e2e",
  // The browser harness intentionally mutates one durable fixture lifecycle.
  workers: 1,
  webServer: [
    {
      command:
        "../backend/.venv/bin/python -m uvicorn tests.task26_provider_app:app --host 127.0.0.1 --port 3201",
      port: 3201,
      reuseExistingServer: false,
      env: {
        APP_ENV: "test",
        PYTHONPATH: "../backend",
      },
    },
    {
      command:
        "../backend/.venv/bin/python -m uvicorn tests.task16_e2e_app:app --host 127.0.0.1 --port 3100",
      port: 3100,
      reuseExistingServer: false,
      env: {
        TASK16_E2E: "1",
        APP_ENV: "test",
        DATABASE_URL: databaseUrl,
        PYTHONPATH: "../backend",
        TASK26_LOCAL_PROVIDER: "1",
        ANTHROPIC_API_KEY: "task26-local",
        ANTHROPIC_BASE_URL: "http://127.0.0.1:3201",
        OPENAI_API_KEY: "task26-local",
        OPENAI_BASE_URL: "http://127.0.0.1:3201/v1",
        GOOGLE_OIDC_CLIENT_ID: "",
        GOOGLE_OIDC_CLIENT_SECRET: "",
        GOOGLE_DRIVE_CLIENT_ID: "",
        GOOGLE_DRIVE_CLIENT_SECRET: "",
        GOOGLE_GMAIL_CLIENT_ID: "",
        GOOGLE_GMAIL_CLIENT_SECRET: "",
        GOOGLE_CLOUD_PROJECT: "",
        GOOGLE_KMS_KEY_NAME: "",
        CONNECTOR_FILE_KEY_PATH: "",
        REDIS_URL: "redis://127.0.0.1:56385/0",
      },
    },
    {
      command: "./node_modules/.bin/next dev --hostname 127.0.0.1 --port 3000",
      port: 3000,
      reuseExistingServer: false,
      env: {
        BACKEND_API_URL: "http://127.0.0.1:3100",
        // Exercise the real local backend SSE route directly.  Production
        // Nginx likewise routes /api without buffering; Next dev rewrites do
        // not provide that streaming guarantee.
        NEXT_PUBLIC_API_BASE_URL: "http://127.0.0.1:3100",
      },
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:3000",
  },
});
