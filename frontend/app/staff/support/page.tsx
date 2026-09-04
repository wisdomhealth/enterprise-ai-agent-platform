"use client";

import { useRef, useState } from "react";

import { ConversationPanel } from "../../../components/support/ConversationPanel";
import { StaffAssist } from "../../../components/support/StaffAssist";
import { SupportQueue } from "../../../components/support/SupportQueue";
import {
  getSupportConversation,
  SupportConversation,
  SupportHandoff,
} from "../../../lib/staff-api";

export default function StaffSupportPage() {
  const [conversation, setConversation] = useState<SupportConversation | null>(null);
  const latestSelection = useRef(0);

  async function select(handoff: SupportHandoff) {
    const selection = ++latestSelection.current;
    setConversation((current) => (current?.id === handoff.id ? current : null));
    const detail = await getSupportConversation(handoff.id);
    if (selection !== latestSelection.current || detail.id !== handoff.id) return;
    const selected = detail.version < handoff.version ? { ...detail, ...handoff } : detail;
    setConversation((current) => {
      if (current?.id !== selected.id || selected.version > current.version) return selected;
      if (selected.version < current.version) return current;
      const messages = new Map(current.messages.map((message) => [message.sequence, message]));
      selected.messages.forEach((message) => messages.set(message.sequence, message));
      return {
        ...current,
        messages: [...messages.values()].sort((left, right) => left.sequence - right.sequence),
      };
    });
  }

  return (
    <section aria-label="Staff support console">
      <SupportQueue onSelect={select} />
      {conversation ? <ConversationPanel conversation={conversation} onUpdate={select} /> : null}
      <StaffAssist />
    </section>
  );
}
