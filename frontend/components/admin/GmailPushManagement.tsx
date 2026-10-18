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
  return (
    <section aria-labelledby="gmail-push-heading">
      <h2 id="gmail-push-heading">Gmail Push Setup</h2>
      <p>Google authorization, watch lifecycle, and application access are managed separately.</p>
      {states.length === 0 ? <p>No Gmail mailbox has been registered.</p> : states.map((state) => (
        <article key={state.connector_id}>
          <h3>{state.mailbox_address ?? "Mailbox registration pending"}</h3>
          <p>Automatic sync: {state.auto_sync_enabled ? "Enabled" : "Stopped"}</p>
          <p>Watch: {state.watch_status} · Sync: {state.sync_status}</p>
          <p>Watch expires: {state.watch_expiration_at ?? "Not available"}</p>
          {state.last_error_code ? <p role="alert">Last error: {state.last_error_code}</p> : null}
          <div>
            <button type="button" onClick={() => void onWatch(state, "start")}>Start</button>
            <button type="button" onClick={() => void onWatch(state, "renew")}>Renew</button>
            <button type="button" onClick={() => void onWatch(state, "stop")}>Stop watching</button>
          </div>
          <label>
            Administrative reason
            <input value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <button type="button" disabled={!reason.trim()} onClick={() => void onManualSync(state, reason)}>
            Run manual sync
          </button>
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
              disabled={!reason.trim() || additionalMessages < 1}
              onClick={() => void onContinueRecovery(state, additionalMessages, reason)}
            >
              Continue recovery
            </button>
            <button
              type="button"
              disabled={!reason.trim()}
              onClick={() => {
                const warning = `Accept limited recovery after ${state.recovery_processed_count} messages? `
                  + "Some older or truncated messages may remain missing. H0 incremental catch-up will still run.";
                if (window.confirm(warning)) void onAcceptLimitedRecovery(state, reason);
              }}
            >
              Accept limited recovery
            </button>
          </div> : null}
        </article>
      ))}
    </section>
  );
}
