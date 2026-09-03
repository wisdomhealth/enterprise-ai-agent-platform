import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ConnectorStatus } from "../components/admin/ConnectorStatus";
import { JobFailures } from "../components/admin/JobFailures";
import { KnowledgeStatus } from "../components/admin/KnowledgeStatus";
import { QualitySummary } from "../components/admin/QualitySummary";
import { RetentionPolicyControl } from "../components/admin/RetentionPolicyControl";
import { UserManagement } from "../components/admin/UserManagement";

afterEach(cleanup);

it("edits versioned retention defaults without making a compliance claim", async () => {
  const update = vi.fn().mockResolvedValue(undefined);
  render(
    <RetentionPolicyControl
      policy={{
        id: "policy-1",
        chat_days: 90,
        email_days: 90,
        audit_days: 365,
        version: 4,
        legal_compliance_guarantee: false,
      }}
      onUpdate={update}
    />,
  );

  expect(screen.getByText(/defaults are configurable/i)).toBeVisible();
  expect(screen.getByText(/not a legal or compliance guarantee/i)).toBeVisible();
  fireEvent.change(screen.getByLabelText("Chat content days"), {
    target: { value: "45" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save retention policy" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith(4, 45, 90, 365));
});

function expectDialogKeyboardBoundary(triggerName: string, dialogName: string) {
  const trigger = screen.getByRole("button", { name: triggerName });
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = screen.getByRole("dialog", { name: dialogName });
  const cancel = screen.getByRole("button", { name: "Cancel" });
  expect(cancel).toHaveFocus();
  const confirm = within(dialog)
    .getAllByRole("button")
    .find((button) => button.textContent !== "Cancel");
  expect(confirm).toBeDefined();
  fireEvent.keyDown(cancel, { key: "Tab" });
  expect(confirm).toHaveFocus();
  fireEvent.keyDown(confirm!, { key: "Tab", shiftKey: true });
  expect(cancel).toHaveFocus();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: dialogName })).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
}

const deliveryUnknownJob = {
  job_id: "job-email",
  kind: "email.delivery",
  state: "RECONCILIATION" as const,
  attempts: 1,
  error_code: "GMAIL_RESPONSE_TIMEOUT",
  updated_at: "2026-09-02T07:00:00Z",
  action: "RECONCILE_GMAIL" as const,
  action_resource_id: "delivery-1",
};
const driveRetryJob = {
  job_id: "job-drive",
  kind: "knowledge.drive_source.sync",
  state: "FAILED" as const,
  attempts: 3,
  error_code: "DRIVE_RATE_LIMITED",
  updated_at: "2026-09-02T06:00:00Z",
  action: "RETRY_DRIVE_SYNC" as const,
  action_resource_id: "source-1",
};

it("separates safe retry from delivery reconciliation", async () => {
  const retry = vi.fn().mockResolvedValue(undefined);
  const reconcile = vi.fn();
  render(
    <JobFailures
      jobs={[deliveryUnknownJob, driveRetryJob]}
      onRetry={retry}
      onReconcile={reconcile}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "Retry Drive sync" }));
  expect(screen.getByRole("dialog", { name: "Retry Drive sync confirmation" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Confirm retry" }));
  await waitFor(() => expect(retry).toHaveBeenCalledWith("job-drive"));
  fireEvent.click(screen.getByRole("button", { name: "Reconcile Gmail" }));
  expect(reconcile).toHaveBeenCalledWith("delivery-1");
  expect(screen.queryByRole("button", { name: "Retry Gmail send" })).not.toBeInTheDocument();
  expect(screen.getByText(/DRIVE_RATE_LIMITED/)).toBeVisible();
  expect(screen.getAllByText(/UTC/)).toHaveLength(2);
});

it("confirms connector reauthorization and displays exact minimum scopes", async () => {
  const reauthorize = vi.fn().mockResolvedValue(undefined);
  render(
    <ConnectorStatus
      connectors={[
        {
          id: "connector-1",
          kind: "GMAIL",
          status: "REAUTH_REQUIRED",
          updated_at: "2026-09-02T07:00:00Z",
          requested_scopes: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
          ],
        },
      ]}
      onReauthorize={reauthorize}
    />,
  );

  expect(screen.getByText(/gmail\.readonly/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Reauthorize Gmail" }));
  expect(
    screen.getByRole("dialog", { name: "Reauthorize Gmail confirmation" }),
  ).toBeVisible();
  expect(reauthorize).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Continue to Google" }));
  await waitFor(() => expect(reauthorize).toHaveBeenCalledWith("connector-1"));
});

it("keeps connector confirmation keyboard focus inside and restores its trigger", () => {
  render(
    <ConnectorStatus
      connectors={[
        {
          id: "connector-keyboard",
          kind: "DRIVE",
          status: "REAUTH_REQUIRED",
          updated_at: "2026-09-02T07:00:00Z",
          requested_scopes: ["https://www.googleapis.com/auth/drive.readonly"],
        },
      ]}
      onReauthorize={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  expectDialogKeyboardBoundary(
    "Reauthorize Google Drive",
    "Reauthorize Google Drive confirmation",
  );
});

it("shows authorized Drive roots and confirms manual synchronization", async () => {
  const sync = vi.fn().mockResolvedValue(undefined);
  const configure = vi.fn().mockResolvedValue(undefined);
  render(
    <KnowledgeStatus
      sources={[
        {
          source_id: "source-1",
          status: "ACTIVE",
          root_folder_id: "approved-root",
          include_descendants: true,
          descendant_count: 2,
          cursor: "cursor-7",
          last_success_at: "2026-09-02T05:00:00Z",
          backlog: 1,
          isolated_files: 3,
          retry_count: 4,
          recent_error_codes: ["DRIVE_RATE_LIMITED"],
        },
      ]}
      onSync={sync}
      onConfigure={configure}
    />,
  );

  expect(screen.getByText(/approved-root/)).toBeVisible();
  expect(screen.getByText(/3 isolated/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Sync Drive now" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm sync" }));
  await waitFor(() => expect(sync).toHaveBeenCalledWith("source-1"));
  fireEvent.change(screen.getByLabelText("Drive root folder ID"), {
    target: { value: "new-approved-root" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save Drive scope" }));
  expect(screen.getByRole("dialog", { name: "Drive scope change confirmation" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Confirm scope change" }));
  await waitFor(() =>
    expect(configure).toHaveBeenCalledWith("new-approved-root", true),
  );
});

it("keeps both Drive confirmations keyboard-contained and restores each trigger", () => {
  render(
    <KnowledgeStatus
      sources={[
        {
          source_id: "source-keyboard",
          status: "ACTIVE",
          root_folder_id: "approved-root",
          include_descendants: true,
          descendant_count: 0,
          cursor: null,
          last_success_at: null,
          backlog: 0,
          isolated_files: 0,
          retry_count: 0,
          recent_error_codes: [],
        },
      ]}
      onSync={vi.fn().mockResolvedValue(undefined)}
      onConfigure={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  expectDialogKeyboardBoundary("Sync Drive now", "Manual Drive sync confirmation");
  expectDialogKeyboardBoundary("Save Drive scope", "Drive scope change confirmation");
});

it("keeps job retry confirmation keyboard-contained and restores its trigger", () => {
  render(
    <JobFailures
      jobs={[driveRetryJob]}
      onRetry={vi.fn().mockResolvedValue(undefined)}
      onReconcile={vi.fn()}
    />,
  );
  expectDialogKeyboardBoundary("Retry Drive sync", "Retry Drive sync confirmation");
});

it("shows quality, latency, and cost without model content", () => {
  render(
    <QualitySummary
      rag={{
        completed_at: "2026-09-02T04:00:00Z",
        status: "COMPLETED",
        quality_score: 0.98,
        latency_ms: 123,
        estimated_cost: 0.12,
      }}
      email={{
        completed_at: "2026-09-02T04:30:00Z",
        status: "COMPLETED",
        quality_score: 0.96,
        latency_ms: 80,
        estimated_cost: 0.03,
      }}
    />,
  );

  expect(screen.getByText("RAG quality 98.0%")).toBeVisible();
  expect(screen.getByText("Email quality 96.0%")).toBeVisible();
  expect(screen.getAllByText(/UTC/)).toHaveLength(2);
});

it("versions user changes and confirms disable", async () => {
  const invite = vi.fn().mockResolvedValue(undefined);
  const update = vi.fn().mockResolvedValue(undefined);
  render(
    <UserManagement
      users={[
        {
          id: "user-1",
          email: "member@example.test",
          role: "MEMBER",
          status: "ACTIVE",
          version: 4,
        },
      ]}
      onInvite={invite}
      onUpdate={update}
    />,
  );

  fireEvent.change(screen.getByLabelText("Invitation email"), {
    target: { value: "new@example.test" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Invite user" }));
  await waitFor(() => expect(invite).toHaveBeenCalledWith("new@example.test", "MEMBER"));

  fireEvent.click(screen.getByRole("button", { name: "Disable member@example.test" }));
  expect(screen.getByRole("dialog", { name: "Disable user confirmation" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Confirm disable" }));
  await waitFor(() =>
    expect(update).toHaveBeenCalledWith("user-1", 4, { status: "DISABLED" }),
  );
});

it("keeps disable confirmation keyboard-contained and restores its trigger", () => {
  render(
    <UserManagement
      users={[
        {
          id: "user-keyboard",
          email: "keyboard@example.test",
          role: "MEMBER",
          status: "ACTIVE",
          version: 1,
        },
      ]}
      onInvite={vi.fn().mockResolvedValue(undefined)}
      onUpdate={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  expectDialogKeyboardBoundary(
    "Disable keyboard@example.test",
    "Disable user confirmation",
  );
});
