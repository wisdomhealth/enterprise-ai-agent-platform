"use client";

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
  return (
    <section aria-labelledby="application-access-heading">
      <h1 id="application-access-heading">Application Access</h1>
      <p>
        Application access opens Email or Support. Knowledge Base assignments and each
        operation&apos;s permission remain separately required.
      </p>
      <label>
        Employee
        <select value={selectedUserId} onChange={(event) => onSelectUser(event.target.value)}>
          {users.map((user) => <option key={user.id} value={user.id}>{user.email}</option>)}
        </select>
      </label>
      {selectedUser ? <p>Role: {selectedUser.role} · Account: {selectedUser.status}</p> : null}
      {access.map((item) => (
        <article key={item.capability}>
          <h2>{item.capability === "email" ? "Email Assistant" : "Support Console"}</h2>
          <p>Application grant: {item.grant_present ? "Granted" : "Missing"}</p>
          <p>Role and account eligible: {item.role_status_eligible ? "Yes" : "No"}</p>
          <p>
            Reviewable knowledge bases: {item.reviewable_knowledge_base_ids.length === 0
              ? "None"
              : item.reviewable_knowledge_base_ids.join(", ")}
          </p>
          <p>Effective access: {item.effective_allowed ? "Allowed" : "Denied"}</p>
          <button type="button" onClick={() => void onChange(item.capability, !item.grant_present)}>
            {item.grant_present ? "Revoke" : "Grant"} {item.capability} access
          </button>
        </article>
      ))}
      <section aria-labelledby="knowledge-assignments-heading">
        <h2 id="knowledge-assignments-heading">Knowledge Base Assignments</h2>
        <p>
          Effective assignments are shown above from existing ResourceGrant records. This change
          does not add a second grant store or a knowledge-assignment editor.
        </p>
      </section>
      <section aria-labelledby="google-authorization-heading">
        <h2 id="google-authorization-heading">Google Connector Authorization</h2>
        <p>Google OAuth access is managed separately from application access.</p>
      </section>
    </section>
  );
}
