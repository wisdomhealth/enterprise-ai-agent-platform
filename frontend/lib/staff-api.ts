export type SupportState = "AI_ACTIVE" | "HANDOFF_REQUESTED" | "QUEUED" | "HUMAN_ACTIVE" | "RESOLVED";

export type SupportHandoff = {
  id: string;
  session_id: string;
  state: SupportState;
  trigger: string;
  assigned_user_id: string | null;
  version: number;
  last_customer_sequence: number;
};

export type SupportMessage = {
  sequence: number;
  actor: "CUSTOMER" | "AI" | "STAFF" | "SYSTEM";
  body: string;
  status: "PERSISTED";
  created_at: string;
  citations: InternalCitation[];
};

export type SupportConversation = SupportHandoff & {
  customer: { name: string | null; email: string | null };
  summary: string;
  tool_results: unknown[];
  messages: SupportMessage[];
};

export type InternalCitation = {
  title: string;
  section: string | null;
  page_number: number | null;
  chunk_id: string;
  document_version_id: string;
  internal_drive_link: string | null;
};

export type StaffKnowledgeAnswer = { text: string; citations: InternalCitation[] };

export type EmailState =
  | "INGESTED"
  | "DRAFTING"
  | "DRAFT_RETRY_WAIT"
  | "AWAITING_REVIEW"
  | "APPROVED"
  | "SEND_PENDING"
  | "SENDING"
  | "SENT"
  | "REJECTED"
  | "SEND_RETRY_WAIT"
  | "DELIVERY_UNKNOWN"
  | "FAILED_TERMINAL";

export type EmailQueueItem = {
  id: string;
  state: EmailState;
  version: number;
  sender: string;
  subject: string;
  received_at: string;
  category: "ACTION_REQUIRED" | "INFORMATIONAL" | "SPAM" | "UNKNOWN" | null;
  priority: "HIGH" | "NORMAL" | "LOW" | null;
};

export type EmailDraft = {
  id: string;
  version: number;
  body: string;
  to: string[];
  cc: string[];
  subject: string;
  thread_id: string;
  reviewer_instruction: string | null;
  model: string;
  prompt_version: string;
  created_at: string;
  citations: InternalCitation[];
  approval: { approved_at: string; invalidated_at: string | null } | null;
};

export type EmailAuditTransition = {
  id: string;
  from_state: EmailState;
  to_state: EmailState;
  action: string;
  reason_code: string | null;
  actor_type: "SYSTEM" | "STAFF";
  created_at: string;
};

export type EmailDeliveryAttempt = {
  id: string;
  attempt_number: number;
  outcome: "IN_PROGRESS" | "SENT" | "DEFINITIVE_FAILURE" | "UNKNOWN";
  error_code: string | null;
  started_at: string;
  completed_at: string | null;
};

export type EmailDelivery = {
  id: string;
  state: EmailState;
  version: number;
  deterministic_message_id: string;
  last_error_code: string | null;
  attempts: EmailDeliveryAttempt[];
};

export type EmailDetail = EmailQueueItem & {
  recipients: string[];
  body: string;
  reply_required: boolean | null;
  classification_rationale: string;
  current_draft_id: string | null;
  drafts: EmailDraft[];
  audit_transitions: EmailAuditTransition[];
  delivery: EmailDelivery | null;
};

export type EmailActionResult = {
  id: string;
  state: EmailState;
  version: number;
  current_draft_id: string;
};

export type EmailDeliveryResult = {
  id: string;
  work_item_id: string;
  state: EmailState;
  version: number;
};

export class StaffApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly handoff?: SupportHandoff,
  ) {
    super(message);
  }
}

function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const value = document.cookie
    .split("; ")
    .find((item) => item.startsWith("staff_csrf="))
    ?.split("=", 2)[1];
  return value ? decodeURIComponent(value) : null;
}

async function requestAt<T>(prefix: "/api/v1/staff" | "/api/v1/admin", path: string, init: RequestInit = {}): Promise<T> {
  const csrf = init.method === undefined || init.method === "GET" ? null : csrfToken();
  const response = await fetch(`${prefix}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(csrf === null ? {} : { "X-CSRF-Token": csrf }),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const detail = typeof payload === "object" && payload !== null ? (payload as { detail?: unknown }).detail : null;
    const handoff =
      path.startsWith("/support") &&
      typeof detail === "object" &&
      detail !== null &&
      "state" in detail &&
      "version" in detail
        ? ({
            id: "",
            session_id: "",
            trigger: "",
            assigned_user_id: null,
            last_customer_sequence: 0,
            ...(detail as Pick<SupportHandoff, "state" | "version">),
          } as SupportHandoff)
        : undefined;
    throw new StaffApiError(
      response.status === 409 && handoff
        ? "Already claimed"
        : response.status === 409
          ? "The resource changed"
          : "Unable to complete the staff action.",
      response.status,
      handoff,
    );
  }
  return (await response.json()) as T;
}

function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  return requestAt<T>("/api/v1/staff", path, init);
}

function adminRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  return requestAt<T>("/api/v1/admin", path, init);
}

export function listSupportQueue(): Promise<SupportHandoff[]> {
  return request<SupportHandoff[]>("/support/queue");
}

export function getSupportConversation(handoffId: string): Promise<SupportConversation> {
  return request<SupportConversation>(`/support/${handoffId}`);
}

function action(path: string, version: number): Promise<SupportHandoff> {
  return request<SupportHandoff>(path, { method: "POST", body: JSON.stringify({ version }) });
}

export function claimHandoff(handoffId: string, version: number): Promise<SupportHandoff> {
  return action(`/support/${handoffId}/claim`, version);
}

export function resolveHandoff(handoffId: string, version: number): Promise<SupportHandoff> {
  return action(`/support/${handoffId}/resolve`, version);
}

export function resumeAi(handoffId: string, version: number): Promise<SupportHandoff> {
  return action(`/support/${handoffId}/resume-ai`, version);
}

export function replyToHandoff(
  handoffId: string,
  version: number,
  body: string,
): Promise<{ sequence: number; actor: "STAFF"; body: string }> {
  return request(`/support/${handoffId}/reply`, {
    method: "POST",
    body: JSON.stringify({ version, body }),
  });
}

export function searchStaffKnowledge(question: string): Promise<StaffKnowledgeAnswer> {
  return request<StaffKnowledgeAnswer>("/knowledge/search", {
    method: "POST",
    body: JSON.stringify({ question }),
  });
}

export function listEmailQueue(states: EmailState[] = []): Promise<EmailQueueItem[]> {
  const query = new URLSearchParams(states.map((state) => ["state", state]));
  return request<EmailQueueItem[]>(`/email${query.size === 0 ? "" : `?${query}`}`);
}

export function getEmailDetail(workItemId: string): Promise<EmailDetail> {
  return request<EmailDetail>(`/email/${workItemId}`);
}

function idempotencyKey(operation: string, objectId: string): string {
  const random =
    typeof crypto === "undefined" || typeof crypto.randomUUID !== "function"
      ? Math.random().toString(36).slice(2)
      : crypto.randomUUID();
  return `${operation}:${objectId}:${random}`;
}

function emailAction(
  path: string,
  operation: string,
  objectId: string,
  method: "POST" | "PATCH",
  body: Record<string, unknown>,
): Promise<EmailActionResult> {
  return request<EmailActionResult>(path, {
    method,
    headers: { "Idempotency-Key": idempotencyKey(operation, objectId) },
    body: JSON.stringify(body),
  });
}

export function editEmailDraft(
  workItemId: string,
  expectedVersion: number,
  currentDraftId: string,
  patch: Pick<EmailDraft, "body" | "to" | "cc" | "subject" | "thread_id">,
): Promise<EmailActionResult> {
  return emailAction(`/email/${workItemId}/draft`, "draft-edit", workItemId, "PATCH", {
    expected_version: expectedVersion,
    current_draft_id: currentDraftId,
    ...patch,
  });
}

export function regenerateEmailDraft(
  workItemId: string,
  expectedVersion: number,
  currentDraftId: string,
  instruction: string,
): Promise<EmailActionResult> {
  return emailAction(`/email/${workItemId}/regenerate`, "draft-regenerate", workItemId, "POST", {
    expected_version: expectedVersion,
    current_draft_id: currentDraftId,
    instruction,
  });
}

export function approveEmail(
  workItemId: string,
  expectedVersion: number,
  currentDraftId: string,
): Promise<EmailActionResult> {
  return emailAction(`/email/${workItemId}/approve`, "email-approve", workItemId, "POST", {
    expected_version: expectedVersion,
    current_draft_id: currentDraftId,
  });
}

export function rejectEmail(
  workItemId: string,
  expectedVersion: number,
  currentDraftId: string,
): Promise<EmailActionResult> {
  return emailAction(`/email/${workItemId}/reject`, "email-reject", workItemId, "POST", {
    expected_version: expectedVersion,
    current_draft_id: currentDraftId,
  });
}

export function retryEmailDelivery(
  deliveryIntentId: string,
  expectedVersion: number,
): Promise<EmailDeliveryResult> {
  return request(`/email/delivery/${deliveryIntentId}/retry`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey("delivery-retry", deliveryIntentId) },
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function reconcileEmailDelivery(
  deliveryIntentId: string,
  expectedVersion: number,
): Promise<EmailDeliveryResult> {
  return request(`/email/delivery/${deliveryIntentId}/reconcile`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey("delivery-reconcile", deliveryIntentId) },
    body: JSON.stringify({ expected_version: expectedVersion, confirm_absent: false }),
  });
}

export type AdminConnectorStatus = {
  id: string;
  kind: "DRIVE" | "GMAIL";
  status: "ACTIVE" | "REAUTH_REQUIRED" | "ERROR";
  updated_at: string;
  requested_scopes: string[];
};

export type AdminKnowledgeStatus = {
  source_id: string;
  status: string;
  root_folder_id: string;
  include_descendants: boolean;
  descendant_count: number;
  cursor: string | null;
  last_success_at: string | null;
  backlog: number;
  isolated_files: number;
  retry_count: number;
  recent_error_codes: string[];
};

export type AdminQualityStatus = {
  completed_at: string;
  status: string;
  quality_score: number;
  latency_ms: number;
  estimated_cost: number;
};

export type AdminOperationsSummary = {
  generated_at: string;
  connectors: AdminConnectorStatus[];
  knowledge_sources: AdminKnowledgeStatus[];
  jobs: { queue_depth: number; failed: number };
  support: { backlog: number };
  email: { retry_wait: number; delivery_unknown: number };
  rag_quality: AdminQualityStatus | null;
  email_quality: AdminQualityStatus | null;
};

export type AdminFailedJob = {
  job_id: string;
  kind: string;
  state: "FAILED" | "RECONCILIATION";
  attempts: number;
  error_code: string | null;
  updated_at: string;
  action: "RETRY_DRIVE_SYNC" | "RETRY_EMAIL_DELIVERY" | "RECONCILE_GMAIL" | "NONE";
  action_resource_id: string | null;
};

export type AdminUser = {
  id: string;
  email: string;
  role: "ADMIN" | "REVIEWER" | "MEMBER";
  status: "INVITED" | "ACTIVE" | "DISABLED";
  version: number;
};

export function getAdminOperationsSummary(): Promise<AdminOperationsSummary> {
  return adminRequest<AdminOperationsSummary>("/operations/summary");
}

export function listAdminFailedJobs(): Promise<AdminFailedJob[]> {
  return adminRequest<AdminFailedJob[]>("/jobs/failed");
}

export function retryAdminJob(jobId: string): Promise<void> {
  return adminRequest(`/jobs/${jobId}/retry`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey("admin-job-retry", jobId) },
  });
}

export function beginConnectorReauthorization(
  connectorId: string,
): Promise<{ connector_id: string; authorization_url: string; requested_scopes: string[] }> {
  return adminRequest(`/connectors/${connectorId}/reauthorize`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey("connector-reauthorize", connectorId) },
  });
}

export function requestAdminDriveSync(sourceId: string): Promise<void> {
  return adminRequest(`/knowledge-sources/${sourceId}/sync`, { method: "POST" });
}

export function configureAdminDriveScope(
  rootFolderId: string,
  includeDescendants: boolean,
): Promise<void> {
  return adminRequest("/knowledge-sources/drive", {
    method: "PUT",
    body: JSON.stringify({
      root_folder_id: rootFolderId,
      include_descendants: includeDescendants,
    }),
  });
}

export function listAdminUsers(): Promise<AdminUser[]> {
  return adminRequest<AdminUser[]>("/users");
}

export function inviteAdminUser(
  email: string,
  role: AdminUser["role"],
): Promise<AdminUser> {
  return adminRequest<AdminUser>("/users/invitations", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey("admin-user-invite", email) },
    body: JSON.stringify({ email, role }),
  });
}

export function updateAdminUser(
  userId: string,
  expectedVersion: number,
  change: Partial<Pick<AdminUser, "role" | "status">>,
): Promise<AdminUser> {
  return adminRequest<AdminUser>(`/users/${userId}`, {
    method: "PATCH",
    headers: { "Idempotency-Key": idempotencyKey("admin-user-update", userId) },
    body: JSON.stringify({ expected_version: expectedVersion, ...change }),
  });
}
