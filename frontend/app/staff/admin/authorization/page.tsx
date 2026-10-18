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
} from "../../../../lib/staff-api";

export default function ConnectorAuthorizationPage() {
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

  if (authorizations === null) return <p role="status">{notice ?? "Loading connector authorization…"}</p>;
  const selectedUser = users.find((user) => user.id === selectedUserId);
  return (<>
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
    <ConnectorAuthorizationManagement
      authorizations={authorizations}
      onSave={async (kind: "DRIVE" | "GMAIL", actions: ConnectorGrantAction[]) => {
        const current = authorizations.find((authorization) => authorization.kind === kind);
        if (!current) return;
        await updateAdminConnectorAuthorization(kind, current.staff_user_id, actions);
        await refresh();
      }}
    />
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
    />
  </>);
}
