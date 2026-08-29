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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const csrf = init.method === undefined || init.method === "GET" ? null : csrfToken();
  const response = await fetch(`/api/v1/staff${path}`, {
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
      typeof detail === "object" && detail !== null && "state" in detail && "version" in detail
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
      response.status === 409 ? "Already claimed" : "Unable to complete the staff action.",
      response.status,
      handoff,
    );
  }
  return (await response.json()) as T;
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
