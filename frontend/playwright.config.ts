import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  webServer: [
    {
      command: "node e2e/staff-auth-server.mjs",
      port: 3100,
      reuseExistingServer: false,
    },
    {
      command:
        "BACKEND_API_URL=http://127.0.0.1:3100 ./node_modules/.bin/next dev --hostname 127.0.0.1 --port 3000",
      port: 3000,
      reuseExistingServer: false,
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:3000",
  },
});
