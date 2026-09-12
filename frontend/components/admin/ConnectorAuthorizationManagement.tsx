"use client";

import { useState } from "react";

import { AdminConnectorAuthorization, ConnectorGrantAction } from "../../lib/staff-api";

const actions: ConnectorGrantAction[] = [
  "connector.create",
  "connector.reauthorize",
  "connector.revoke",
];

function connectorName(kind: AdminConnectorAuthorization["kind"]) {
  return kind === "DRIVE" ? "Google Drive" : "Gmail";
}

export function ConnectorAuthorizationManagement({
  authorizations,
  onSave,
}: {
  authorizations: AdminConnectorAuthorization[];
  onSave: (kind: AdminConnectorAuthorization["kind"], actions: ConnectorGrantAction[]) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Record<string, ConnectorGrantAction[]>>({});
  const [saving, setSaving] = useState<AdminConnectorAuthorization["kind"] | null>(null);
  const selectedFor = (authorization: AdminConnectorAuthorization) =>
    selected[authorization.kind] ?? authorization.actions.filter((item) => item.granted).map((item) => item.action);
  const save = async (authorization: AdminConnectorAuthorization, next: ConnectorGrantAction[]) => {
    setSaving(authorization.kind);
    try {
      await onSave(authorization.kind, next);
      setSelected((current) => ({ ...current, [authorization.kind]: next }));
    } finally {
      setSaving(null);
    }
  };

  return (
    <section aria-labelledby="connector-authorization-heading">
      <h1 id="connector-authorization-heading">Connector authorization</h1>
      <p>Manage the fixed permissions used to connect Google Drive and Gmail.</p>
      {authorizations.map((authorization) => {
        const current = selectedFor(authorization);
        const name = connectorName(authorization.kind);
        return (
          <fieldset key={authorization.kind} aria-label={`${name} connector permissions`}>
            <legend>{name}</legend>
            <p>{authorization.authorize_endpoint}</p>
            {actions.map((action) => (
              <label key={action}>
                <input
                  type="checkbox"
                  aria-label={`${name} ${action}`}
                  checked={current.includes(action)}
                  onChange={(event) => {
                    const next = event.target.checked
                      ? [...current, action]
                      : current.filter((item) => item !== action);
                    setSelected((existing) => ({ ...existing, [authorization.kind]: next }));
                  }}
                />
                {action} · {current.includes(action) ? "Granted" : "Missing"}
              </label>
            ))}
            <p>
              <button
                type="button"
                onClick={() => void save(authorization, actions)}
                disabled={saving === authorization.kind}
              >
                Grant recommended {authorization.kind === "DRIVE" ? "Drive" : "Gmail"} permissions
              </button>
              <button
                type="button"
                onClick={() => void save(authorization, current)}
                disabled={saving === authorization.kind}
              >
                Save {authorization.kind === "DRIVE" ? "Drive" : "Gmail"} permissions
              </button>
            </p>
          </fieldset>
        );
      })}
    </section>
  );
}
