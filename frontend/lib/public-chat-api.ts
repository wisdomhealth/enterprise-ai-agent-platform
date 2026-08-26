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

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/public/chat${path}`, {
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
    body: JSON.stringify({
      public_key: input.publicKey,
      customer_name: input.customerName || null,
      customer_email: input.customerEmail || null,
    }),
  });
}
