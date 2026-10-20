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
  const [notice, setNotice] = useState<string | null>(null);
  const selectedFor = (authorization: AdminConnectorAuthorization) =>
    selected[authorization.kind] ?? authorization.actions.filter((item) => item.granted).map((item) => item.action);
  const save = async (authorization: AdminConnectorAuthorization, next: ConnectorGrantAction[]) => {
    setSaving(authorization.kind);
    setNotice(null);
    try {
      await onSave(authorization.kind, next);
      setSelected((current) => ({ ...current, [authorization.kind]: next }));
      setNotice(`${connectorName(authorization.kind)} staff permissions updated.`);
    } catch {
      setNotice(`Unable to update ${connectorName(authorization.kind)} staff permissions.`);
    } finally {
      setSaving(null);
    }
  };

  return (
    <section aria-labelledby="connector-authorization-heading">
      <h2 id="connector-authorization-heading">Connector authorization</h2>
      <p>These staff permissions control who may create, reauthorize, or revoke Google OAuth connections. They do not grant Email or Support application access.</p>
      <p className="staff-notice" aria-live="polite">{notice}</p>
      <div className="staff-summary-grid">{authorizations.map((authorization) => {
        const current = selectedFor(authorization);
        const name = connectorName(authorization.kind);
        return (
          <fieldset key={authorization.kind} aria-label={`${name} connector permissions`}>
            <legend>{name}</legend>
            <p><span className="staff-status">Google OAuth authorization</span></p>
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
            <div className="staff-actions">
              <button
                className="button-primary"
                type="button"
                onClick={() => void save(authorization, actions)}
                disabled={saving === authorization.kind}
              >
                {saving === authorization.kind ? "Saving…" : `Grant recommended ${authorization.kind === "DRIVE" ? "Drive" : "Gmail"} permissions`}
              </button>
              <button
                type="button"
                onClick={() => void save(authorization, current)}
                disabled={saving === authorization.kind}
              >
                {saving === authorization.kind ? "Saving…" : `Save ${authorization.kind === "DRIVE" ? "Drive" : "Gmail"} permissions`}
              </button>
            </div>
            <details className="staff-detail" open><summary>Technical endpoint</summary><p>{authorization.authorize_endpoint}</p></details>
          </fieldset>
        );
      })}</div>
    </section>
  );
}
