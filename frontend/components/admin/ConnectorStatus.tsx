"use client";

import { useState } from "react";

import { AdminConnectorStatus } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { formatUtc, statusLabel } from "./format";

const connectorKinds = ["DRIVE", "GMAIL"] as const;

function connectorName(kind: AdminConnectorStatus["kind"]) {
  return kind === "GMAIL" ? "Gmail" : "Google Drive";
}

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
        {connectorKinds.map((kind) => {
          const connector = connectors.find((candidate) => candidate.kind === kind);
          const name = connectorName(kind);
          return (
            <li key={kind}>
              <strong>{name}</strong>
              {connector ? (
                <>
                  {` · ${statusLabel(connector.status)} · Updated ${formatUtc(connector.updated_at)}`}
                  <p>Requested scopes: {connector.requested_scopes.join(", ")}</p>
                  {connector.status !== "ACTIVE" ? (
                    <button type="button" onClick={() => setPending(connector)}>
                      Reauthorize {name}
                    </button>
                  ) : null}
                </>
              ) : (
                <>
                  {" · Not connected"}
                  <p>
                    <button
                      type="button"
                      onClick={() => window.location.assign(`/api/v1/admin/connectors/${kind}/authorize`)}
                    >
                      Connect {name}
                    </button>
                  </p>
                </>
              )}
            </li>
          );
        })}
      </ul>
      {pending ? (
        <ConfirmDialog
          label={`Reauthorize ${connectorName(pending.kind)} confirmation`}
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
