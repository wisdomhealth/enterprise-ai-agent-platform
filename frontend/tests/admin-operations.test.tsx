import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ConnectorStatus } from "../components/admin/ConnectorStatus";
import { GmailPushManagement } from "../components/admin/GmailPushManagement";
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
  last_error_code: "GMAIL_RESPONSE_TIMEOUT",
  error_category: "AMBIGUOUS",
  created_at: "2026-09-02T06:50:00Z",
  updated_at: "2026-09-02T07:00:00Z",
  action: "RECONCILE_GMAIL" as const,
  action_resource_id: "delivery-1",
};
const driveRetryJob = {
  job_id: "job-drive",
  kind: "knowledge.drive_source.sync",
  state: "FAILED" as const,
  attempts: 3,
  last_error_code: "DRIVE_RATE_LIMITED",
  error_category: "RETRYABLE",
  created_at: "2026-09-02T05:50:00Z",
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

it("offers Gmail reauthorization when synchronization detected revoked credentials", () => {
  render(
    <ConnectorStatus
      connectors={[{
        id: "connector-active-gmail",
        kind: "GMAIL",
        status: "ACTIVE",
        updated_at: "2026-10-25T01:00:00Z",
        requested_scopes: [
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send",
        ],
      }]}
      reauthorizationRequiredConnectorIds={new Set(["connector-active-gmail"])}
      onReauthorize={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  expect(screen.getByRole("button", { name: "Reauthorize Gmail" })).toBeVisible();
});

it("shows initial Drive and Gmail authorization when no connectors exist", () => {
  render(<ConnectorStatus connectors={[]} onReauthorize={vi.fn().mockResolvedValue(undefined)} />);

  expect(screen.getByText("Google Drive").closest("li")).toHaveTextContent(
    "Google Drive · Not connected",
  );
  expect(screen.getByRole("button", { name: "Connect Google Drive" })).toBeVisible();
  expect(screen.getByText("Gmail").closest("li")).toHaveTextContent("Gmail · Not connected");
  expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeVisible();
});

it("routes missing initial connector permission to authorization management", () => {
  const assign = vi.fn();
  vi.stubGlobal("location", { assign });
  render(
    <ConnectorStatus
      connectors={[]}
      authorizations={[
        {
          staff_user_id: "admin-1",
          kind: "DRIVE",
          resource_id: "drive-resource",
          authorize_endpoint: "/api/v1/admin/connectors/DRIVE/authorize",
          actions: [{ action: "connector.create", granted: false }],
        },
        {
          staff_user_id: "admin-1",
          kind: "GMAIL",
          resource_id: "gmail-resource",
          authorize_endpoint: "/api/v1/admin/connectors/GMAIL/authorize",
          actions: [{ action: "connector.create", granted: true }],
        },
      ]}
      onReauthorize={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  expect(screen.getByText("Google Drive").closest("li")).toHaveTextContent(
    "Google Drive · Authorization required",
  );
  expect(screen.getByText("Missing permission: connector.create")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Connect Google Drive" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Manage authorization" }));
  expect(assign).toHaveBeenCalledWith("/staff/admin/authorization");
  expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeVisible();
  vi.unstubAllGlobals();
});

it("uses existing Drive status without a duplicate initial authorization button", () => {
  render(
    <ConnectorStatus
      connectors={[
        {
          id: "drive-1",
          kind: "DRIVE",
          status: "ACTIVE",
          updated_at: "2026-09-02T07:00:00Z",
          requested_scopes: ["https://www.googleapis.com/auth/drive.readonly"],
        },
      ]}
      onReauthorize={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  expect(screen.queryByRole("button", { name: "Connect Google Drive" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reauthorize Google Drive" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeVisible();
});

it("navigates initial authorization buttons to same-origin connector routes", () => {
  const assign = vi.fn();
  vi.stubGlobal("location", { assign });
  render(<ConnectorStatus connectors={[]} onReauthorize={vi.fn().mockResolvedValue(undefined)} />);

  fireEvent.click(screen.getByRole("button", { name: "Connect Google Drive" }));
  fireEvent.click(screen.getByRole("button", { name: "Connect Gmail" }));

  expect(assign).toHaveBeenNthCalledWith(1, "/api/v1/admin/connectors/DRIVE/authorize");
  expect(assign).toHaveBeenNthCalledWith(2, "/api/v1/admin/connectors/GMAIL/authorize");
  vi.unstubAllGlobals();
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

it("explains how to recover a source disabled by root-folder loss", () => {
  render(
    <KnowledgeStatus
      sources={[
        {
          source_id: "source-disabled-root",
          status: "DISABLED",
          root_folder_id: "unavailable-root",
          include_descendants: true,
          descendant_count: 0,
          cursor: "cursor-8",
          last_success_at: null,
          backlog: 0,
          isolated_files: 1,
          retry_count: 1,
          recent_error_codes: [],
          disabled_reason: "ROOT_UNAVAILABLE_NOT_FOUND_OR_NO_ACCESS",
        },
      ]}
      onSync={vi.fn().mockResolvedValue(undefined)}
      onConfigure={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  expect(
    screen.getByText(/restore access to the authorized root or save a new Drive scope/i),
  ).toBeVisible();
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

it("requires an explicit warning confirmation before accepting limited Gmail recovery", async () => {
  const accept = vi.fn().mockResolvedValue(undefined);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  render(
    <GmailPushManagement
      states={[{
        connector_id: "gmail-1",
        mailbox_address: "support@example.test",
        auto_sync_enabled: true,
        polling_enabled: true,
        push_enabled: false,
        watch_status: "ACTIVE",
        sync_status: "RECOVERY_TRUNCATED",
        processed_history_id: null,
        pending_history_id: "300",
        watch_expiration_at: "2026-10-20T00:00:00Z",
        recovery_batch_id: "batch-1",
        recovery_window_started_at: "2026-10-12T00:00:00Z",
        recovery_processed_count: 500,
        recovery_message_limit: 500,
        recovery_has_more: true,
        last_error_code: null,
        last_success_at: "2026-10-20T08:00:00Z",
        active_job_id: null,
        active_job_state: null,
        active_job_trigger: null,
        version: 7,
      }]}
      onWatch={vi.fn().mockResolvedValue(undefined)}
      onManualSync={vi.fn().mockResolvedValue(undefined)}
      onContinueRecovery={vi.fn().mockResolvedValue(undefined)}
      onAcceptLimitedRecovery={accept}
      onResumeRecovery={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  fireEvent.change(screen.getByLabelText("Administrative reason"), {
    target: { value: "Reviewed the documented gap." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Accept limited recovery" }));

  expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/500 messages/));
  await waitFor(() => expect(accept).toHaveBeenCalledWith(
    expect.objectContaining({ connector_id: "gmail-1", version: 7 }),
    "Reviewed the documented gap.",
  ));
  confirm.mockRestore();
});

it("explains Gmail authorization failure and prevents duplicate resume clicks", async () => {
  let finishResume: (() => void) | undefined;
  const resume = vi.fn().mockImplementation(
    () => new Promise<void>((resolve) => { finishResume = resolve; }),
  );
  render(
    <GmailPushManagement
      states={[{
        connector_id: "gmail-paused",
        mailbox_address: "support@example.test",
        auto_sync_enabled: false,
        polling_enabled: true,
        push_enabled: false,
        watch_status: "STOPPED",
        sync_status: "PAUSED_ERROR",
        processed_history_id: "100",
        pending_history_id: "105",
        watch_expiration_at: null,
        recovery_batch_id: null,
        recovery_window_started_at: null,
        recovery_processed_count: 0,
        recovery_message_limit: 0,
        recovery_has_more: false,
        last_error_code: "GMAIL_REAUTH_REQUIRED",
        last_success_at: null,
        active_job_id: null,
        active_job_state: null,
        active_job_trigger: null,
        version: 3,
      }]}
      onWatch={vi.fn().mockResolvedValue(undefined)}
      onManualSync={vi.fn().mockResolvedValue(undefined)}
      onContinueRecovery={vi.fn().mockResolvedValue(undefined)}
      onAcceptLimitedRecovery={vi.fn().mockResolvedValue(undefined)}
      onResumeRecovery={resume}
    />,
  );

  expect(screen.getByText(/authorization was revoked or expired/i)).toBeVisible();
  fireEvent.change(screen.getByLabelText("Administrative reason"), {
    target: { value: "OAuth completed successfully." },
  });
  const button = screen.getByRole("button", { name: "Resume synchronization" });
  fireEvent.click(button);
  fireEvent.click(button);

  expect(resume).toHaveBeenCalledTimes(1);
  expect(button).toBeDisabled();
  finishResume?.();
  await waitFor(() => expect(button).not.toBeDisabled());
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
