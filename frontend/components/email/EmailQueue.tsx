"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { EmailQueueItem, EmailState, listEmailQueue } from "../../lib/staff-api";

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

export function EmailQueue() {
  const [items, setItems] = useState<EmailQueueItem[]>([]);
  const [state, setState] = useState<EmailState | "ALL">("ALL");
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setNotice(null);
    void listEmailQueue(state === "ALL" ? reviewStates : [state])
      .then(setItems)
      .catch(() => setNotice("Unable to load the email queue."));
  }, [state]);

  return (
    <section aria-labelledby="email-queue-heading">
      <h1 id="email-queue-heading">Email review queue</h1>
      <label htmlFor="email-state-filter">State</label>
      <select
        id="email-state-filter"
        value={state}
        onChange={(event) => setState(event.target.value as EmailState | "ALL")}
      >
        <option value="ALL">Open review and delivery work</option>
        {reviewStates.map((value) => (
          <option key={value} value={value}>
            {stateLabel(value)}
          </option>
        ))}
      </select>
      <p aria-live="polite">{notice}</p>
      <ul aria-label="Email review items">
        {items.map((item) => (
          <li key={item.id}>
            <Link href={`/staff/email/${item.id}`} aria-label={`Review ${item.subject}`}>
              {item.subject}
            </Link>
            <p>From {item.sender}</p>
            <p>{item.priority ? `${stateLabel(item.priority as EmailState)} priority` : "Unprioritized"}</p>
            <p>{stateLabel(item.state)}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
