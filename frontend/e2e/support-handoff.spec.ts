import { expect, test } from "@playwright/test";

test("claim, reply, and explicit Resume AI suppress a pending stale answer", async ({ browser }) => {
  const context = await browser.newContext();
  await context.addCookies([
    { name: "staff_session", value: "staff-session", domain: "127.0.0.1", path: "/" },
    { name: "staff_csrf", value: "csrf-token", domain: "127.0.0.1", path: "/" },
  ]);
  const customer = await context.newPage();
  const staff = await context.newPage();
  let state = "AI_ACTIVE";
  let version = 1;
  let assignedUserId: string | null = null;
  let resumeRequests = 0;
  let releaseSse!: () => void;
  const resumed = new Promise<void>((resolve) => {
    releaseSse = resolve;
  });
  const messages = [
    {
      sequence: 1,
      actor: "CUSTOMER",
      body: "I need a person",
      status: "PERSISTED",
      created_at: "2026-08-29T00:00:00Z",
      citations: [],
    },
  ];
  const handoff = () => ({
    id: "handoff-1",
    session_id: "session-1",
    state,
    trigger: "CUSTOMER_REQUEST",
    assigned_user_id: assignedUserId,
    version,
    last_customer_sequence: 1,
  });

  await context.route("**/api/v1/public/chat/sessions", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        session: {
          id: "session-1",
          state: "AI_ACTIVE",
          version: 1,
          customer_name: "Ada",
          customer_email: "ada@example.test",
          created_at: "2026-08-29T00:00:00Z",
          messages: [],
        },
        credential: { token: "opaque-token", expires_at: "2026-08-30T00:00:00Z" },
      }),
    });
  });
  await context.route("**/api/v1/public/chat/sessions/session-1/handoff", async (route) => {
    state = "QUEUED";
    version += 1;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(handoff()) });
  });
  await context.route("**/api/v1/public/chat/sessions/session-1/events?after=0", async (route) => {
    await resumed;
    await route.fulfill({
      contentType: "text/event-stream",
      body: `id: 0:t:${version}\nevent: session.state\ndata: {"sequence":0,"state":"AI_ACTIVE","version":${version}}\n\n`,
    });
  });
  await context.route("**/api/v1/staff/support/queue", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify([handoff()]) });
  });
  await context.route("**/api/v1/staff/support/handoff-1", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ...handoff(),
        customer: { name: "Ada", email: "ada@example.test" },
        summary: "",
        tool_results: [],
        messages,
      }),
    });
  });
  await context.route("**/api/v1/staff/support/handoff-1/claim", async (route) => {
    state = "HUMAN_ACTIVE";
    assignedUserId = "staff-1";
    version += 1;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(handoff()) });
  });
  await context.route("**/api/v1/staff/support/handoff-1/reply", async (route) => {
    const body = (await route.request().postDataJSON()) as { body: string };
    version += 1;
    const message = {
      sequence: 2,
      actor: "STAFF",
      body: body.body,
      status: "PERSISTED",
      created_at: "2026-08-29T00:01:00Z",
      citations: [],
    };
    messages.push(message);
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(message) });
  });
  await context.route("**/api/v1/staff/support/handoff-1/resume-ai", async (route) => {
    resumeRequests += 1;
    state = "AI_ACTIVE";
    version += 1;
    releaseSse();
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(handoff()) });
  });

  await customer.goto("/chat/public-acme");
  await customer.getByLabel("Name (optional)").fill("Ada");
  await customer.getByLabel("Email").fill("ada@example.test");
  await customer.getByRole("button", { name: "Start chat" }).click();
  await customer.getByRole("button", { name: "Request a person" }).click();
  await expect(customer.getByText("Your request is queued for a support person.")).toBeVisible();

  await staff.goto("/staff/support");
  await staff.getByRole("button", { name: "Claim" }).click();
  await expect(staff.getByText("Human active")).toBeVisible();
  await staff.getByLabel("Reply to customer").fill("A human reply");
  await staff.getByRole("button", { name: "Send reply" }).click();
  await expect(staff.getByText("Reply sent.")).toBeVisible();
  await staff.getByRole("button", { name: "Resume AI" }).click();
  await expect(staff.getByRole("dialog", { name: "Resume AI confirmation" })).toBeVisible();
  expect(resumeRequests).toBe(0);
  await staff.getByRole("button", { name: "Confirm resume AI" }).click();
  await expect(staff.getByText("AI will wait for the next customer message.")).toBeVisible();
  expect(resumeRequests).toBe(1);

  await expect(customer.getByText("Chat started. An AI assistant is ready to help.")).toBeVisible();
  await expect(customer.getByText("stale AI answer", { exact: false })).toHaveCount(0);
  await context.close();
});
