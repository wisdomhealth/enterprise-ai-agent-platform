"use client";

import { useCallback, useEffect, useState } from "react";

import { ConnectorAuthorizationManagement } from "../../../../components/admin/ConnectorAuthorizationManagement";
import {
  AdminConnectorAuthorization,
  ConnectorGrantAction,
  getAdminConnectorAuthorizations,
  updateAdminConnectorAuthorization,
} from "../../../../lib/staff-api";

export default function ConnectorAuthorizationPage() {
  const [authorizations, setAuthorizations] = useState<AdminConnectorAuthorization[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      setAuthorizations(await getAdminConnectorAuthorizations());
      setNotice(null);
    } catch {
      setNotice("Connector authorization is unavailable or not authorized.");
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  if (authorizations === null) return <p role="status">{notice ?? "Loading connector authorization…"}</p>;
  return (
    <ConnectorAuthorizationManagement
      authorizations={authorizations}
      onSave={async (kind: "DRIVE" | "GMAIL", actions: ConnectorGrantAction[]) => {
        const current = authorizations.find((authorization) => authorization.kind === kind);
        if (!current) return;
        await updateAdminConnectorAuthorization(kind, current.staff_user_id, actions);
        await refresh();
      }}
    />
  );
}
