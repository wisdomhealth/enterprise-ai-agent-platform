import { PublicChatSession } from "../../lib/public-chat-api";

type MessageListProps = { messages: PublicChatSession["messages"] };

export function MessageList({ messages }: MessageListProps) {
  return (
    <ol aria-label="Chat messages">
      {messages.map((message) => (
        <li key={message.sequence} data-sequence={message.sequence}>
          <strong>{message.actor === "AI" ? "Assistant" : message.actor}</strong>
          <p>{message.body}</p>
        </li>
      ))}
    </ol>
  );
}
