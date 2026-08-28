"use client";

import { useEffect, useRef, useState } from "react";

import { claimHandoff, listSupportQueue, SupportHandoff } from "../../lib/staff-api";

const stateLabel: Record<SupportHandoff["state"], string> = {
  AI_ACTIVE: "AI active",
  HANDOFF_REQUESTED: "Handoff requested",
  QUEUED: "Queued",
  HUMAN_ACTIVE: "Human active",
  RESOLVED: "Resolved",
};

export function SupportQueue({ onSelect }: { onSelect?: (handoff: SupportHandoff) => void }) {
  const [handoffs, setHandoffs] = useState<SupportHandoff[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const selectionButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    void listSupportQueue()
      .then(setHandoffs)
      .catch(() => setNotice("Unable to load the support queue."));
  }, []);

  async function claim(handoff: SupportHandoff) {
    setNotice(null);
    try {
      const claimed = await claimHandoff(handoff.id, handoff.version);
      setHandoffs((current) => current.map((item) => (item.id === handoff.id ? claimed : item)));
      onSelect?.(claimed);
      setNotice("Conversation claimed.");
    } catch (error) {
      if (
        typeof error === "object" &&
        error !== null &&
        "status" in error &&
        error.status === 409 &&
        "handoff" in error &&
        error.handoff !== undefined
      ) {
        const current = { ...handoff, ...(error.handoff as Partial<SupportHandoff>) };
        setHandoffs((items) => items.map((item) => (item.id === handoff.id ? current : item)));
        onSelect?.(current);
        setNotice("Already claimed");
      } else {
        setNotice("Unable to claim the conversation.");
      }
    } finally {
      selectionButton.current?.focus();
    }
  }

  return (
    <section aria-labelledby="support-queue-heading">
      <h1 id="support-queue-heading">Support queue</h1>
      <p aria-live="polite">{notice}</p>
      <ul aria-label="Queued customer conversations">
        {handoffs.map((handoff) => (
          <li key={handoff.id}>
            <button ref={selectionButton} type="button" onClick={() => onSelect?.(handoff)}>
              Conversation {handoff.session_id}
            </button>
            <span> {stateLabel[handoff.state]}</span>
            <span> Version {handoff.version}</span>
            {handoff.state === "QUEUED" ? (
              <button type="button" onClick={() => void claim(handoff)}>
                Claim
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
