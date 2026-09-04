import { expect, test } from "@playwright/test";

test("public chat publishes durable validated output through the real local backend", async ({
  page,
  request,
}) => {
  await page.goto("/chat/task26-public-key");
  const [started] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/api/v1/public/chat/sessions") &&
        response.status() === 201,
    ),
    page.getByRole("button", { name: "Start chat" }).click(),
  ]);
  const start = (await started.json()) as {
    credential: { token: string };
    session: { id: string };
  };
  await expect(page.getByText("Chat started. An AI assistant is ready to help.")).toBeVisible();

  await page.getByRole("textbox", { name: "Message" }).fill("What is the refund policy?");
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/v1/public/chat/sessions/") &&
        response.url().endsWith("/messages") &&
        response.status() === 202,
    ),
    page.getByRole("button", { name: "Send" }).click(),
  ]);
  await expect(page.getByText("What is the refund policy?")).toBeVisible();
  const session = await request.get("http://127.0.0.1:3100/__e2e__/latest-public-session");
  expect(session.ok()).toBeTruthy();
  const { session_id } = (await session.json()) as { session_id: string };
  expect(session_id).toBe(start.session.id);
  const processed = await request.post(
    `http://127.0.0.1:3100/__e2e__/consume-public-answer/${session_id}`,
  );
  expect(processed.ok()).toBeTruthy();
  // Keep the production stream open rather than adding a second test-only
  // connection: a missed Redis hint must be recovered from PostgreSQL by the
  // live browser stream itself.
  await expect(page.getByText("Regenerated grounded reply.")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/chunk_id|internal_drive_link|drive\.google/i)).toHaveCount(0);

  await page.getByRole("button", { name: "Request a person" }).click();
  await expect(page.getByText("Your request is queued for a support person.")).toBeVisible();
  await expect(page.getByText(/not promising a live response/i)).toBeVisible();
});
