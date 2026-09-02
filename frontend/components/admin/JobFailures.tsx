"use client";

import { useState } from "react";

import { AdminFailedJob } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { formatUtc, statusLabel } from "./format";

export function JobFailures({
  jobs,
  onRetry,
  onReconcile,
}: {
  jobs: AdminFailedJob[];
  onRetry: (jobId: string) => Promise<void>;
  onReconcile: (resourceId: string) => void;
}) {
  const [pending, setPending] = useState<AdminFailedJob | null>(null);
  return (
    <section aria-labelledby="job-failures-heading">
      <h2 id="job-failures-heading">Failed and uncertain work</h2>
      <ul>
        {jobs.map((job) => (
          <li key={job.job_id}>
            <strong>{statusLabel(job.kind)}</strong>
            {` · ${statusLabel(job.state)} · ${job.attempts} attempts · ${formatUtc(job.updated_at)}`}
            {job.error_code ? <p>Error: {job.error_code}</p> : null}
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
          </li>
        ))}
      </ul>
      {pending ? (
        <ConfirmDialog
          label={`${pending.action === "RETRY_DRIVE_SYNC" ? "Retry Drive sync" : "Retry email delivery"} confirmation`}
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
