import { afterEach, expect, it, vi } from "vitest";

import {
  approveEmail,
  listEmailQueue,
  StaffApiError,
} from "../lib/staff-api";

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "staff_csrf=; Max-Age=0; path=/";
});

it("reads the email queue through the state-filtered staff endpoint", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);

  await listEmailQueue(["AWAITING_REVIEW", "DELIVERY_UNKNOWN"]);

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/staff/email?state=AWAITING_REVIEW&state=DELIVERY_UNKNOWN",
    expect.objectContaining({ credentials: "same-origin" }),
  );
});

it("sends versioned review actions with CSRF and an idempotency key", async () => {
  document.cookie = "staff_csrf=task20-csrf; path=/";
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        id: "email-1",
        state: "SEND_PENDING",
        version: 5,
        current_draft_id: "draft-2",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ),
  );
  vi.stubGlobal("fetch", fetchMock);

  await approveEmail("email-1", 4, "draft-2");

  const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual(
    expect.objectContaining({
      "Content-Type": "application/json",
      "X-CSRF-Token": "task20-csrf",
      "Idempotency-Key": expect.stringMatching(/^email-approve:email-1:/),
    }),
  );
  expect(JSON.parse(init.body as string)).toEqual({
    expected_version: 4,
    current_draft_id: "draft-2",
  });
});

it("returns authoritative email state from a stale-version conflict", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            state: "SEND_PENDING",
            version: 8,
            current_draft_id: "draft-4",
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    ),
  );

  const error = await approveEmail("email-1", 4, "draft-2").catch((value: unknown) => value);

  expect(error).toBeInstanceOf(StaffApiError);
  expect((error as StaffApiError).message).toBe("The resource changed");
  expect((error as StaffApiError).handoff).toBeUndefined();
  expect((error as StaffApiError).email).toEqual({
    state: "SEND_PENDING",
    version: 8,
    current_draft_id: "draft-4",
  });
});
