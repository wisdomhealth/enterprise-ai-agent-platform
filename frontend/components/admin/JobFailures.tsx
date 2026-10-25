"use client";

import { useState } from "react";

import { AdminFailedJob, AdminFailedJobQuery, AdminFailedJobsPage } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { formatUtc, statusLabel } from "./format";

export function JobFailures({
  jobs,
  onRetry,
  onReconcile,
  pagination,
  loading = false,
  error = null,
  onApplyFilters,
  onPageChange,
}: {
  jobs: AdminFailedJob[];
  onRetry: (jobId: string) => Promise<void>;
  onReconcile: (resourceId: string) => void;
  pagination?: Pick<AdminFailedJobsPage, "page" | "page_size" | "total" | "count" | "total_pages">;
  loading?: boolean;
  error?: string | null;
  onApplyFilters?: (filters: Omit<AdminFailedJobQuery, "page" | "pageSize">) => void;
  onPageChange?: (page: number) => void;
}) {
  const [pending, setPending] = useState<AdminFailedJob | null>(null);
  const [kind, setKind] = useState("");
  const [state, setState] = useState<"" | AdminFailedJob["state"]>("");
  const [updatedFrom, setUpdatedFrom] = useState("");
  const [updatedTo, setUpdatedTo] = useState("");
  const rangeStart = pagination && pagination.total > 0
    ? (pagination.page - 1) * pagination.page_size + 1
    : 0;
  const rangeEnd = pagination
    ? pagination.count > 0 ? rangeStart + pagination.count - 1 : 0
    : 0;
  return (
    <section aria-labelledby="job-failures-heading">
      <h2 id="job-failures-heading">Failed and uncertain work</h2>
      {onApplyFilters ? (
        <div className="staff-filter-grid">
          <label htmlFor="failed-job-kind">Task type</label>
          <input id="failed-job-kind" value={kind} onChange={(event) => setKind(event.target.value)} />
          <label htmlFor="failed-job-state">State</label>
          <select id="failed-job-state" value={state} onChange={(event) => setState(event.target.value as "" | AdminFailedJob["state"])}>
            <option value="">All failed and uncertain states</option>
            <option value="FAILED">Failed</option>
            <option value="RECONCILIATION">Uncertain</option>
            <option value="PENDING">Pending retry</option>
          </select>
          <label htmlFor="failed-job-from">Updated from</label>
          <input id="failed-job-from" type="datetime-local" value={updatedFrom} onChange={(event) => setUpdatedFrom(event.target.value)} />
          <label htmlFor="failed-job-to">Updated to</label>
          <input id="failed-job-to" type="datetime-local" value={updatedTo} onChange={(event) => setUpdatedTo(event.target.value)} />
          <button
            type="button"
            disabled={loading}
            onClick={() => onApplyFilters({
              kind: kind.trim() || undefined,
              state: state || undefined,
              updatedFrom: updatedFrom ? new Date(updatedFrom).toISOString() : undefined,
              updatedTo: updatedTo ? new Date(updatedTo).toISOString() : undefined,
            })}
          >
            Apply failed-work filters
          </button>
        </div>
      ) : null}
      {loading ? <p role="status">Loading failed and uncertain work…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      <ul className="staff-list">
        {jobs.map((job) => (
          <li key={job.job_id}>
            <strong>{statusLabel(job.kind)}</strong>
            {` · ${statusLabel(job.state)} · ${job.attempts} attempts · ${formatUtc(job.updated_at)}`}
            {job.last_error_code ? <p>Error: {job.last_error_code}</p> : null}
            {job.error_category ? <p>Category: {statusLabel(job.error_category)}</p> : null}
            {job.action === "RECONCILE_GMAIL" && job.action_resource_id ? (
              <button type="button" onClick={() => onReconcile(job.action_resource_id!)}>
                Reconcile Gmail
              </button>
            ) : null}
            {job.action === "RETRY_DRIVE_SYNC" ? (
              <button type="button" onClick={() => setPending(job)}>Retry Drive sync</button>
            ) : null}
            {job.action === "RETRY_EMAIL_DELIVERY" ? (
              <button type="button" onClick={() => setPending(job)}>Retry email delivery</button>
            ) : null}
            {job.action === "RETRY_EMAIL_CLASSIFICATION" ? (
              <button type="button" onClick={() => setPending(job)}>Retry email classification</button>
            ) : null}
          </li>
        ))}
      </ul>
      {jobs.length === 0 && !loading && !error ? <p>No failed or uncertain work requires attention.</p> : null}
      {pagination ? (
        <nav className="staff-pagination" aria-label="Failed work pages">
          <p>{rangeStart}–{rangeEnd} of {pagination.total}</p>
          <div className="staff-actions">
            <button type="button" disabled={loading || pagination.page <= 1} onClick={() => onPageChange?.(pagination.page - 1)}>Previous</button>
            <button type="button" disabled={loading || pagination.page >= pagination.total_pages} onClick={() => onPageChange?.(pagination.page + 1)}>Next</button>
          </div>
        </nav>
      ) : null}
      {pending ? (
        <ConfirmDialog
          label={`${pending.action === "RETRY_DRIVE_SYNC" ? "Retry Drive sync" : pending.action === "RETRY_EMAIL_CLASSIFICATION" ? "Retry email classification" : "Retry email delivery"} confirmation`}
          confirmLabel="Confirm retry"
          onConfirm={() => void onRetry(pending.job_id).finally(() => setPending(null))}
          onCancel={() => setPending(null)}
        >
          <p>Retry through the owning domain state machine?</p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
