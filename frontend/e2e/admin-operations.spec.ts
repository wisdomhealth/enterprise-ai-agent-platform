import { expect, test, type APIRequestContext, type Browser } from "@playwright/test";

type Fixture = { admin_session_id: string; csrf_token: string; staff_session_id: string };

async function createStaffContext(
  browser: Browser,
  request: APIRequestContext,
  session: "admin" | "reviewer",
) {
  const response = await request.get("http://127.0.0.1:3100/__e2e__/fixture");
  expect(response.ok()).toBeTruthy();
  const fixture = (await response.json()) as Fixture;
  const context = await browser.newContext();
  await context.addCookies([
    {
      name: "staff_session",
      value: session === "admin" ? fixture.admin_session_id : fixture.staff_session_id,
      domain: "127.0.0.1",
      path: "/",
    },
    { name: "staff_csrf", value: fixture.csrf_token, domain: "127.0.0.1", path: "/" },
  ]);
  return context;
}

test("authorized administrator sees safe operational projections", async ({ browser, request }) => {
  const context = await createStaffContext(browser, request, "admin");
  const page = await context.newPage();
  await page.goto("/staff/admin");

  await expect(page.getByRole("heading", { name: "Administrator operations" })).toBeVisible();
  await expect(page.getByText("Authorized root: task26-root")).toBeVisible();
  await expect(page.getByText("Cursor: Not initialized")).toBeVisible();
  await expect(page.getByText(/token|secret|ciphertext|credential/i)).toHaveCount(0);
  await context.close();
});

test("unauthorized administrator projection fails without disclosing resources", async ({ browser, request }) => {
  const context = await createStaffContext(browser, request, "reviewer");
  const page = await context.newPage();
  await page.goto("/staff/admin");

  await expect(page.getByRole("status")).toHaveText(
    "Administrator operations are unavailable or not authorized.",
  );
  await expect(page.getByText("task26-root")).toHaveCount(0);
  await context.close();
});
