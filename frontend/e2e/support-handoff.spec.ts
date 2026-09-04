import { expect, test } from "@playwright/test";

type Fixture = {
  staff_session_id: string;
  csrf_token: string;
  handoff_id: string;
  session_id: string;
};

test("live PostgreSQL claim, reply, and explicit Resume AI fence stale output", async ({
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
  const staff = await context.newPage();

  await staff.goto("/staff/support");
  const handoff = staff.getByRole("listitem").filter({
    hasText: `Conversation ${fixture.session_id}`,
  });
  await expect(handoff.getByText("Queued")).toBeVisible();
  const initialVersion = Number((await handoff.getByText(/Version /).textContent())?.split(" ")[2]);
  await handoff.getByRole("button", { name: "Claim" }).click();
  await expect(staff.getByText("Human active")).toBeVisible();

  const staleClaimStatus = await staff.evaluate(
    async ({ handoffId, version }) =>
      (
        await fetch(`/api/v1/staff/support/${handoffId}/claim`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": "task16-browser-csrf" },
          body: JSON.stringify({ version }),
        })
      ).status,
    { handoffId: fixture.handoff_id, version: initialVersion },
  );
  expect(staleClaimStatus).toBe(409);

  await staff.getByLabel("Reply to customer").fill("A human reply");
  await staff.getByRole("button", { name: "Send reply" }).click();
  await expect(staff.getByText("Reply sent.")).toBeVisible();
  await expect(staff.getByText("A human reply")).toBeVisible();

  await staff.getByRole("button", { name: "Resume AI" }).click();
  await expect(staff.getByRole("dialog", { name: "Resume AI confirmation" })).toBeVisible();
  const headers = {
    Cookie: `staff_session=${fixture.staff_session_id}; staff_csrf=${fixture.csrf_token}`,
    "X-CSRF-Token": fixture.csrf_token,
  };
  const beforeResume = await request.get("http://127.0.0.1:3100/__e2e__/state", { headers });
  expect((await beforeResume.json()).job_state).toBe("PENDING");

  await staff.getByRole("button", { name: "Confirm resume AI" }).click();
  await expect(staff.getByText("AI will wait for the next customer message.")).toBeVisible();

  const staleAttempt = await request.post(
    "http://127.0.0.1:3100/__e2e__/attempt-stale-answer",
    { headers },
  );
  expect(await staleAttempt.json()).toEqual({ published: false, model_calls: 0 });
  const afterResume = await request.get("http://127.0.0.1:3100/__e2e__/state", { headers });
  expect(await afterResume.json()).toMatchObject({
    handoff_state: "AI_ACTIVE",
    job_state: "FAILED",
    job_error: "HANDOFF_RESUME_STALE",
    output_count: 0,
    answer_event_count: 0,
  });

  await context.close();
});
