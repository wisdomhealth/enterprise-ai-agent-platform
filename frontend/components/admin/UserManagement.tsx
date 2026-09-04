"use client";

import { FormEvent, useState } from "react";

import { AdminUser } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { statusLabel } from "./format";

export function UserManagement({
  users,
  onInvite,
  onUpdate,
}: {
  users: AdminUser[];
  onInvite: (email: string, role: AdminUser["role"]) => Promise<void>;
  onUpdate: (
    userId: string,
    expectedVersion: number,
    change: Partial<Pick<AdminUser, "role" | "status">>,
  ) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<AdminUser["role"]>("MEMBER");
  const [pendingDisable, setPendingDisable] = useState<AdminUser | null>(null);

  function submit(event: FormEvent) {
    event.preventDefault();
    void onInvite(email, role).then(() => setEmail(""));
  }

  return (
    <section aria-labelledby="user-management-heading">
      <h2 id="user-management-heading">Users</h2>
      <form onSubmit={submit}>
        <label>Invitation email<input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label>Invitation role<select value={role} onChange={(event) => setRole(event.target.value as AdminUser["role"])}>
          <option value="MEMBER">Member</option><option value="REVIEWER">Reviewer</option><option value="ADMIN">Administrator</option>
        </select></label>
        <button type="submit">Invite user</button>
      </form>
      <ul>
        {users.map((user) => (
          <li key={user.id}>
            <strong>{user.email}</strong>{` · ${statusLabel(user.status)} · Version ${user.version}`}
            <label>Role for {user.email}<select value={user.role} disabled={user.status === "DISABLED"} onChange={(event) => void onUpdate(user.id, user.version, { role: event.target.value as AdminUser["role"] })}>
              <option value="MEMBER">Member</option><option value="REVIEWER">Reviewer</option><option value="ADMIN">Administrator</option>
            </select></label>
            {user.status !== "DISABLED" ? <button type="button" onClick={() => setPendingDisable(user)}>Disable {user.email}</button> : null}
          </li>
        ))}
      </ul>
      {pendingDisable ? (
        <ConfirmDialog
          label="Disable user confirmation"
          confirmLabel="Confirm disable"
          onConfirm={() =>
            void onUpdate(pendingDisable.id, pendingDisable.version, {
              status: "DISABLED",
            }).finally(() => setPendingDisable(null))
          }
          onCancel={() => setPendingDisable(null)}
        >
          <p>Disabling this user immediately revokes active sessions.</p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
