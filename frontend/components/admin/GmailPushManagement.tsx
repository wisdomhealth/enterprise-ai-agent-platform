"use client";

import { useState } from "react";

import { AdminGmailSyncState } from "../../lib/staff-api";

export function GmailPushManagement({
  states,
  onWatch,
  onManualSync,
  onContinueRecovery,
  onAcceptLimitedRecovery,
}: {
  states: AdminGmailSyncState[];
  onWatch: (state: AdminGmailSyncState, action: "start" | "renew" | "stop") => Promise<void>;
  onManualSync: (state: AdminGmailSyncState, reason: string) => Promise<void>;
  onContinueRecovery: (
    state: AdminGmailSyncState,
    additionalMessages: number,
    reason: string,
  ) => Promise<void>;
  onAcceptLimitedRecovery: (state: AdminGmailSyncState, reason: string) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [additionalMessages, setAdditionalMessages] = useState(500);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  async function run(key: string, operation: () => Promise<void>) {
    if (busyAction) return;
    setBusyAction(key);
    setNotice(null);
    try {
      await operation();
      setNotice("Gmail operation accepted. Refresh status to confirm completion.");
    } catch {
      setNotice("Unable to complete the Gmail operation.");
    } finally {
      setBusyAction(null);
    }
  }
  return (
    <section aria-labelledby="gmail-push-heading">
      <h2 id="gmail-push-heading">Gmail synchronization</h2>
      <p>Hourly polling and Gmail Push are independent. Watch controls never disable polling.</p>
      <p className="staff-notice" aria-live="polite">{notice}</p>
      {states.length === 0 ? <p>No Gmail mailbox has been registered.</p> : states.map((state) => (
        <article className="staff-card" key={state.connector_id}>
          <h3>{state.mailbox_address ?? "Mailbox registration pending"}</h3>
          <div className="staff-summary-grid"><div><strong>Push listener</strong><p><span className="staff-status" data-tone={state.watch_status === "ACTIVE" ? "success" : "warning"}>{state.push_enabled ? `Enabled · ${state.watch_status}` : `Disabled · ${state.watch_status}`}</span></p><p>Expires: {state.watch_expiration_at ?? "Not available"}</p></div><div><strong>Scheduled polling</strong><p><span className="staff-status" data-tone={state.polling_enabled ? "success" : "warning"}>{state.polling_enabled ? "Enabled" : "Disabled"}</span></p><p>Sync state: {state.sync_status}</p><p>Last success: {state.last_success_at ?? "Never"}</p></div></div>
          {state.last_error_code ? <p role="alert">Last error: {state.last_error_code}</p> : null}
          <div className="staff-actions">
            <button type="button" className="button-primary" disabled={busyAction !== null} onClick={() => void run(`${state.connector_id}:start`, () => onWatch(state, "start"))}>{busyAction === `${state.connector_id}:start` ? "Starting…" : "Start watch"}</button>
            <button type="button" disabled={busyAction !== null} onClick={() => void run(`${state.connector_id}:renew`, () => onWatch(state, "renew"))}>{busyAction === `${state.connector_id}:renew` ? "Renewing…" : "Renew watch"}</button>
            <button type="button" className="button-danger" disabled={busyAction !== null} onClick={() => void run(`${state.connector_id}:stop`, () => onWatch(state, "stop"))}>Stop watching</button>
          </div>
          <label>
            Administrative reason
            <input value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <button type="button" disabled={!reason.trim() || busyAction !== null || ["PAUSED_ERROR", "RECOVERY_REQUIRED", "RECOVERY_TRUNCATED"].includes(state.sync_status) || Boolean(state.active_job_state && ["PENDING", "RUNNING", "RECONCILIATION"].includes(state.active_job_state))} onClick={() => void run(`${state.connector_id}:sync`, () => onManualSync(state, reason))}>
            {busyAction === `${state.connector_id}:sync` ? "Starting sync…" : state.active_job_state && ["PENDING", "RUNNING", "RECONCILIATION"].includes(state.active_job_state) ? "Sync in progress" : "Run manual sync"}
          </button>
          {["PAUSED_ERROR", "RECOVERY_REQUIRED"].includes(state.sync_status) ? <p role="alert">Manual sync is paused. Reauthorize or use the existing authorized recovery action before retrying.</p> : null}
          {state.sync_status === "RECOVERY_TRUNCATED" ? <div>
            <p role="alert">
              Recovery scanned from {state.recovery_window_started_at ?? "the configured window"},
              processed {state.recovery_processed_count} messages, and stopped at the configured
              limit. Older or unscanned messages may be missing.
            </p>
            <label>
              Additional message allowance
              <input
                type="number"
                min={1}
                max={10000}
                value={additionalMessages}
                onChange={(event) => setAdditionalMessages(Number(event.target.value))}
              />
            </label>
            <button
              type="button"
              disabled={!reason.trim() || additionalMessages < 1 || busyAction !== null}
              onClick={() => void run(`${state.connector_id}:continue`, () => onContinueRecovery(state, additionalMessages, reason))}
            >
              Continue recovery
            </button>
            <button
              type="button"
              className="button-danger"
              disabled={!reason.trim() || busyAction !== null}
              onClick={() => {
                const warning = `Accept limited recovery after ${state.recovery_processed_count} messages? `
                  + "Some older or truncated messages may remain missing. H0 incremental catch-up will still run.";
                if (window.confirm(warning)) void run(`${state.connector_id}:accept`, () => onAcceptLimitedRecovery(state, reason));
              }}
            >
              Accept limited recovery
            </button>
          </div> : null}
          <details className="staff-detail"><summary>Technical diagnostics</summary><p>Processed history ID: {state.processed_history_id ?? "Not available"}</p><p>Pending history ID: {state.pending_history_id ?? "None"}</p><p>Recovery generation: {state.version}</p></details>
        </article>
      ))}
    </section>
  );
}
