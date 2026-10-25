"use client";

import { useCallback, useEffect, useState } from "react";

import { ConnectorAuthorizationManagement } from "../../../../components/admin/ConnectorAuthorizationManagement";
import { ApplicationAccessManagement } from "../../../../components/admin/ApplicationAccessManagement";
import { GmailPushManagement } from "../../../../components/admin/GmailPushManagement";
import {
  AdminApplicationAccess,
  AdminConnectorAuthorization,
  AdminUser,
  AdminGmailSyncState,
  ConnectorGrantAction,
  getAdminConnectorAuthorizations,
  getAdminApplicationAccess,
  getAdminGmailSyncStates,
  listAdminUsers,
  updateAdminApplicationAccess,
  updateAdminConnectorAuthorization,
  startAdminGmailWatch,
  renewAdminGmailWatch,
  stopAdminGmailWatch,
  requestAdminGmailManualSync,
  continueAdminGmailRecovery,
  acceptAdminLimitedGmailRecovery,
  resumeAdminGmailRecovery,
} from "../../../../lib/staff-api";

export default function ConnectorAuthorizationPage() {
  const [tab, setTab] = useState<"applications" | "connections" | "gmail">("applications");
  const [authorizations, setAuthorizations] = useState<AdminConnectorAuthorization[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [applicationAccess, setApplicationAccess] = useState<AdminApplicationAccess[]>([]);
  const [gmailStates, setGmailStates] = useState<AdminGmailSyncState[]>([]);
  const refresh = useCallback(async () => {
    try {
      const [nextAuthorizations, nextUsers, nextGmailStates] = await Promise.all([
        getAdminConnectorAuthorizations(),
        listAdminUsers(),
        getAdminGmailSyncStates(),
      ]);
      setAuthorizations(nextAuthorizations);
      setUsers(nextUsers);
      setGmailStates(nextGmailStates);
      const targetId = selectedUserId || nextUsers[0]?.id || "";
      setSelectedUserId(targetId);
      setApplicationAccess(targetId ? await getAdminApplicationAccess(targetId) : []);
      setNotice(null);
    } catch {
      setNotice("Connector authorization is unavailable or not authorized.");
    }
  }, [selectedUserId]);
  useEffect(() => { void refresh(); }, [refresh]);

  if (authorizations === null) return <p className="staff-notice" role="status">{notice ?? "Loading connector authorization…"}</p>;
  const selectedUser = users.find((user) => user.id === selectedUserId);
  return (
    <div className="staff-stack">
      <section>
        <div className="staff-toolbar">
          <div>
            <h2>Authorization management</h2>
            <p>Application permissions and Google OAuth connections are separate controls.</p>
          </div>
          <button type="button" onClick={() => void refresh()}>Refresh</button>
        </div>
        <p className="staff-notice" aria-live="polite">{notice}</p>
        <div className="staff-tabs" role="tablist" aria-label="Authorization sections">
          <button type="button" role="tab" aria-selected={tab === "applications"} onClick={() => setTab("applications")}>Application Access</button>
          <button type="button" role="tab" aria-selected={tab === "connections"} onClick={() => setTab("connections")}>Google Connections</button>
          <button type="button" role="tab" aria-selected={tab === "gmail"} onClick={() => setTab("gmail")}>Gmail Sync</button>
        </div>
        <div role="tabpanel">
          {tab === "applications" ? (
            <ApplicationAccessManagement
              users={users}
              selectedUserId={selectedUserId}
              access={applicationAccess}
              onSelectUser={(userId) => {
                setSelectedUserId(userId);
                void getAdminApplicationAccess(userId).then(setApplicationAccess);
              }}
              onChange={async (capability, granted) => {
                if (!selectedUser) return;
                await updateAdminApplicationAccess(selectedUser, capability, granted);
                await refresh();
              }}
            />
          ) : null}
          {tab === "connections" ? (
            <ConnectorAuthorizationManagement
              authorizations={authorizations}
              onSave={async (kind: "DRIVE" | "GMAIL", actions: ConnectorGrantAction[]) => {
                const current = authorizations.find((authorization) => authorization.kind === kind);
                if (!current) return;
                await updateAdminConnectorAuthorization(kind, current.staff_user_id, actions);
                await refresh();
              }}
            />
          ) : null}
          {tab === "gmail" ? (
            <GmailPushManagement
              states={gmailStates}
              onWatch={async (state, action) => {
                if (action === "start") await startAdminGmailWatch(state.connector_id);
                if (action === "renew") await renewAdminGmailWatch(state.connector_id);
                if (action === "stop") await stopAdminGmailWatch(state.connector_id);
                await refresh();
              }}
              onManualSync={async (state, reason) => {
                await requestAdminGmailManualSync(state, reason);
                await refresh();
              }}
              onContinueRecovery={async (state, additionalMessages, reason) => {
                await continueAdminGmailRecovery(state, additionalMessages, reason);
                await refresh();
              }}
              onAcceptLimitedRecovery={async (state, reason) => {
                await acceptAdminLimitedGmailRecovery(state, reason);
                await refresh();
              }}
              onResumeRecovery={async (state, reason) => {
                await resumeAdminGmailRecovery(state, reason);
                await refresh();
              }}
            />
          ) : null}
        </div>
      </section>
    </div>
  );
}
