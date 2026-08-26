"use client";

import { FormEvent, useState } from "react";

import { PublicChatStart, startPublicChat } from "../../lib/public-chat-api";

type ChatShellProps = { publicKey: string };

export function ChatShell({ publicKey }: ChatShellProps) {
  const [customerName, setCustomerName] = useState("");
  const [customerEmail, setCustomerEmail] = useState("");
  const [chat, setChat] = useState<PublicChatStart | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

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

  if (chat !== null) {
    return (
      <section aria-label="Customer support chat">
        <h1>How can we help?</h1>
        <p role="status">Chat started. An AI assistant is ready to help.</p>
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
