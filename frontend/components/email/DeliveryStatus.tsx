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

export function DeliveryStatus({ delivery }: { delivery: EmailDelivery }) {
  const [current, setCurrent] = useState(delivery);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(operation: "reconcile" | "retry") {
    setBusy(true);
    setNotice(null);
    try {
      const result =
        operation === "reconcile"
          ? await reconcileEmailDelivery(current.id, current.version)
          : await retryEmailDelivery(current.id, current.version);
      setCurrent((value) => ({ ...value, state: result.state, version: result.version }));
      setNotice(operation === "reconcile" ? "Gmail delivery checked." : "Delivery retry queued.");
    } catch (error) {
      if (error instanceof StaffApiError && error.status === 409 && error.email) {
        setCurrent((value) => ({
          ...value,
          state: error.email?.state ?? value.state,
          version: error.email?.version ?? value.version,
        }));
        setNotice("Delivery changed elsewhere. The latest state is shown.");
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
      <p>{stateLabel(current.state)}</p>
      <p aria-live="polite">{notice}</p>
      {current.state === "DELIVERY_UNKNOWN" ? (
        <>
          <p>
            This message may already have been sent. Check Gmail before any new attempt to avoid a
            duplicate customer email.
          </p>
          <button type="button" disabled={busy} onClick={() => void run("reconcile")}>
            Check Gmail delivery
          </button>
        </>
      ) : null}
      {current.state === "SEND_RETRY_WAIT" ? (
        <button type="button" disabled={busy} onClick={() => void run("retry")}>
          Retry delivery
        </button>
      ) : null}
      <p>Message identity: {current.deterministic_message_id}</p>
      {current.last_error_code ? <p>Error: {current.last_error_code}</p> : null}
      <ol aria-label="Delivery attempts">
        {current.attempts.map((attempt) => (
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
