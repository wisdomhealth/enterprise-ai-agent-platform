"use client";

import { useState } from "react";

import { AdminConnectorAuthorization, AdminConnectorStatus } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { formatUtc, statusLabel } from "./format";

const connectorKinds = ["DRIVE", "GMAIL"] as const;

function connectorName(kind: AdminConnectorStatus["kind"]) {
  return kind === "GMAIL" ? "Gmail" : "Google Drive";
}

export function ConnectorStatus({
  connectors,
  authorizations,
  onReauthorize,
}: {
  connectors: AdminConnectorStatus[];
  authorizations?: AdminConnectorAuthorization[];
  onReauthorize: (connectorId: string) => Promise<void>;
}) {
  const [pending, setPending] = useState<AdminConnectorStatus | null>(null);
  return (
    <section aria-labelledby="connector-status-heading">
      <h2 id="connector-status-heading">Google connections</h2>
      <ul>
        {connectorKinds.map((kind) => {
          const connector = connectors.find((candidate) => candidate.kind === kind);
          const createGranted = authorizations === undefined || authorizations
            .find((authorization) => authorization.kind === kind)
            ?.actions.find((action) => action.action === "connector.create")?.granted === true;
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
                  {createGranted ? " · Not connected" : " · Authorization required"}
                  <p>
                    {createGranted ? (
                      <button
                        type="button"
                        onClick={() => window.location.assign(`/api/v1/admin/connectors/${kind}/authorize`)}
                      >
                        Connect {name}
                      </button>
                    ) : (
                      <>
                        <span>Missing permission: connector.create</span>{" "}
                        <button type="button" onClick={() => window.location.assign("/staff/admin/authorization")}>
                          Manage authorization
                        </button>
                      </>
                    )}
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
