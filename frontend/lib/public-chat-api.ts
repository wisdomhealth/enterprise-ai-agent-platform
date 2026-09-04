export type ConversationState =
  | "AI_ACTIVE"
  | "HANDOFF_REQUESTED"
  | "QUEUED"
  | "HUMAN_ACTIVE"
  | "RESOLVED";

export type PublicChatSession = {
  id: string;
  state: ConversationState;
  version: number;
  customer_name: string | null;
  customer_email: string | null;
  created_at: string;
  messages: Array<{
    sequence: number;
    actor: "CUSTOMER" | "AI" | "STAFF" | "SYSTEM";
    body: string;
    status: "PERSISTED";
    created_at: string;
  }>;
};

export type PublicChatStart = {
  session: PublicChatSession;
  credential: { token: string; expires_at: string };
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "";

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}/api/v1/public/chat${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    throw new Error("We couldn't start a chat right now. Please try again.");
  }
  return (await response.json()) as T;
}

export function startPublicChat(input: {
  publicKey: string;
  customerName?: string;
  customerEmail?: string;
}): Promise<PublicChatStart> {
  return request<PublicChatStart>("/sessions", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      public_key: input.publicKey,
      customer_name: input.customerName || null,
      customer_email: input.customerEmail || null,
    }),
  });
}

export function sendPublicChatMessage(input: {
  sessionId: string;
  token: string;
  body: string;
  idempotencyKey?: string;
}): Promise<PublicChatSession["messages"][number]> {
  return request<PublicChatSession["messages"][number]>(
    `/sessions/${input.sessionId}/messages`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${input.token}`,
        "Idempotency-Key": input.idempotencyKey ?? crypto.randomUUID(),
      },
      body: JSON.stringify({ body: input.body }),
    },
  );
}

export function requestPublicHandoff(input: {
  sessionId: string;
  token: string;
  contactName?: string;
  contactEmail?: string;
}): Promise<{ state: "QUEUED" | "HUMAN_ACTIVE" }> {
  return request(`/sessions/${input.sessionId}/handoff`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${input.token}`,
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify({
      contact_name: input.contactName || null,
      contact_email: input.contactEmail || null,
    }),
  });
}
