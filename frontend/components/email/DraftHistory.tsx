import { EmailDraft } from "../../lib/staff-api";

export function DraftHistory({ drafts, currentDraftId }: { drafts: EmailDraft[]; currentDraftId: string }) {
  return (
    <section aria-labelledby="draft-history-heading">
      <h2 id="draft-history-heading">Immutable draft history</h2>
      <ol>
        {drafts.map((draft) => (
          <li key={draft.id}>
            <details open={draft.id === currentDraftId}>
              <summary>
                Draft version {draft.version}{draft.id === currentDraftId ? " — current" : ""}
              </summary>
              <p>{draft.body}</p>
              <p>To: {draft.to.join(", ")}</p>
              <p>CC: {draft.cc.length ? draft.cc.join(", ") : "None"}</p>
              <p>Subject: {draft.subject}</p>
              <p>Thread: {draft.thread_id}</p>
              <p>Model {draft.model} · Prompt {draft.prompt_version}</p>
              {draft.reviewer_instruction ? <p>Reviewer instruction: {draft.reviewer_instruction}</p> : null}
              {draft.approval ? (
                <p>
                  Approved {draft.approval.approved_at}
                  {draft.approval.invalidated_at
                    ? ` · Invalidated ${draft.approval.invalidated_at}`
                    : " · Active approval"}
                </p>
              ) : null}
            </details>
          </li>
        ))}
      </ol>
    </section>
  );
}
