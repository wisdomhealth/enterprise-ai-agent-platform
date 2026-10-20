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
  const [busyId, setBusyId] = useState<string | null>(null);
  const selectionButtons = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    void listSupportQueue()
      .then(setHandoffs)
      .catch(() => setNotice("Unable to load the support queue."));
  }, []);

  async function claim(handoff: SupportHandoff) {
    if (busyId) return;
    setBusyId(handoff.id);
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
        const authoritative = error.handoff as Partial<SupportHandoff>;
        const current = {
          ...handoff,
          state: authoritative.state ?? handoff.state,
          version: authoritative.version ?? handoff.version,
        };
        setHandoffs((items) => items.map((item) => (item.id === handoff.id ? current : item)));
        onSelect?.(current);
        setNotice("Already claimed");
      } else {
        setNotice("Unable to claim the conversation.");
      }
    } finally {
      setBusyId(null);
      selectionButtons.current.get(handoff.id)?.focus();
    }
  }

  return (
    <section aria-labelledby="support-queue-heading">
      <h2 id="support-queue-heading">Conversation queue</h2>
      <p>Customer conversations waiting for review or human ownership.</p>
      <p className="staff-notice" aria-live="polite">{notice}</p>
      <ul className="staff-list" aria-label="Queued customer conversations">
        {handoffs.map((handoff) => (
          <li key={handoff.id}>
            <button
              ref={(button) => {
                if (button === null) selectionButtons.current.delete(handoff.id);
                else selectionButtons.current.set(handoff.id, button);
              }}
              type="button"
              onClick={() => onSelect?.(handoff)}
            >
              Conversation {handoff.session_id}
            </button>
            <span className="staff-status" data-tone={handoff.state === "HUMAN_ACTIVE" ? "success" : handoff.state === "HANDOFF_REQUESTED" || handoff.state === "QUEUED" ? "warning" : undefined}>{stateLabel[handoff.state]}</span>
            <span className="staff-list-meta">Version {handoff.version}</span>
            {handoff.state === "QUEUED" ? (
              <button type="button" className="button-primary" disabled={busyId !== null} onClick={() => void claim(handoff)}>
                {busyId === handoff.id ? "Claiming…" : "Claim"}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {handoffs.length === 0 && !notice ? <p>No conversations need attention.</p> : null}
    </section>
  );
}
