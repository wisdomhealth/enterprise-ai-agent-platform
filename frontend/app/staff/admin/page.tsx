"use client";

import { useCallback, useEffect, useState } from "react";

import { ConnectorStatus } from "../../../components/admin/ConnectorStatus";
import { JobFailures } from "../../../components/admin/JobFailures";
import { KnowledgeStatus } from "../../../components/admin/KnowledgeStatus";
import { QualitySummary } from "../../../components/admin/QualitySummary";
import { UserManagement } from "../../../components/admin/UserManagement";
import {
  AdminFailedJob,
  AdminOperationsSummary,
  AdminUser,
  beginConnectorReauthorization,
  configureAdminDriveScope,
  getAdminOperationsSummary,
  inviteAdminUser,
  listAdminFailedJobs,
  listAdminUsers,
  requestAdminDriveSync,
  retryAdminJob,
  updateAdminUser,
} from "../../../lib/staff-api";

export default function AdminOperationsPage() {
  const [summary, setSummary] = useState<AdminOperationsSummary | null>(null);
  const [jobs, setJobs] = useState<AdminFailedJob[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextSummary, nextJobs, nextUsers] = await Promise.all([
        getAdminOperationsSummary(),
        listAdminFailedJobs(),
        listAdminUsers(),
      ]);
      setSummary(nextSummary);
      setJobs(nextJobs);
      setUsers(nextUsers);
      setNotice(null);
    } catch {
      setNotice("Administrator operations are unavailable or not authorized.");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  if (summary === null) return <p role="status">{notice ?? "Loading administrator operations…"}</p>;
  return (
    <section aria-labelledby="admin-operations-heading">
      <h1 id="admin-operations-heading">Administrator operations</h1>
      <p aria-live="polite">{notice}</p>
      <p>Queue depth {summary.jobs.queue_depth} · Failed {summary.jobs.failed} · Support backlog {summary.support.backlog}</p>
      <ConnectorStatus connectors={summary.connectors} onReauthorize={async (id) => {
        const result = await beginConnectorReauthorization(id);
        window.location.assign(result.authorization_url);
      }} />
      <KnowledgeStatus
        sources={summary.knowledge_sources}
        onSync={async (id) => { await requestAdminDriveSync(id); await refresh(); }}
        onConfigure={async (rootFolderId, includeDescendants) => {
          await configureAdminDriveScope(rootFolderId, includeDescendants);
          await refresh();
        }}
      />
      <JobFailures jobs={jobs} onRetry={async (id) => { await retryAdminJob(id); await refresh(); }} onReconcile={(id) => window.location.assign(`/staff/email/${id}`)} />
      <QualitySummary rag={summary.rag_quality} email={summary.email_quality} />
      <UserManagement users={users} onInvite={async (email, role) => { await inviteAdminUser(email, role); await refresh(); }} onUpdate={async (id, version, change) => { await updateAdminUser(id, version, change); await refresh(); }} />
    </section>
  );
}
