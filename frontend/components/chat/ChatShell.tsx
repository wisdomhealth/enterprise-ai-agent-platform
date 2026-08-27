"use client";

import { FormEvent, useEffect, useState } from "react";

import {
  PublicChatSession,
  PublicChatStart,
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
      after: messages.at(-1)?.sequence ?? 0,
      signal: controller.signal,
      onEvent: (event) => {
        if (event.event !== "message.validated" && event.event !== "error.safe") return;
        const body = String(event.data.body ?? "");
        const actor = event.event === "error.safe" ? "SYSTEM" : "AI";
        setMessages((current) => {
          if (current.some((message) => message.sequence === event.sequence)) return current;
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

  if (chat !== null) {
    return (
      <section aria-label="Customer support chat">
        <h1>How can we help?</h1>
        <p role="status">Chat started. An AI assistant is ready to help.</p>
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
