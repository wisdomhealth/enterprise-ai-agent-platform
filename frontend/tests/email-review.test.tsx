import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { EmailQueue } from "../components/email/EmailQueue";
import { EmailReviewPanel } from "../components/email/EmailReviewPanel";
import {
  approveEmail,
  getEmailDetail,
  listEmailQueue,
  regenerateEmailDraft,
} from "../lib/staff-api";
import { emailDetail } from "./fixtures/email";

vi.mock("../lib/staff-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/staff-api")>();
  return {
    ...actual,
    approveEmail: vi.fn(),
    editEmailDraft: vi.fn(),
    getEmailDetail: vi.fn(),
    listEmailQueue: vi.fn(),
    regenerateEmailDraft: vi.fn(),
  };
});

beforeEach(() => vi.resetAllMocks());

it("renders the state-filtered queue and its durable classification fields", async () => {
  vi.mocked(listEmailQueue).mockResolvedValue([
    {
      id: emailDetail.id,
      state: emailDetail.state,
      version: emailDetail.version,
      sender: emailDetail.sender,
      subject: emailDetail.subject,
      received_at: emailDetail.received_at,
      category: emailDetail.category,
      priority: emailDetail.priority,
    },
  ]);

  render(<EmailQueue />);

  expect(await screen.findByText("Need account help")).toBeVisible();
  expect(screen.getByText("High priority")).toBeVisible();
  expect(screen.getAllByText("Awaiting review")).toHaveLength(2);
  expect(screen.getByRole("link", { name: "Review Need account help" })).toHaveAttribute(
    "href",
    "/staff/email/email-1",
  );
});

it("shows original mail, rationale, current source details, history, and audit transitions", () => {
  render(<EmailReviewPanel item={emailDetail} />);

  expect(screen.getByText(emailDetail.body)).toBeVisible();
  expect(screen.getByText(emailDetail.classification_rationale)).toBeVisible();
  expect(screen.getAllByText("The response time is one business day.")).toHaveLength(2);
  expect(screen.getAllByText("Model claude-test · Prompt email-v2")).toHaveLength(2);
  expect(screen.getByText("Internal source: Support policy")).toBeVisible();
  expect(screen.getByText("Draft version 1")).toBeVisible();
  expect(screen.getByText("DRAFTING → AWAITING_REVIEW")).toBeVisible();
});

it("regenerates only after an explicit instruction and confirmation", async () => {
  vi.mocked(regenerateEmailDraft).mockResolvedValue({
    id: emailDetail.id,
    state: "AWAITING_REVIEW",
    version: 5,
    current_draft_id: "draft-3",
  });
  render(<EmailReviewPanel item={emailDetail} />);

  expect(screen.getByRole("button", { name: "Regenerate draft" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Regeneration instruction"), {
    target: { value: "Use a friendlier tone." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Regenerate draft" }));
  expect(screen.getByRole("dialog", { name: "Regenerate draft confirmation" })).toBeVisible();
  expect(regenerateEmailDraft).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm regeneration" }));

  await waitFor(() =>
    expect(regenerateEmailDraft).toHaveBeenCalledWith(
      emailDetail.id,
      emailDetail.version,
      emailDetail.current_draft_id,
      "Use a friendlier tone.",
    ),
  );
});

it("replaces the editor with the newly persisted draft after regeneration", async () => {
  const regenerated = {
    ...emailDetail,
    version: 5,
    current_draft_id: "draft-3",
    drafts: [
      ...emailDetail.drafts,
      {
        ...emailDetail.drafts[1],
        id: "draft-3",
        version: 3,
        body: "A newly persisted response.",
        reviewer_instruction: "Use a friendlier tone.",
      },
    ],
  };
  vi.mocked(regenerateEmailDraft).mockResolvedValue({
    id: emailDetail.id,
    state: "AWAITING_REVIEW",
    version: 5,
    current_draft_id: "draft-3",
  });
  vi.mocked(getEmailDetail).mockResolvedValue(regenerated);
  render(<EmailReviewPanel item={emailDetail} />);

  fireEvent.change(screen.getByLabelText("Regeneration instruction"), {
    target: { value: "Use a friendlier tone." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Regenerate draft" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm regeneration" }));

  expect(await screen.findByDisplayValue("A newly persisted response.")).toBeVisible();
  expect(screen.getByText("Draft version 3 — current")).toBeVisible();
});

it("approves only the current immutable draft version", async () => {
  vi.mocked(approveEmail).mockResolvedValue({
    id: emailDetail.id,
    state: "SEND_PENDING",
    version: 5,
    current_draft_id: emailDetail.current_draft_id!,
  });
  render(<EmailReviewPanel item={emailDetail} />);

  fireEvent.click(screen.getByRole("button", { name: "Approve" }));

  await waitFor(() =>
    expect(approveEmail).toHaveBeenCalledWith(
      emailDetail.id,
      emailDetail.version,
      emailDetail.current_draft_id,
    ),
  );
  expect(await screen.findByText("Send pending")).toBeVisible();
});
