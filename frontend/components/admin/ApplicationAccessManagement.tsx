"use client";

import { useMemo, useState } from "react";

import { AdminApplicationAccess, AdminUser } from "../../lib/staff-api";

export function ApplicationAccessManagement({
  users,
  selectedUserId,
  access,
  onSelectUser,
  onChange,
}: {
  users: AdminUser[];
  selectedUserId: string;
  access: AdminApplicationAccess[];
  onSelectUser: (userId: string) => void;
  onChange: (capability: "email" | "support", granted: boolean) => Promise<void>;
}) {
  const selectedUser = users.find((user) => user.id === selectedUserId);
  const [busyCapability, setBusyCapability] = useState<"email" | "support" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const knowledgeBases = useMemo(
    () => [...new Set(access.flatMap((item) => item.reviewable_knowledge_base_ids))],
    [access],
  );
  async function change(capability: "email" | "support", granted: boolean) {
    if (busyCapability) return;
    setBusyCapability(capability);
    setNotice(null);
    try {
      await onChange(capability, granted);
      setNotice(`${capability === "email" ? "Email Assistant" : "Support Console"} access updated.`);
    } catch {
      setNotice("Unable to update application access.");
    } finally {
      setBusyCapability(null);
    }
  }
  return (
    <section aria-labelledby="application-access-heading">
      <h2 id="application-access-heading">Application Access</h2>
      <p>
        Application access opens Email or Support. Knowledge Base assignments and each
        operation&apos;s permission remain separately required.
      </p>
      <label className="staff-field-wide">
        Employee
        <select value={selectedUserId} onChange={(event) => onSelectUser(event.target.value)}>
          {users.map((user) => <option key={user.id} value={user.id}>{user.email}</option>)}
        </select>
      </label>
      {selectedUser ? <div className="staff-summary-grid"><div><strong>{selectedUser.email}</strong><p>Role: {selectedUser.role}</p></div><div><strong>Account status</strong><p><span className="staff-status" data-tone={selectedUser.status === "ACTIVE" ? "success" : "warning"}>{selectedUser.status}</span></p></div></div> : null}
      <p className="staff-notice" aria-live="polite">{notice}</p>
      <div className="staff-list">
        {access.map((item) => (
          <article key={item.capability}>
            <div className="staff-list-row"><div><h3>{item.capability === "email" ? "Email Assistant" : "Support Console"}</h3><p>Application grant: <strong>{item.grant_present ? "Granted" : "Missing"}</strong></p></div><span className="staff-status" data-tone={item.effective_allowed ? "success" : "danger"}>{item.effective_allowed ? "Effective access allowed" : "Effective access denied"}</span></div>
            {!item.effective_allowed ? <p>Access remains unavailable when the account, role, application grant, or required knowledge permissions are missing.</p> : null}
            <details className="staff-detail"><summary>Eligibility details</summary><p>Role and account eligible: {item.role_status_eligible ? "Yes" : "No"}</p><p>Reviewable knowledge bases: {item.reviewable_knowledge_base_ids.length === 0 ? "None" : item.reviewable_knowledge_base_ids.join(", ")}</p></details>
            <div className="staff-actions"><button type="button" className={item.grant_present ? "button-danger" : "button-primary"} disabled={busyCapability !== null} onClick={() => void change(item.capability, !item.grant_present)}>{busyCapability === item.capability ? "Saving…" : `${item.grant_present ? "Revoke" : "Grant"} ${item.capability} access`}</button></div>
          </article>
        ))}
      </div>
      <section aria-labelledby="knowledge-assignments-heading">
        <h2 id="knowledge-assignments-heading">Knowledge Base Assignments</h2>
        <p>{knowledgeBases.length ? knowledgeBases.join(", ") : "No reviewable knowledge bases are assigned."}</p>
        <p>Assignments come from existing resource grants. Missing assignments can prevent effective access even when the application grant is present.</p>
      </section>
    </section>
  );
}
