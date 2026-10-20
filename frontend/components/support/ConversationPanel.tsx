"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import {
  replyToHandoff,
  resumeAi,
  SupportConversation,
  SupportHandoff,
} from "../../lib/staff-api";

type ConversationPanelProps = {
  conversation: SupportConversation;
  onUpdate?: (handoff: SupportHandoff) => void;
};

export function ConversationPanel({ conversation, onUpdate }: ConversationPanelProps) {
  const [current, setCurrent] = useState<SupportConversation>(conversation);
  const [reply, setReply] = useState("");
  const [confirmResume, setConfirmResume] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const replyBox = useRef<HTMLTextAreaElement>(null);
  const selectedConversationId = useRef(conversation.id);

  useEffect(() => {
    setCurrent((value) => {
      if (value.id !== conversation.id || conversation.version > value.version) return conversation;
      if (conversation.version < value.version) return value;
      const messages = new Map(value.messages.map((message) => [message.sequence, message]));
      conversation.messages.forEach((message) => messages.set(message.sequence, message));
      return {
        ...value,
        messages: [...messages.values()].sort((left, right) => left.sequence - right.sequence),
      };
    });
    if (selectedConversationId.current === conversation.id) return;
    selectedConversationId.current = conversation.id;
    setReply("");
    setConfirmResume(false);
    setNotice(null);
  }, [conversation]);

  const humanOwned = current.state === "HUMAN_ACTIVE" && current.assigned_user_id !== null;

  function update(handoff: SupportHandoff) {
    const next = { ...current, ...handoff };
    setCurrent(next);
    onUpdate?.(handoff);
  }

  async function submitReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!humanOwned || !reply.trim() || busy) return;
    setBusy(true);
    try {
      const message = await replyToHandoff(current.id, current.version, reply.trim());
      setCurrent((value) => ({
        ...value,
        version: value.version + 1,
        messages: [
          ...value.messages,
          {
            ...message,
            status: "PERSISTED",
            created_at: new Date().toISOString(),
            citations: [],
          },
        ],
      }));
      setReply("");
      setNotice("Reply sent.");
    } catch {
      setNotice("Unable to send reply.");
    } finally {
      setBusy(false);
      replyBox.current?.focus();
    }
  }

  async function confirmResumeAi() {
    if (busy) return;
    setBusy(true);
    try {
      update(await resumeAi(current.id, current.version));
      setNotice("AI will wait for the next customer message.");
      setConfirmResume(false);
    } catch {
      setNotice("Unable to resume AI.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="conversation-heading">
      <div className="staff-toolbar"><div><h2 id="conversation-heading">Customer conversation</h2><p>Authorized transcript and human reply controls.</p></div><span className="staff-status" data-tone={humanOwned ? "success" : "warning"}>{humanOwned ? "Human handling" : "AI or queue handling"}</span></div>
      <p className="staff-notice" aria-live="polite">{notice}</p>
      <ol className="staff-message-list" aria-label="Authorized conversation transcript">
        {current.messages.map((message) => (
          <li key={message.sequence}>
            <strong>{message.actor}</strong>
            <p>{message.body}</p>
            {message.citations.map((citation) => (
              <details key={citation.chunk_id}>
                <summary>Internal source: {citation.title}</summary>
                <p>Chunk {citation.chunk_id}</p>
                <p>Version {citation.document_version_id}</p>
                {citation.section ? <p>Section {citation.section}</p> : null}
                {citation.page_number ? <p>Page {citation.page_number}</p> : null}
                {citation.internal_drive_link ? (
                  <a href={citation.internal_drive_link}>Open internal source</a>
                ) : null}
              </details>
            ))}
          </li>
        ))}
      </ol>
      {humanOwned ? (
        <form onSubmit={submitReply}>
          <label htmlFor="staff-reply">Reply to customer</label>
          <textarea
            ref={replyBox}
            id="staff-reply"
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            maxLength={4000}
            disabled={busy}
          />
          <button type="submit" disabled={busy || !reply.trim()}>{busy ? "Sending…" : "Send reply"}</button>
        </form>
      ) : null}
      {humanOwned ? (
        <button type="button" disabled={busy} onClick={() => setConfirmResume(true)}>
          Resume AI
        </button>
      ) : null}
      {confirmResume ? (
        <div className="staff-dialog" role="dialog" aria-modal="true" aria-label="Resume AI confirmation">
          <div><p>Resume AI only for the customer&apos;s next message?</p><div className="staff-actions">
            <button type="button" className="button-primary" disabled={busy} onClick={() => void confirmResumeAi()}>{busy ? "Resuming…" : "Confirm resume AI"}</button>
            <button type="button" disabled={busy} onClick={() => setConfirmResume(false)}>Cancel</button>
          </div></div>
        </div>
      ) : null}
    </section>
  );
}
