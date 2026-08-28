"use client";

import { useState } from "react";

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

  async function select(handoff: SupportHandoff) {
    const detail = await getSupportConversation(handoff.id);
    setConversation(detail);
  }

  return (
    <section aria-label="Staff support console">
      <SupportQueue onSelect={select} />
      {conversation ? <ConversationPanel conversation={conversation} onUpdate={select} /> : null}
      <StaffAssist />
    </section>
  );
}
