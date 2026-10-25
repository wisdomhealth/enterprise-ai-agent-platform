"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ConnectorStatus } from "../../../components/admin/ConnectorStatus";
import { JobFailures } from "../../../components/admin/JobFailures";
import { KnowledgeStatus } from "../../../components/admin/KnowledgeStatus";
import { QualitySummary } from "../../../components/admin/QualitySummary";
import { RetentionPolicyControl } from "../../../components/admin/RetentionPolicyControl";
import { UserManagement } from "../../../components/admin/UserManagement";
import {
  AdminConnectorAuthorization,
  AdminFailedJobQuery,
  AdminFailedJobsPage,
  AdminGmailSyncState,
  AdminOperationsSummary,
  AdminRetentionPolicy,
  AdminUser,
  beginConnectorReauthorization,
  configureAdminDriveScope,
  getAdminConnectorAuthorizations,
  getAdminGmailSyncStates,
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

const emptyJobsPage: AdminFailedJobsPage = {
  items: [],
  page: 1,
  page_size: 20,
  total: 0,
  count: 0,
  total_pages: 0,
};

type FailedJobFilters = Omit<AdminFailedJobQuery, "page" | "pageSize">;

export default function AdminOperationsPage() {
  const [summary, setSummary] = useState<AdminOperationsSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [authorizations, setAuthorizations] = useState<AdminConnectorAuthorization[]>([]);
  const [gmailStates, setGmailStates] = useState<AdminGmailSyncState[]>([]);
  const [retentionPolicy, setRetentionPolicy] = useState<AdminRetentionPolicy | null>(null);
  const [auxiliaryError, setAuxiliaryError] = useState<string | null>(null);
  const [jobsPage, setJobsPage] = useState<AdminFailedJobsPage>(emptyJobsPage);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [jobFilters, setJobFilters] = useState<FailedJobFilters>({});
  const jobsRequest = useRef(0);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(true);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadSummary = useCallback(async () => {
    setSummaryLoading(true);
    try {
      setSummary(await getAdminOperationsSummary());
      setSummaryError(null);
    } catch {
      setSummaryError("Operational status is unavailable or not authorized.");
    } finally {
      setSummaryLoading(false);
    }
  }, []);

  const loadAuxiliary = useCallback(async () => {
    try {
      const [nextRetentionPolicy, nextAuthorizations, nextGmailStates] = await Promise.all([
        getAdminRetentionPolicy(),
        getAdminConnectorAuthorizations(),
        getAdminGmailSyncStates(),
      ]);
      setRetentionPolicy(nextRetentionPolicy);
      setAuthorizations(nextAuthorizations);
      setGmailStates(nextGmailStates);
      setAuxiliaryError(null);
    } catch {
      setAuxiliaryError("Connector or retention details are unavailable.");
    }
  }, []);

  const loadJobs = useCallback(async (query: AdminFailedJobQuery) => {
    const request = ++jobsRequest.current;
    setJobsLoading(true);
    try {
      const nextPage = await listAdminFailedJobs(query);
      if (request !== jobsRequest.current) return;
      setJobsPage(nextPage);
      setJobsError(null);
    } catch {
      if (request !== jobsRequest.current) return;
      setJobsError("Failed and uncertain work is unavailable.");
    } finally {
      if (request === jobsRequest.current) setJobsLoading(false);
    }
  }, []);

  const loadUsers = useCallback(async () => {
    setUsersLoading(true);
    try {
      setUsers(await listAdminUsers());
      setUsersError(null);
    } catch {
      setUsersError("Users are unavailable or not authorized.");
    } finally {
      setUsersLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSummary();
    void loadAuxiliary();
    void loadJobs({ page: 1, pageSize: 20 });
    void loadUsers();
  }, [loadAuxiliary, loadJobs, loadSummary, loadUsers]);

  const refreshAll = () => {
    void loadSummary();
    void loadAuxiliary();
    void loadJobs({ ...jobFilters, page: jobsPage.page, pageSize: 20 });
    void loadUsers();
  };

  return (
    <div className="staff-stack" aria-labelledby="admin-operations-heading">
      <section>
        <div className="staff-toolbar">
          <div>
            <h2 id="admin-operations-heading">Operational status</h2>
            <p>Live workload and exception counts requiring administrator attention.</p>
          </div>
          <button type="button" disabled={summaryLoading || jobsLoading || usersLoading} onClick={refreshAll}>Refresh</button>
        </div>
        <p className="staff-notice" aria-live="polite">{notice}</p>
        {summaryLoading ? <p role="status">Loading operational status…</p> : null}
        {summaryError ? <p role="alert">{summaryError}</p> : null}
        {summary ? (
          <div className="staff-metrics">
            <div className="staff-metric"><strong>{summary.jobs.queue_depth}</strong><span>Queue depth</span></div>
            <div className="staff-metric"><strong>{summary.jobs.failed}</strong><span>Failed tasks</span></div>
            <div className="staff-metric"><strong>{summary.support.backlog}</strong><span>Support backlog</span></div>
          </div>
        ) : null}
      </section>
      <div className="staff-page-grid">
        <div className="staff-stack">
          {auxiliaryError ? <p role="alert">{auxiliaryError}</p> : null}
          {summary ? (
            <>
              <ConnectorStatus connectors={summary.connectors} authorizations={authorizations} reauthorizationRequiredConnectorIds={new Set(gmailStates.filter((state) => state.last_error_code === "GMAIL_REAUTH_REQUIRED").map((state) => state.connector_id))} onReauthorize={async (id) => {
                try {
                  const result = await beginConnectorReauthorization(id);
                  window.location.assign(result.authorization_url);
                } catch (error) {
                  setNotice(error instanceof StaffApiError ? error.message : "Unable to start Google Drive reauthorization.");
                }
              }} />
              <KnowledgeStatus
                sources={summary.knowledge_sources}
                onSync={async (id) => { await requestAdminDriveSync(id); await loadSummary(); }}
                onConfigure={async (rootFolderId, includeDescendants) => {
                  try {
                    await configureAdminDriveScope(rootFolderId, includeDescendants);
                    await loadSummary();
                  } catch (error) {
                    if (error instanceof StaffApiError && error.code === "GOOGLE_DRIVE_REAUTH_REQUIRED") {
                      await loadSummary();
                      setNotice(error.message);
                      return;
                    }
                    setNotice(error instanceof StaffApiError ? error.message : "Unable to save the Google Drive scope.");
                  }
                }}
              />
            </>
          ) : null}
          <JobFailures
            jobs={jobsPage.items}
            pagination={jobsPage}
            loading={jobsLoading}
            error={jobsError}
            onApplyFilters={(filters) => {
              setJobFilters(filters);
              void loadJobs({ ...filters, page: 1, pageSize: 20 });
            }}
            onPageChange={(page) => void loadJobs({ ...jobFilters, page, pageSize: 20 })}
            onRetry={async (id) => {
              await retryAdminJob(id);
              await loadJobs({ ...jobFilters, page: jobsPage.page, pageSize: 20 });
              await loadSummary();
            }}
            onReconcile={(id) => window.location.assign(`/staff/email/${id}`)}
          />
        </div>
        <div className="staff-stack">
          {usersLoading ? <p role="status">Loading users…</p> : null}
          {usersError ? <p role="alert">{usersError}</p> : null}
          {!usersLoading && !usersError ? (
            <UserManagement
              users={users}
              onInvite={async (email, role) => { await inviteAdminUser(email, role); await loadUsers(); }}
              onUpdate={async (id, version, change) => { await updateAdminUser(id, version, change); await loadUsers(); }}
            />
          ) : null}
          {summary ? (
            <details className="staff-card staff-detail">
              <summary>Quality, retention, and diagnostics</summary>
              <QualitySummary rag={summary.rag_quality} email={summary.email_quality} />
              {retentionPolicy ? (
                <RetentionPolicyControl
                  policy={retentionPolicy}
                  onUpdate={async (version, chatDays, emailDays, auditDays) => {
                    await updateAdminRetentionPolicy(version, chatDays, emailDays, auditDays);
                    await loadAuxiliary();
                  }}
                />
              ) : null}
            </details>
          ) : null}
        </div>
      </div>
    </div>
  );
}
