"use client";

import { useState } from "react";

import {
  EmailDelivery,
  EmailState,
  reconcileEmailDelivery,
  retryEmailDelivery,
  StaffApiError,
} from "../../lib/staff-api";

const stateLabel = (state: EmailState) =>
  state
    .toLowerCase()
    .replaceAll("_", " ")
    .replace(/^./, (value) => value.toUpperCase());

export function DeliveryStatus({
  delivery,
  onRefresh,
}: {
  delivery: EmailDelivery;
  onRefresh: () => Promise<void>;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [conflictLocked, setConflictLocked] = useState(false);

  async function run(operation: "reconcile" | "retry") {
    setBusy(true);
    setNotice(null);
    try {
      if (operation === "reconcile") {
        await reconcileEmailDelivery(delivery.id, delivery.version);
      } else {
        await retryEmailDelivery(delivery.id, delivery.version);
      }
      await onRefresh();
      setConflictLocked(false);
      setNotice(operation === "reconcile" ? "Gmail delivery checked." : "Delivery retry queued.");
    } catch (error) {
      if (error instanceof StaffApiError && error.status === 409) {
        setConflictLocked(true);
        try {
          await onRefresh();
          setConflictLocked(false);
          setNotice("Delivery changed elsewhere. The latest state is shown.");
        } catch {
          setNotice("Delivery changed elsewhere. Reload before taking another action.");
        }
      } else {
        setNotice("Unable to update delivery status.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="delivery-heading">
      <h2 id="delivery-heading">Delivery</h2>
      <p>{stateLabel(delivery.state)}</p>
      <p aria-live="polite">{notice}</p>
      {delivery.state === "DELIVERY_UNKNOWN" ? (
        <>
          <p>
            This message may already have been sent. Check Gmail before any new attempt to avoid a
            duplicate customer email.
          </p>
          <button
            type="button"
            disabled={busy || conflictLocked}
            onClick={() => void run("reconcile")}
          >
            Check Gmail delivery
          </button>
        </>
      ) : null}
      {delivery.state === "SEND_RETRY_WAIT" ? (
        <button
          type="button"
          disabled={busy || conflictLocked}
          onClick={() => void run("retry")}
        >
          Retry delivery
        </button>
      ) : null}
      <p>Message identity: {delivery.deterministic_message_id}</p>
      {delivery.last_error_code ? <p>Error: {delivery.last_error_code}</p> : null}
      <ol aria-label="Delivery attempts">
        {delivery.attempts.map((attempt) => (
          <li key={attempt.id}>
            Attempt {attempt.attempt_number}: {stateLabel(attempt.outcome as EmailState)}
            {attempt.error_code ? ` (${attempt.error_code})` : ""}
            {` · Started ${attempt.started_at}`}
            {attempt.completed_at ? ` · Completed ${attempt.completed_at}` : ""}
          </li>
        ))}
      </ol>
    </section>
  );
}
