import { expect, test } from "@playwright/test";

test("customer can request a queued human handoff without a live-response promise", async ({ page }) => {
  await page.route("**/api/v1/public/chat/sessions", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        session: {
          id: "session-1",
          state: "AI_ACTIVE",
          version: 1,
          customer_name: null,
          customer_email: null,
          created_at: "2026-08-28T00:00:00Z",
          messages: [],
        },
        credential: { token: "opaque-token", expires_at: "2026-08-29T00:00:00Z" },
      }),
    });
  });
  await page.route("**/api/v1/public/chat/sessions/session-1/handoff", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ state: "QUEUED" }) });
  });
  await page.route("**/api/v1/public/chat/sessions/session-1/events?after=0", async (route) => {
    await route.fulfill({ contentType: "text/event-stream", body: "" });
  });

  await page.goto("/chat/public-acme");
  await page.getByRole("button", { name: "Start chat" }).click();
  await page.getByRole("button", { name: "Request a person" }).click();

  await expect(page.getByText("Your request is queued for a support person.")).toBeVisible();
  await expect(page.getByText("We are not promising a live response. Follow up may arrive by email.")).toBeVisible();
});
