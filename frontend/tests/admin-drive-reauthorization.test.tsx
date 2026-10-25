import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import AdminOperationsPage from "../app/staff/admin/page";
import {
  beginConnectorReauthorization,
  configureAdminDriveScope,
  getAdminConnectorAuthorizations,
  getAdminGmailSyncStates,
  getAdminOperationsSummary,
  getAdminRetentionPolicy,
  listAdminFailedJobs,
  listAdminUsers,
  StaffApiError,
} from "../lib/staff-api";

vi.mock("../lib/staff-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/staff-api")>();
  return {
    ...actual,
    beginConnectorReauthorization: vi.fn(),
    configureAdminDriveScope: vi.fn(),
    getAdminConnectorAuthorizations: vi.fn(),
    getAdminGmailSyncStates: vi.fn(),
    getAdminOperationsSummary: vi.fn(),
    getAdminRetentionPolicy: vi.fn(),
    listAdminFailedJobs: vi.fn(),
    listAdminUsers: vi.fn(),
  };
});

const source = {
  source_id: "source-1",
  status: "ACTIVE",
  root_folder_id: "approved-root",
  include_descendants: true,
  descendant_count: 1,
  cursor: "cursor-9",
  last_success_at: null,
  backlog: 0,
  isolated_files: 0,
  retry_count: 0,
  recent_error_codes: [],
};

function summary(status: "ACTIVE" | "REAUTH_REQUIRED") {
  return {
    generated_at: "2026-10-07T00:00:00Z",
    connectors: [
      {
        id: "drive-connector",
        kind: "DRIVE" as const,
        status,
        updated_at: "2026-10-07T00:00:00Z",
        requested_scopes: ["https://www.googleapis.com/auth/drive.readonly"],
      },
    ],
    knowledge_sources: [source],
    jobs: { queue_depth: 0, failed: 0 },
    support: { backlog: 0 },
    email: { retry_wait: 0, delivery_unknown: 0 },
    rag_quality: null,
    email_quality: null,
  };
}

beforeEach(() => {
  vi.mocked(getAdminConnectorAuthorizations).mockResolvedValue([]);
  vi.mocked(getAdminGmailSyncStates).mockResolvedValue([]);
  vi.mocked(listAdminFailedJobs).mockResolvedValue({
    items: [],
    page: 1,
    page_size: 20,
    total: 0,
    count: 0,
    total_pages: 0,
  });
  vi.mocked(listAdminUsers).mockResolvedValue([]);
  vi.mocked(getAdminRetentionPolicy).mockResolvedValue({
    id: "retention",
    chat_days: 90,
    email_days: 90,
    audit_days: 365,
    version: 1,
    legal_compliance_guarantee: false,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("refreshes reauthorization status while preserving the unsaved Drive folder", async () => {
  vi.mocked(getAdminOperationsSummary)
    .mockResolvedValueOnce(summary("ACTIVE"))
    .mockResolvedValue(summary("REAUTH_REQUIRED"));
  vi.mocked(configureAdminDriveScope).mockRejectedValue(
    new StaffApiError(
      "Google Drive authorization has expired or been revoked. Please reconnect Google Drive.",
      409,
      undefined,
      "GOOGLE_DRIVE_REAUTH_REQUIRED",
    ),
  );

  render(<AdminOperationsPage />);
  const input = await screen.findByLabelText("Drive root folder ID");
  fireEvent.change(input, { target: { value: "draft-root" } });
  fireEvent.click(screen.getByRole("button", { name: "Save Drive scope" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm scope change" }));

  expect(
    await screen.findByText(
      "Google Drive authorization has expired or been revoked. Please reconnect Google Drive.",
    ),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Reauthorize Google Drive" })).toBeVisible();
  expect(screen.getByLabelText("Drive root folder ID")).toHaveValue("draft-root");
  expect(screen.getByText("Authorized root: approved-root")).toBeVisible();
  expect(getAdminOperationsSummary).toHaveBeenCalledTimes(2);
});

it("shows an understandable error when reauthorization cannot start", async () => {
  vi.mocked(getAdminOperationsSummary).mockResolvedValue(summary("REAUTH_REQUIRED"));
  vi.mocked(beginConnectorReauthorization).mockRejectedValue(
    new StaffApiError("Unable to start Google Drive reauthorization.", 503),
  );

  render(<AdminOperationsPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Reauthorize Google Drive" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue to Google" }));

  await waitFor(() =>
    expect(
      screen.getByText("Unable to start Google Drive reauthorization."),
    ).toBeVisible(),
  );
});

it("renders failed work and users while the summary request is still pending", async () => {
  vi.mocked(getAdminOperationsSummary).mockReturnValue(new Promise(() => undefined));
  vi.mocked(listAdminFailedJobs).mockResolvedValue({
    items: [{
      job_id: "job-visible",
      kind: "knowledge.drive_source.sync",
      state: "FAILED",
      attempts: 2,
      last_error_code: "DRIVE_RATE_LIMITED",
      error_category: "RETRYABLE",
      created_at: "2026-10-24T08:00:00Z",
      updated_at: "2026-10-24T09:00:00Z",
      action: "RETRY_DRIVE_SYNC",
      action_resource_id: "source-1",
    }],
    page: 1,
    page_size: 20,
    total: 1007,
    count: 1,
    total_pages: 51,
  });
  vi.mocked(listAdminUsers).mockResolvedValue([{
    id: "admin-1",
    email: "visible-admin@example.test",
    role: "ADMIN",
    status: "ACTIVE",
    version: 1,
  }]);

  render(<AdminOperationsPage />);

  expect(await screen.findByText(/DRIVE_RATE_LIMITED/)).toBeVisible();
  expect(screen.getByText("visible-admin@example.test")).toBeVisible();
  expect(screen.getByText("Loading operational status…")).toBeVisible();
});

it("keeps failed work and users visible when the summary request fails", async () => {
  vi.mocked(getAdminOperationsSummary).mockRejectedValue(new Error("summary unavailable"));
  vi.mocked(listAdminFailedJobs).mockResolvedValue({
    items: [{
      job_id: "job-visible-after-summary-error",
      kind: "knowledge.drive_source.sync",
      state: "FAILED",
      attempts: 1,
      last_error_code: "DRIVE_TEMPORARY_FAILURE",
      error_category: "RETRYABLE",
      created_at: "2026-10-24T08:00:00Z",
      updated_at: "2026-10-24T09:00:00Z",
      action: "RETRY_DRIVE_SYNC",
      action_resource_id: "source-1",
    }],
    page: 1,
    page_size: 20,
    total: 1,
    count: 1,
    total_pages: 1,
  });
  vi.mocked(listAdminUsers).mockResolvedValue([{
    id: "admin-1",
    email: "independent-admin@example.test",
    role: "ADMIN",
    status: "ACTIVE",
    version: 1,
  }]);

  render(<AdminOperationsPage />);

  expect(await screen.findByText(/DRIVE_TEMPORARY_FAILURE/)).toBeVisible();
  expect(screen.getByText("independent-admin@example.test")).toBeVisible();
  expect(screen.getByText("Operational status is unavailable or not authorized.")).toBeVisible();
});

it("loads only one failed-work page and keeps filters while paging", async () => {
  vi.mocked(getAdminOperationsSummary).mockResolvedValue(summary("ACTIVE"));
  vi.mocked(listAdminFailedJobs)
    .mockResolvedValueOnce({
      items: [],
      page: 1,
      page_size: 20,
      total: 1007,
      count: 20,
      total_pages: 51,
    })
    .mockResolvedValueOnce({
      items: [],
      page: 1,
      page_size: 20,
      total: 12,
      count: 0,
      total_pages: 1,
    });

  render(<AdminOperationsPage />);
  await screen.findByText("1–20 of 1007");
  fireEvent.change(screen.getByLabelText("Task type"), {
    target: { value: "email.classify" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Apply failed-work filters" }));

  await waitFor(() =>
    expect(listAdminFailedJobs).toHaveBeenLastCalledWith({
      page: 1,
      pageSize: 20,
      kind: "email.classify",
      state: undefined,
      updatedFrom: undefined,
      updatedTo: undefined,
    }),
  );
  expect(listAdminFailedJobs).toHaveBeenCalledTimes(2);
});
