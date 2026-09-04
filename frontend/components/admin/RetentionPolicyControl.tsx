"use client";

import { FormEvent, useEffect, useState } from "react";

import { AdminRetentionPolicy } from "../../lib/staff-api";

export function RetentionPolicyControl({
  policy,
  onUpdate,
}: {
  policy: AdminRetentionPolicy;
  onUpdate: (
    expectedVersion: number,
    chatDays: number,
    emailDays: number,
    auditDays: number,
  ) => Promise<void>;
}) {
  const [chatDays, setChatDays] = useState(policy.chat_days);
  const [emailDays, setEmailDays] = useState(policy.email_days);
  const [auditDays, setAuditDays] = useState(policy.audit_days);

  useEffect(() => {
    setChatDays(policy.chat_days);
    setEmailDays(policy.email_days);
    setAuditDays(policy.audit_days);
  }, [policy]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void onUpdate(policy.version, chatDays, emailDays, auditDays);
  }

  return (
    <section aria-labelledby="retention-policy-heading">
      <h2 id="retention-policy-heading">Content retention</h2>
      <p>Product defaults are configurable for each organization.</p>
      <p>These settings are not a legal or compliance guarantee.</p>
      <form onSubmit={submit}>
        <label htmlFor="retention-chat-days">Chat content days</label>
        <input
          id="retention-chat-days"
          type="number"
          min={1}
          required
          value={chatDays}
          onChange={(event) => setChatDays(Number(event.target.value))}
        />
        <label htmlFor="retention-email-days">Email content days</label>
        <input
          id="retention-email-days"
          type="number"
          min={1}
          required
          value={emailDays}
          onChange={(event) => setEmailDays(Number(event.target.value))}
        />
        <label htmlFor="retention-audit-days">Audit event days</label>
        <input
          id="retention-audit-days"
          type="number"
          min={1}
          required
          value={auditDays}
          onChange={(event) => setAuditDays(Number(event.target.value))}
        />
        <button type="submit">Save retention policy</button>
      </form>
    </section>
  );
}
