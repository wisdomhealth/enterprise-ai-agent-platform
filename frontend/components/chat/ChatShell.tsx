"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import {
  PublicChatSession,
  PublicChatStart,
  requestPublicHandoff,
  sendPublicChatMessage,
  startPublicChat,
} from "../../lib/public-chat-api";
import { connectChatEvents } from "../../lib/sse";
import { MessageList } from "./MessageList";

type ChatShellProps = { publicKey: string };

export function ChatShell({ publicKey }: ChatShellProps) {
  const [customerName, setCustomerName] = useState("");
  const [customerEmail, setCustomerEmail] = useState("");
  const [chat, setChat] = useState<PublicChatStart | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [messageBody, setMessageBody] = useState("");
  const [messages, setMessages] = useState<PublicChatSession["messages"]>([]);
  const [conversationState, setConversationState] = useState<PublicChatSession["state"] | null>(null);
  const eventCursor = useRef("0");
  const deliveredCursors = useRef(new Set<string>());

  async function startChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStarting(true);
    setError(null);
    try {
      setChat(
        await startPublicChat({
          publicKey,
          customerName,
          customerEmail,
        }),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to start chat.");
    } finally {
      setStarting(false);
    }
  }

  useEffect(() => {
    if (chat === null) return;
    const controller = new AbortController();
    void connectChatEvents({
      sessionId: chat.session.id,
      token: chat.credential.token,
      after: eventCursor.current,
      signal: controller.signal,
      onEvent: (event) => {
        // Session-state is a snapshot, not an answer-progress cursor.  It
        // must never move a reconnect backwards from a persisted segment.
        if (event.event === "session.state") {
          const state = event.data.state;
          if (typeof state === "string") setConversationState(state as PublicChatSession["state"]);
          return;
        }
        if (deliveredCursors.current.has(event.cursor)) return;
        deliveredCursors.current.add(event.cursor);
        eventCursor.current = event.cursor;
        if (event.event === "message.validated") return;
        const body = String(event.data.body ?? event.data.text ?? "");
        const actor = event.event === "error.safe" ? "SYSTEM" : "AI";
        setMessages((current) => {
          const existing = current.find((message) => message.sequence === event.sequence);
          if (existing !== undefined && event.event === "message.segment") {
            return current.map((message) =>
              message.sequence === event.sequence
                ? { ...message, body: `${message.body}${body}` }
                : message,
            );
          }
          if (existing !== undefined) return current;
          return [
            ...current,
            {
              sequence: event.sequence,
              actor,
              body,
              status: "PERSISTED",
              created_at: new Date().toISOString(),
            },
          ];
        });
      },
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : "Unable to reconnect to chat.");
      }
    });
    return () => controller.abort();
  }, [chat]);

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (chat === null || !messageBody.trim()) return;
    setError(null);
    try {
      const message = await sendPublicChatMessage({
        sessionId: chat.session.id,
        token: chat.credential.token,
        body: messageBody.trim(),
      });
      setMessages((current) => [...current, message]);
      setMessageBody("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to send message.");
    }
  }

  async function requestPerson() {
    if (chat === null) return;
    setError(null);
    try {
      const handoff = await requestPublicHandoff({
        sessionId: chat.session.id,
        token: chat.credential.token,
        contactName: customerName,
        contactEmail: customerEmail,
      });
      setConversationState(handoff.state);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to request support.");
    }
  }

  if (chat !== null) {
    const state = conversationState ?? chat.session.state;
    const offline = state === "QUEUED" || state === "HUMAN_ACTIVE";
    return (
      <section aria-label="Customer support chat">
        <h1>How can we help?</h1>
        <p role="status">
          {state === "QUEUED"
            ? "Your request is queued for a support person."
            : state === "HUMAN_ACTIVE"
              ? "A support person is handling your conversation."
              : "Chat started. An AI assistant is ready to help."}
        </p>
        {offline ? <p>We are not promising a live response. Follow up may arrive by email.</p> : null}
        <MessageList messages={messages} />
        <form onSubmit={sendMessage}>
          <label htmlFor="chat-message">Message</label>
          <textarea
            id="chat-message"
            value={messageBody}
            onChange={(event) => setMessageBody(event.target.value)}
            maxLength={4000}
          />
          <button type="submit">Send</button>
        </form>
        {state === "AI_ACTIVE" ? (
          <button type="button" onClick={() => void requestPerson()}>
            Request a person
          </button>
        ) : null}
        {error === null ? null : <p role="alert">{error}</p>}
      </section>
    );
  }

  return (
    <section aria-label="Customer support chat">
      <h1>How can we help?</h1>
      <p>You can begin without sharing contact details.</p>
      <form onSubmit={startChat}>
        <label htmlFor="customer-name">Name (optional)</label>
        <input
          id="customer-name"
          name="customer-name"
          value={customerName}
          onChange={(event) => setCustomerName(event.target.value)}
          autoComplete="name"
        />
        <label htmlFor="customer-email">Email</label>
        <input
          id="customer-email"
          name="customer-email"
          type="email"
          value={customerEmail}
          onChange={(event) => setCustomerEmail(event.target.value)}
          autoComplete="email"
        />
        <button type="submit" disabled={starting}>
          {starting ? "Starting chat…" : "Start chat"}
        </button>
      </form>
      {error === null ? null : <p role="alert">{error}</p>}
    </section>
  );
}
