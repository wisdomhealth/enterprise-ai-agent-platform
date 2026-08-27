export type ChatSSEEvent = {
  cursor: string;
  sequence: number;
  event: "message.validated" | "message.segment" | "session.state" | "error.safe";
  data: Record<string, unknown>;
};

export async function connectChatEvents(options: {
  sessionId: string;
  token: string;
  after?: string;
  onEvent: (event: ChatSSEEvent) => void;
  signal?: AbortSignal;
}): Promise<void> {
  let after = options.after ?? "0";
  while (!options.signal?.aborted) {
    const response = await fetch(
      `/api/v1/public/chat/sessions/${options.sessionId}/events?after=${encodeURIComponent(after)}`,
      {
        headers: { Accept: "text/event-stream", Authorization: `Bearer ${options.token}` },
        signal: options.signal,
      },
    );
    if (!response.ok || response.body === null) {
      throw new Error("We couldn't reconnect to the chat right now.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (!options.signal?.aborted) {
        const result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const event = parseEvent(frame);
          if (event === null) continue;
          after = event.cursor;
          options.onEvent(event);
        }
      }
    } finally {
      reader.releaseLock();
    }
    if (!options.signal?.aborted) await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function parseEvent(frame: string): ChatSSEEvent | null {
  const id = frame.match(/^id: ([\w:.-]+)$/m)?.[1];
  const event = frame.match(/^event: ([\w.]+)$/m)?.[1];
  const data = frame.match(/^data: (.+)$/m)?.[1];
  if (id === undefined || event === undefined || data === undefined) return null;
  if (![
    "message.validated",
    "message.segment",
    "session.state",
    "error.safe",
  ].includes(event)) return null;
  const parsed = JSON.parse(data) as Record<string, unknown>;
  const sequence = parsed.sequence;
  if (typeof sequence !== "number") return null;
  return {
    cursor: id,
    sequence,
    event: event as ChatSSEEvent["event"],
    data: parsed,
  };
}
