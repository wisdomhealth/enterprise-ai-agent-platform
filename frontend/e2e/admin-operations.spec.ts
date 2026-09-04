import { expect, test, type APIRequestContext, type Browser } from "@playwright/test";

import { installAdminOperationsJourney } from "./fixtures";

type Fixture = { staff_session_id: string; csrf_token: string };

async function createStaffContext(browser: Browser, request: APIRequestContext) {
  const response = await request.get("http://127.0.0.1:3100/__e2e__/fixture");
  expect(response.ok()).toBeTruthy();
  const fixture = (await response.json()) as Fixture;
  const context = await browser.newContext();
  await context.addCookies([
    { name: "staff_session", value: fixture.staff_session_id, domain: "127.0.0.1", path: "/" },
    { name: "staff_csrf", value: fixture.csrf_token, domain: "127.0.0.1", path: "/" },
  ]);
  return context;
}

test("authorized administrator sees safe operational projections", async ({ browser, request }) => {
  const context = await createStaffContext(browser, request);
  const page = await context.newPage();
  await installAdminOperationsJourney(page);
  await page.goto("/staff/admin");

  await expect(page.getByRole("heading", { name: "Administrator operations" })).toBeVisible();
  await expect(page.getByText("Authorized root: authorized-root")).toBeVisible();
  await expect(page.getByText("Cursor: cursor-7")).toBeVisible();
  await expect(page.getByText(/token|secret|ciphertext|credential/i)).toHaveCount(0);
  await context.close();
});

test("unauthorized administrator projection fails without disclosing resources", async ({ browser, request }) => {
  const context = await createStaffContext(browser, request);
  const page = await context.newPage();
  await installAdminOperationsJourney(page, false);
  await page.goto("/staff/admin");

  await expect(page.getByRole("status")).toHaveText(
    "Administrator operations are unavailable or not authorized.",
  );
  await expect(page.getByText("authorized-root")).toHaveCount(0);
  await expect(page.getByText("cursor-7")).toHaveCount(0);
  await context.close();
});
