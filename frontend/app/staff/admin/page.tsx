"use client";

import { useCallback, useEffect, useState } from "react";

import { ConnectorStatus } from "../../../components/admin/ConnectorStatus";
import { JobFailures } from "../../../components/admin/JobFailures";
import { KnowledgeStatus } from "../../../components/admin/KnowledgeStatus";
import { QualitySummary } from "../../../components/admin/QualitySummary";
import { RetentionPolicyControl } from "../../../components/admin/RetentionPolicyControl";
import { UserManagement } from "../../../components/admin/UserManagement";
import {
  AdminConnectorAuthorization,
  AdminFailedJob,
  AdminOperationsSummary,
  AdminRetentionPolicy,
  AdminUser,
  beginConnectorReauthorization,
  configureAdminDriveScope,
  getAdminConnectorAuthorizations,
  getAdminOperationsSummary,
  getAdminRetentionPolicy,
  inviteAdminUser,
  listAdminFailedJobs,
  listAdminUsers,
  requestAdminDriveSync,
  retryAdminJob,
  StaffApiError,
  updateAdminUser,
  updateAdminRetentionPolicy,
} from "../../../lib/staff-api";

export default function AdminOperationsPage() {
  const [summary, setSummary] = useState<AdminOperationsSummary | null>(null);
  const [authorizations, setAuthorizations] = useState<AdminConnectorAuthorization[]>([]);
  const [jobs, setJobs] = useState<AdminFailedJob[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [retentionPolicy, setRetentionPolicy] = useState<AdminRetentionPolicy | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextSummary, nextJobs, nextUsers, nextRetentionPolicy, nextAuthorizations] = await Promise.all([
        getAdminOperationsSummary(),
        listAdminFailedJobs(),
        listAdminUsers(),
        getAdminRetentionPolicy(),
        getAdminConnectorAuthorizations(),
      ]);
      setSummary(nextSummary);
      setJobs(nextJobs);
      setUsers(nextUsers);
      setRetentionPolicy(nextRetentionPolicy);
      setAuthorizations(nextAuthorizations);
      setNotice(null);
    } catch {
      setNotice("Administrator operations are unavailable or not authorized.");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  if (summary === null) return <p className="staff-notice" role="status">{notice ?? "Loading administrator operations…"}</p>;
  return (
    <div className="staff-stack" aria-labelledby="admin-operations-heading">
      <section>
        <div className="staff-toolbar">
          <div>
            <h2 id="admin-operations-heading">Operational status</h2>
            <p>Live workload and exception counts requiring administrator attention.</p>
          </div>
          <button type="button" onClick={() => void refresh()}>Refresh</button>
        </div>
        <p className="staff-notice" aria-live="polite">{notice}</p>
        <div className="staff-metrics">
          <div className="staff-metric"><strong>{summary.jobs.queue_depth}</strong><span>Queue depth</span></div>
          <div className="staff-metric"><strong>{summary.jobs.failed}</strong><span>Failed tasks</span></div>
          <div className="staff-metric"><strong>{summary.support.backlog}</strong><span>Support backlog</span></div>
        </div>
      </section>
      <div className="staff-page-grid">
        <div className="staff-stack">
          <ConnectorStatus connectors={summary.connectors} authorizations={authorizations} onReauthorize={async (id) => {
            try {
              const result = await beginConnectorReauthorization(id);
              window.location.assign(result.authorization_url);
            } catch (error) {
              setNotice(
                error instanceof StaffApiError
                  ? error.message
                  : "Unable to start Google Drive reauthorization.",
              );
            }
          }} />
          <KnowledgeStatus
            sources={summary.knowledge_sources}
            onSync={async (id) => { await requestAdminDriveSync(id); await refresh(); }}
            onConfigure={async (rootFolderId, includeDescendants) => {
          try {
            await configureAdminDriveScope(rootFolderId, includeDescendants);
            await refresh();
          } catch (error) {
            if (
              error instanceof StaffApiError &&
              error.code === "GOOGLE_DRIVE_REAUTH_REQUIRED"
            ) {
              await refresh();
              setNotice(error.message);
              return;
            }
            setNotice(
              error instanceof StaffApiError
                ? error.message
                : "Unable to save the Google Drive scope.",
            );
          }
            }}
          />
          <JobFailures jobs={jobs} onRetry={async (id) => { await retryAdminJob(id); await refresh(); }} onReconcile={(id) => window.location.assign(`/staff/email/${id}`)} />
        </div>
        <div className="staff-stack">
          <UserManagement users={users} onInvite={async (email, role) => { await inviteAdminUser(email, role); await refresh(); }} onUpdate={async (id, version, change) => { await updateAdminUser(id, version, change); await refresh(); }} />
          <details className="staff-card staff-detail">
            <summary>Quality, retention, and diagnostics</summary>
            <QualitySummary rag={summary.rag_quality} email={summary.email_quality} />
            {retentionPolicy ? (
              <RetentionPolicyControl
                policy={retentionPolicy}
                onUpdate={async (version, chatDays, emailDays, auditDays) => {
                  await updateAdminRetentionPolicy(version, chatDays, emailDays, auditDays);
                  await refresh();
                }}
              />
            ) : null}
          </details>
        </div>
      </div>
    </div>
  );
}
