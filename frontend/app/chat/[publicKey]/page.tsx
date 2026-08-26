import { ChatShell } from "../../../components/chat/ChatShell";

type PublicChatPageProps = { params: Promise<{ publicKey: string }> };

export default async function PublicChatPage({ params }: PublicChatPageProps) {
  const { publicKey } = await params;
  return <ChatShell publicKey={publicKey} />;
}
