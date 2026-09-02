import { expect, test } from "@playwright/test";

type Fixture = { staff_session_id: string; csrf_token: string; email_id: string };

test("real review lifecycle preserves history and fences unknown delivery", async ({
  browser,
  request,
}) => {
  const fixtureResponse = await request.get("http://127.0.0.1:3100/__e2e__/fixture");
  expect(fixtureResponse.ok()).toBeTruthy();
  const fixture = (await fixtureResponse.json()) as Fixture;
  const context = await browser.newContext();
  await context.addCookies([
    {
      name: "staff_session",
      value: fixture.staff_session_id,
      domain: "127.0.0.1",
      path: "/",
    },
    { name: "staff_csrf", value: fixture.csrf_token, domain: "127.0.0.1", path: "/" },
  ]);
  const page = await context.newPage();

  await page.goto("/staff/email");
  await page.getByRole("link", { name: "Review Browser review request" }).click();
  await expect(page.getByText("Initial grounded reply.").first()).toBeVisible();

  await page.getByLabel("Regeneration instruction").fill("Use a concise tone.");
  await page.getByRole("button", { name: "Regenerate draft" }).click();
  await page.getByRole("button", { name: "Confirm regeneration" }).click();
  await expect(page.getByText("Draft version 1")).toBeVisible();
  await expect(page.getByText("Draft version 2 — current")).toBeVisible();
  await expect(page.getByLabel("Reply body")).toHaveValue("Regenerated grounded reply.");

  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByLabel("Email state")).toHaveText("Send pending");
  await page.getByLabel("Reply body").fill("Reviewer-edited grounded reply.");
  await expect(page.getByText("Approval will be cleared when these changes are saved.")).toBeVisible();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Awaiting review").first()).toBeVisible();
  await expect(page.getByText("Draft version 3 — current")).toBeVisible();

  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByLabel("Email state")).toHaveText("Send pending");
  const unknown = await request.post(
    "http://127.0.0.1:3100/__e2e__/email-delivery-unknown",
    {
      headers: {
        Cookie: `staff_session=${fixture.staff_session_id}; staff_csrf=${fixture.csrf_token}`,
        "X-CSRF-Token": fixture.csrf_token,
      },
    },
  );
  expect(unknown.ok()).toBeTruthy();
  await page.reload();

  await expect(page.getByLabel("Email state")).toHaveText("Delivery unknown");
  await expect(page.getByRole("button", { name: "Check Gmail delivery" })).toBeVisible();
  await expect(page.getByText(/may already have been sent/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /send again|retry delivery/i })).toHaveCount(0);
  await expect(page.getByText("Staff Assist")).toBeVisible();

  await context.close();
});
