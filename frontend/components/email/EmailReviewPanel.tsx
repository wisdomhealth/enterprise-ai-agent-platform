"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  approveEmail,
  editEmailDraft,
  EmailActionResult,
  EmailDetail,
  getEmailDetail,
  regenerateEmailDraft,
  rejectEmail,
  StaffApiError,
} from "../../lib/staff-api";
import { DeliveryStatus } from "./DeliveryStatus";
import { DraftHistory } from "./DraftHistory";

const stateLabel = (state: EmailDetail["state"]) =>
  state
    .toLowerCase()
    .replaceAll("_", " ")
    .replace(/^./, (value) => value.toUpperCase());

export function EmailReviewPanel({ item }: { item: EmailDetail }) {
  const [current, setCurrent] = useState(item);
  const draft = useMemo(
    () => current.drafts.find((candidate) => candidate.id === current.current_draft_id) ?? null,
    [current],
  );
  const [body, setBody] = useState(draft?.body ?? "");
  const [to, setTo] = useState(draft?.to.join(", ") ?? "");
  const [cc, setCc] = useState(draft?.cc.join(", ") ?? "");
  const [subject, setSubject] = useState(draft?.subject ?? "");
  const [threadId, setThreadId] = useState(draft?.thread_id ?? "");
  const [instruction, setInstruction] = useState("");
  const [confirmRegeneration, setConfirmRegeneration] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [conflictLocked, setConflictLocked] = useState(false);

  useEffect(() => {
    setBody(draft?.body ?? "");
    setTo(draft?.to.join(", ") ?? "");
    setCc(draft?.cc.join(", ") ?? "");
    setSubject(draft?.subject ?? "");
    setThreadId(draft?.thread_id ?? "");
  }, [draft]);

  const dirty =
    draft !== null &&
    (body !== draft.body ||
      to !== draft.to.join(", ") ||
      cc !== draft.cc.join(", ") ||
      subject !== draft.subject ||
      threadId !== draft.thread_id);
  const editable =
    !conflictLocked &&
    draft !== null &&
    ["AWAITING_REVIEW", "APPROVED", "SEND_PENDING"].includes(current.state);
  const displayState = dirty && ["APPROVED", "SEND_PENDING"].includes(current.state)
    ? "AWAITING_REVIEW"
    : current.state;

  async function applyResult(result: EmailActionResult) {
    setCurrent((value) => ({
      ...value,
      state: result.state,
      version: result.version,
      current_draft_id: result.current_draft_id,
    }));
    try {
      const fresh = await getEmailDetail(current.id);
      if (fresh) setCurrent(fresh);
    } catch {
      // The committed mutation result remains authoritative until a later refresh succeeds.
    }
  }

  async function refreshCurrent(): Promise<void> {
    const fresh = await getEmailDetail(current.id);
    setCurrent(fresh);
    setConflictLocked(false);
  }

  async function handleConflict(error: unknown): Promise<boolean> {
    if (!(error instanceof StaffApiError) || error.status !== 409) return false;
    setConflictLocked(true);
    try {
      await refreshCurrent();
      setNotice("This email changed elsewhere. The latest state is shown.");
    } catch {
      setNotice("This email changed elsewhere. Reload before taking another action.");
    }
    return true;
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editable || !draft || !dirty) return;
    setBusy(true);
    setNotice(null);
    try {
      await applyResult(
        await editEmailDraft(current.id, current.version, draft.id, {
          body,
          to: addresses(to),
          cc: addresses(cc),
          subject,
          thread_id: threadId,
        }),
      );
      setNotice("Draft saved. Approval must be reviewed again.");
    } catch (error) {
      if (!(await handleConflict(error))) setNotice("Unable to save the draft.");
    } finally {
      setBusy(false);
    }
  }

  async function review(operation: "approve" | "reject") {
    if (!draft || current.state !== "AWAITING_REVIEW") return;
    setBusy(true);
    setNotice(null);
    try {
      const result =
        operation === "approve"
          ? await approveEmail(current.id, current.version, draft.id)
          : await rejectEmail(current.id, current.version, draft.id);
      await applyResult(result);
      setNotice(operation === "approve" ? "Email approved." : "Email rejected.");
    } catch (error) {
      if (!(await handleConflict(error))) setNotice(`Unable to ${operation} the email.`);
    } finally {
      setBusy(false);
    }
  }

  async function regenerate() {
    if (!draft || current.state !== "AWAITING_REVIEW" || !instruction.trim()) return;
    setBusy(true);
    setNotice(null);
    try {
      await applyResult(
        await regenerateEmailDraft(current.id, current.version, draft.id, instruction.trim()),
      );
      setInstruction("");
      setConfirmRegeneration(false);
      setNotice("A new immutable draft version was generated.");
    } catch (error) {
      if (!(await handleConflict(error))) setNotice("Unable to regenerate the draft.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article aria-labelledby="email-review-heading">
      <h1 id="email-review-heading">Review {current.subject}</h1>
      <p aria-live="polite">{notice}</p>
      <p aria-label="Email state">{stateLabel(displayState)}</p>
      {dirty && ["APPROVED", "SEND_PENDING"].includes(current.state) ? (
        <p role="status">Approval will be cleared when these changes are saved.</p>
      ) : null}

      <section aria-labelledby="original-email-heading">
        <h2 id="original-email-heading">Original email</h2>
        <p>From: {current.sender}</p>
        <p>To: {current.recipients.join(", ")}</p>
        <p>Received: {current.received_at}</p>
        <p>{current.body}</p>
      </section>

      <section aria-labelledby="classification-heading">
        <h2 id="classification-heading">Classification</h2>
        <p>{current.classification_rationale}</p>
      </section>

      {draft ? (
        <form onSubmit={save} aria-label="Current email draft">
          <h2>Current draft</h2>
          <label htmlFor="draft-to">To</label>
          <input
            id="draft-to"
            value={to}
            disabled={!editable || busy}
            onChange={(event) => setTo(event.target.value)}
          />
          <label htmlFor="draft-cc">CC</label>
          <input
            id="draft-cc"
            value={cc}
            disabled={!editable || busy}
            onChange={(event) => setCc(event.target.value)}
          />
          <label htmlFor="draft-subject">Reply subject</label>
          <input
            id="draft-subject"
            value={subject}
            disabled={!editable || busy}
            onChange={(event) => setSubject(event.target.value)}
          />
          <label htmlFor="draft-thread">Gmail thread</label>
          <input
            id="draft-thread"
            value={threadId}
            disabled={!editable || busy}
            onChange={(event) => setThreadId(event.target.value)}
          />
          <label htmlFor="draft-body">Reply body</label>
          <textarea
            id="draft-body"
            value={body}
            disabled={!editable || busy}
            onChange={(event) => setBody(event.target.value)}
          />
          <p>Model {draft.model} · Prompt {draft.prompt_version}</p>
          {draft.reviewer_instruction ? <p>Reviewer instruction: {draft.reviewer_instruction}</p> : null}
          <ul aria-label="Current draft sources">
            {draft.citations.map((citation) => (
              <li key={citation.chunk_id}>
                <p>Internal source: {citation.title}</p>
                <p>Chunk {citation.chunk_id} · Version {citation.document_version_id}</p>
                {citation.internal_drive_link ? (
                  <a href={citation.internal_drive_link}>Open internal source</a>
                ) : null}
              </li>
            ))}
          </ul>
          {editable ? (
            <button type="submit" disabled={!dirty || busy}>
              Save changes
            </button>
          ) : null}
        </form>
      ) : (
        <p>No reviewable draft is available.</p>
      )}

      {draft && !conflictLocked && current.state === "AWAITING_REVIEW" ? (
        <section aria-label="Review actions">
          <button type="button" disabled={busy} onClick={() => void review("approve")}>
            Approve
          </button>
          <button type="button" disabled={busy} onClick={() => void review("reject")}>
            Reject
          </button>
          <label htmlFor="regeneration-instruction">Regeneration instruction</label>
          <textarea
            id="regeneration-instruction"
            value={instruction}
            disabled={busy}
            onChange={(event) => setInstruction(event.target.value)}
          />
          <button
            type="button"
            disabled={busy || !instruction.trim()}
            onClick={() => setConfirmRegeneration(true)}
          >
            Regenerate draft
          </button>
        </section>
      ) : null}

      {confirmRegeneration ? (
        <div role="dialog" aria-modal="true" aria-label="Regenerate draft confirmation">
          <p>Create a new immutable draft using this instruction?</p>
          <button type="button" disabled={busy} onClick={() => void regenerate()}>
            Confirm regeneration
          </button>
          <button type="button" onClick={() => setConfirmRegeneration(false)}>
            Cancel
          </button>
        </div>
      ) : null}

      {current.current_draft_id ? (
        <DraftHistory drafts={current.drafts} currentDraftId={current.current_draft_id} />
      ) : null}
      <section aria-labelledby="audit-heading">
        <h2 id="audit-heading">Audit transitions</h2>
        <ol>
          {current.audit_transitions.map((entry) => (
            <li key={entry.id}>
              <p>{entry.from_state} → {entry.to_state}</p>
              <p>{entry.action}{entry.reason_code ? ` · ${entry.reason_code}` : ""} · {entry.actor_type}</p>
              <p>{entry.created_at}</p>
            </li>
          ))}
        </ol>
      </section>
      {current.delivery ? (
        <DeliveryStatus delivery={current.delivery} onRefresh={refreshCurrent} />
      ) : null}
    </article>
  );
}

function addresses(value: string): string[] {
  return value
    .split(",")
    .map((address) => address.trim())
    .filter(Boolean);
}
