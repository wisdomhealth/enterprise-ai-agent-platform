import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { EmailReviewPanel } from "../components/email/EmailReviewPanel";
import { editEmailDraft, getEmailDetail, StaffApiError } from "../lib/staff-api";
import { emailDetail } from "./fixtures/email";

vi.mock("../lib/staff-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/staff-api")>();
  return { ...actual, editEmailDraft: vi.fn(), getEmailDetail: vi.fn() };
});

beforeEach(() => vi.clearAllMocks());

it("visibly clears approval before a critical draft edit is saved", async () => {
  vi.mocked(editEmailDraft).mockResolvedValue({
    id: emailDetail.id,
    state: "AWAITING_REVIEW",
    version: 6,
    current_draft_id: "draft-3",
  });
  vi.mocked(getEmailDetail).mockResolvedValue({
    ...emailDetail,
    state: "AWAITING_REVIEW",
    version: 6,
    current_draft_id: "draft-3",
    drafts: [
      ...emailDetail.drafts,
      { ...emailDetail.drafts[1], id: "draft-3", version: 3, body: "Updated reply" },
    ],
  });
  render(<EmailReviewPanel item={{ ...emailDetail, state: "APPROVED", version: 5 }} />);

  expect(screen.getByText("Approved")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Reply body"), {
    target: { value: "Updated reply" },
  });

  expect(screen.getByText("Approval will be cleared when these changes are saved.")).toBeVisible();
  expect(screen.queryByText("Approved")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

  expect((await screen.findAllByText("Awaiting review")).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
});

it("replaces stale local state from a 409 and disables stale actions", async () => {
  const latest = {
    ...emailDetail,
    state: "DELIVERY_UNKNOWN" as const,
    version: 8,
    current_draft_id: "draft-4",
    drafts: [
      ...emailDetail.drafts,
      {
        ...emailDetail.drafts[1],
        id: "draft-4",
        version: 4,
        body: "A concurrent review replaced the stale draft.",
        approval: { approved_at: "2026-09-01T08:08:00Z", invalidated_at: null },
      },
    ],
    delivery: {
      id: "intent-concurrent",
      state: "DELIVERY_UNKNOWN" as const,
      version: 4,
      deterministic_message_id: "<concurrent@mail.invalid>",
      last_error_code: "GMAIL_RESPONSE_UNKNOWN",
      attempts: [
        {
          id: "attempt-concurrent",
          attempt_number: 2,
          outcome: "UNKNOWN" as const,
          error_code: "GMAIL_RESPONSE_UNKNOWN",
          started_at: "2026-09-01T08:09:00Z",
          completed_at: "2026-09-01T08:10:00Z",
        },
      ],
    },
  };
  vi.mocked(editEmailDraft).mockRejectedValue(new StaffApiError("Email changed", 409));
  vi.mocked(getEmailDetail).mockResolvedValue(latest);
  render(<EmailReviewPanel item={emailDetail} />);

  fireEvent.change(screen.getByLabelText("Reply body"), { target: { value: "Stale edit" } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

  expect(await screen.findByText("This email changed elsewhere. The latest state is shown.")).toBeVisible();
  expect(getEmailDetail).toHaveBeenCalledWith(emailDetail.id);
  expect(
    await screen.findByDisplayValue("A concurrent review replaced the stale draft."),
  ).toBeVisible();
  expect(screen.getByText("Draft version 4 — current")).toBeVisible();
  expect(screen.getByText(/Attempt 2: Unknown/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Check Gmail delivery" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
});
