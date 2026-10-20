"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  AdminGmailSyncState,
  EmailQueueItem,
  EmailState,
  getAdminGmailSyncStates,
  listEmailQueue,
  requestAdminGmailManualSync,
} from "../../lib/staff-api";

const reviewStates: EmailState[] = [
  "AWAITING_REVIEW",
  "APPROVED",
  "SEND_PENDING",
  "SENDING",
  "SEND_RETRY_WAIT",
  "DELIVERY_UNKNOWN",
];

const stateLabel = (state: EmailState) =>
  state
    .toLowerCase()
    .replaceAll("_", " ")
    .replace(/^./, (value) => value.toUpperCase());

const activeJobStates = new Set(["PENDING", "RUNNING", "RECONCILIATION"]);
const recoveryStates = new Set(["PAUSED_ERROR", "RECOVERY_REQUIRED", "RECOVERY_TRUNCATED"]);

export function EmailQueue({ canSync = false }: { canSync?: boolean }) {
  const [items, setItems] = useState<EmailQueueItem[]>([]);
  const [state, setState] = useState<EmailState | "ALL">("ALL");
  const [notice, setNotice] = useState<string | null>(null);
  const [syncStates, setSyncStates] = useState<AdminGmailSyncState[]>([]);
  const [connectorId, setConnectorId] = useState("");
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncNotice, setSyncNotice] = useState<string | null>(null);

  const refreshQueue = useCallback(() => {
    setNotice(null);
    void listEmailQueue(state === "ALL" ? reviewStates : [state])
      .then(setItems)
      .catch(() => setNotice("Unable to load the email queue."));
  }, [state]);

  useEffect(refreshQueue, [refreshQueue]);

  const selectedSync = useMemo(
    () => syncStates.find((value) => value.connector_id === connectorId) ?? syncStates[0],
    [connectorId, syncStates],
  );

  const refreshSync = useCallback(async () => {
    const values = await getAdminGmailSyncStates();
    setSyncStates(values);
    setConnectorId((current) => current || values[0]?.connector_id || "");
    return values;
  }, []);

  useEffect(() => {
    if (!canSync) return;
    void refreshSync().catch(() => setSyncNotice("Unable to load Gmail synchronization status."));
  }, [canSync, refreshSync]);

  useEffect(() => {
    if (!canSync || !selectedSync?.active_job_state || !activeJobStates.has(selectedSync.active_job_state)) return;
    const jobId = selectedSync.active_job_id;
    const previousSuccess = selectedSync.last_success_at;
    const timeout = window.setTimeout(() => {
      void refreshSync()
        .then((values) => {
          const updated = values.find((value) => value.connector_id === selectedSync.connector_id);
          if (!updated) return;
          if (updated.active_job_id === jobId && updated.active_job_state && activeJobStates.has(updated.active_job_state)) return;
          if (recoveryStates.has(updated.sync_status) || updated.last_error_code) {
            setSyncNotice(`Synchronization needs administrator recovery: ${updated.last_error_code ?? updated.sync_status}.`);
            return;
          }
          if (updated.last_success_at && updated.last_success_at !== previousSuccess) {
            setSyncNotice("Email synchronization completed. Classification and draft generation may still be pending.");
            refreshQueue();
          }
        })
        .catch(() => setSyncNotice("Unable to refresh Gmail synchronization status."));
    }, 1500);
    return () => window.clearTimeout(timeout);
  }, [canSync, refreshQueue, refreshSync, selectedSync]);

  async function syncEmails() {
    if (!selectedSync || syncBusy || (selectedSync.active_job_state && activeJobStates.has(selectedSync.active_job_state))) return;
    setSyncBusy(true);
    setSyncNotice(null);
    try {
      const accepted = await requestAdminGmailManualSync(
        selectedSync,
        "Manual synchronization requested from the staff email queue.",
      );
      setSyncStates((values) => values.map((value) => value.connector_id === accepted.connector_id ? accepted : value));
      setSyncNotice("Synchronization accepted. New mail is being retrieved; classification and draft generation may still be pending.");
    } catch {
      setSyncNotice("Unable to start synchronization. Review the connector and recovery status.");
      await refreshSync().catch(() => undefined);
    } finally {
      setSyncBusy(false);
    }
  }

  return (
    <section aria-labelledby="email-queue-heading">
      {canSync ? <div className="staff-card">
        <div className="staff-toolbar">
          <div><h2>Email synchronization</h2><p>Retrieve new Gmail messages without sending email.</p></div>
          {syncStates.length > 1 ? <label htmlFor="gmail-sync-connector">Mailbox
            <select id="gmail-sync-connector" value={selectedSync?.connector_id ?? ""} onChange={(event) => setConnectorId(event.target.value)}>
              {syncStates.map((value) => <option key={value.connector_id} value={value.connector_id}>{value.mailbox_address ?? value.connector_id}</option>)}
            </select>
          </label> : null}
        </div>
        {selectedSync ? <>
          <div className="staff-list-meta">
            <span>Polling {selectedSync.polling_enabled ? "enabled" : "disabled"}</span>
            <span>Push {selectedSync.push_enabled ? "enabled" : "disabled"} · Watch {selectedSync.watch_status.toLowerCase()}</span>
            <span>Last successful sync: {selectedSync.last_success_at ? new Date(selectedSync.last_success_at).toLocaleString() : "Never"}</span>
          </div>
          <p><span className="staff-status">{selectedSync.active_job_state ?? selectedSync.sync_status}</span></p>
          {recoveryStates.has(selectedSync.sync_status) ? <p role="alert">Synchronization is {selectedSync.sync_status}. Use Gmail Recovery in Authorization before syncing again.</p> : null}
          <button
            type="button"
            className="button-primary"
            disabled={syncBusy || recoveryStates.has(selectedSync.sync_status) || Boolean(selectedSync.active_job_state && activeJobStates.has(selectedSync.active_job_state))}
            onClick={() => void syncEmails()}
          >
            {syncBusy ? "Syncing…" : selectedSync.active_job_state && activeJobStates.has(selectedSync.active_job_state) ? "Sync in progress" : "Sync emails"}
          </button>
        </> : <p>No authorized Gmail connector is available.</p>}
        <p className="staff-notice" aria-live="polite">{syncNotice}</p>
      </div> : null}
      <div className="staff-toolbar">
        <div><h2 id="email-queue-heading">Email review queue</h2><p>Review drafts and monitor delivery work.</p></div>
        <label htmlFor="email-state-filter">Status
          <select
            id="email-state-filter"
            value={state}
            onChange={(event) => setState(event.target.value as EmailState | "ALL")}
          >
            <option value="ALL">All open work</option>
            {reviewStates.map((value) => <option key={value} value={value}>{stateLabel(value)}</option>)}
          </select>
        </label>
      </div>
      <p className="staff-notice" aria-live="polite">{notice}</p>
      <ul className="staff-list" aria-label="Email review items">
        {items.map((item) => (
          <li key={item.id}>
            <div className="staff-list-row">
              <div>
                <Link href={`/staff/email/${item.id}`} aria-label={`Review ${item.subject}`}>{item.subject}</Link>
                <p>From {item.sender}</p>
                <div className="staff-list-meta">
                  <span>{item.priority ? `${stateLabel(item.priority as EmailState)} priority` : "Unprioritized"}</span>
                </div>
              </div>
              <span className="staff-status">{stateLabel(item.state)}</span>
            </div>
          </li>
        ))}
      </ul>
      {items.length === 0 && !notice ? <p>No email work matches this filter.</p> : null}
    </section>
  );
}
