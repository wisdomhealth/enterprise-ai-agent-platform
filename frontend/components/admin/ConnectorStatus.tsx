"use client";

import { useState } from "react";

import { AdminConnectorStatus } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { formatUtc, statusLabel } from "./format";

export function ConnectorStatus({
  connectors,
  onReauthorize,
}: {
  connectors: AdminConnectorStatus[];
  onReauthorize: (connectorId: string) => Promise<void>;
}) {
  const [pending, setPending] = useState<AdminConnectorStatus | null>(null);
  return (
    <section aria-labelledby="connector-status-heading">
      <h2 id="connector-status-heading">Google connections</h2>
      <ul>
        {connectors.map((connector) => (
          <li key={connector.id}>
            <strong>{connector.kind === "GMAIL" ? "Gmail" : "Google Drive"}</strong>
            {` · ${statusLabel(connector.status)} · Updated ${formatUtc(connector.updated_at)}`}
            <p>Requested scopes: {connector.requested_scopes.join(", ")}</p>
            {connector.status !== "ACTIVE" ? (
              <button type="button" onClick={() => setPending(connector)}>
                Reauthorize {connector.kind === "GMAIL" ? "Gmail" : "Google Drive"}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {pending ? (
        <ConfirmDialog
          label={`Reauthorize ${pending.kind === "GMAIL" ? "Gmail" : "Google Drive"} confirmation`}
          confirmLabel="Continue to Google"
          onConfirm={() => {
            void onReauthorize(pending.id).finally(() => setPending(null));
          }}
          onCancel={() => setPending(null)}
        >
          <p>Continue to Google to rotate this connector&apos;s authorization.</p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
