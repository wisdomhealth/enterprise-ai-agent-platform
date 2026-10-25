import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { EmailQueue } from "../components/email/EmailQueue";
import { EmailReviewPanel } from "../components/email/EmailReviewPanel";
import {
  approveEmail,
  getAdminGmailSyncStates,
  getEmailDetail,
  listEmailQueue,
  regenerateEmailDraft,
  requestAdminGmailManualSync,
} from "../lib/staff-api";
import { emailDetail } from "./fixtures/email";

vi.mock("../lib/staff-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/staff-api")>();
  return {
    ...actual,
    approveEmail: vi.fn(),
    editEmailDraft: vi.fn(),
    getAdminGmailSyncStates: vi.fn(),
    getEmailDetail: vi.fn(),
    listEmailQueue: vi.fn(),
    regenerateEmailDraft: vi.fn(),
    requestAdminGmailManualSync: vi.fn(),
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

it("shows administrator sync acceptance without claiming completion", async () => {
  vi.mocked(listEmailQueue).mockResolvedValue([]);
  vi.mocked(getAdminGmailSyncStates).mockResolvedValue([
    {
      connector_id: "gmail-1",
      mailbox_address: "staff@example.test",
      auto_sync_enabled: false,
      polling_enabled: true,
      push_enabled: false,
      watch_status: "STOPPED",
      sync_status: "IDLE",
      processed_history_id: "100",
      pending_history_id: null,
      watch_expiration_at: null,
      recovery_batch_id: null,
      recovery_window_started_at: null,
      recovery_processed_count: 0,
      recovery_message_limit: 0,
      recovery_has_more: false,
      last_error_code: null,
      last_success_at: "2026-10-20T08:00:00Z",
      active_job_id: null,
      active_job_state: null,
      active_job_trigger: null,
      version: 1,
    },
  ]);
  vi.mocked(requestAdminGmailManualSync).mockResolvedValue({
    ...(await getAdminGmailSyncStates())[0],
    sync_status: "RUNNING",
    active_job_id: "job-1",
    active_job_state: "PENDING",
    active_job_trigger: "MANUAL",
    version: 2,
  });

  render(<EmailQueue canSync />);
  const syncButton = await screen.findByRole("button", { name: "Sync emails" });
  fireEvent.click(syncButton);
  fireEvent.click(syncButton);

  await waitFor(() => expect(requestAdminGmailManualSync).toHaveBeenCalledTimes(1));
  expect(screen.getByText(/accepted.*classification and draft generation may still be pending/i)).toBeVisible();
  expect(screen.queryByText("Synchronization completed")).not.toBeInTheDocument();
  expect(screen.getByText("Polling enabled")).toBeVisible();
  expect(screen.getByText("Push disabled · Watch stopped")).toBeVisible();
});

it("does not expose mailbox synchronization to non-administrators", async () => {
  vi.mocked(listEmailQueue).mockResolvedValue([]);

  render(<EmailQueue />);

  await waitFor(() => expect(listEmailQueue).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "Sync emails" })).not.toBeInTheDocument();
  expect(getAdminGmailSyncStates).not.toHaveBeenCalled();
});

it("refreshes the queue only after the accepted sync actually succeeds", async () => {
  const idle = {
    connector_id: "gmail-1",
    mailbox_address: "staff@example.test",
    auto_sync_enabled: false,
    polling_enabled: true,
    push_enabled: false,
    watch_status: "STOPPED",
    sync_status: "IDLE",
    processed_history_id: "100",
    pending_history_id: null,
    watch_expiration_at: null,
    recovery_batch_id: null,
    recovery_window_started_at: null,
    recovery_processed_count: 0,
    recovery_message_limit: 0,
    recovery_has_more: false,
    last_error_code: null,
    last_success_at: "2026-10-20T08:00:00Z",
    active_job_id: null,
    active_job_state: null,
    active_job_trigger: null,
    version: 1,
  };
  const running = {
    ...idle,
    sync_status: "RUNNING",
    active_job_id: "job-1",
    active_job_state: "PENDING",
    active_job_trigger: "MANUAL",
    version: 2,
  };
  vi.mocked(listEmailQueue).mockResolvedValue([]);
  vi.mocked(getAdminGmailSyncStates)
    .mockResolvedValueOnce([idle])
    .mockResolvedValueOnce([{ ...idle, last_success_at: "2026-10-20T09:00:00Z", version: 3 }]);
  vi.mocked(requestAdminGmailManualSync).mockResolvedValue(running);

  render(<EmailQueue canSync />);
  fireEvent.click(await screen.findByRole("button", { name: "Sync emails" }));

  expect(await screen.findByText(/Synchronization accepted/)).toBeVisible();
  expect(listEmailQueue).toHaveBeenCalledTimes(1);
  expect(await screen.findByText(/Email synchronization completed/, {}, { timeout: 3000 })).toBeVisible();
  expect(listEmailQueue).toHaveBeenCalledTimes(2);
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

it("keeps each current draft label and input in one accessible field row", () => {
  render(<EmailReviewPanel item={emailDetail} />);

  const form = screen.getByRole("form", { name: "Current email draft" });
  expect(within(form).getByRole("heading", { name: "Current draft" })).toHaveClass(
    "staff-field-wide",
  );
  for (const label of ["To", "CC", "Reply subject", "Gmail thread"]) {
    const input = within(form).getByLabelText(label);
    const field = input.closest(".staff-draft-field");
    expect(field).not.toBeNull();
    expect(field).toContainElement(within(form).getByText(label, { selector: "label" }));
    expect(input).toHaveAttribute("id");
    expect(within(form).getByText(label, { selector: "label" })).toHaveAttribute(
      "for",
      input.id,
    );
  }
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
