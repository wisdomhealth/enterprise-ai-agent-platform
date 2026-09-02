"use client";

import { useState } from "react";

import { AdminKnowledgeStatus } from "../../lib/staff-api";
import { ConfirmDialog } from "./ConfirmDialog";
import { formatUtc, statusLabel } from "./format";

export function KnowledgeStatus({
  sources,
  onSync,
  onConfigure,
}: {
  sources: AdminKnowledgeStatus[];
  onSync: (sourceId: string) => Promise<void>;
  onConfigure: (rootFolderId: string, includeDescendants: boolean) => Promise<void>;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [scopePending, setScopePending] = useState(false);
  const [rootFolderId, setRootFolderId] = useState(sources[0]?.root_folder_id ?? "");
  const [includeDescendants, setIncludeDescendants] = useState(
    sources[0]?.include_descendants ?? true,
  );
  return (
    <section aria-labelledby="knowledge-status-heading">
      <h2 id="knowledge-status-heading">Knowledge sources</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setScopePending(true);
        }}
      >
        <label htmlFor="drive-root-folder">Drive root folder ID</label>
        <input
          id="drive-root-folder"
          required
          value={rootFolderId}
          onChange={(event) => setRootFolderId(event.target.value)}
        />
        <label>
          <input
            type="checkbox"
            checked={includeDescendants}
            onChange={(event) => setIncludeDescendants(event.target.checked)}
          />
          Include authorized subfolders
        </label>
        <button type="submit">Save Drive scope</button>
      </form>
      <ul>
        {sources.map((source) => (
          <li key={source.source_id}>
            <strong>{statusLabel(source.status)}</strong>
            <p>Authorized root: {source.root_folder_id}</p>
            <p>{source.descendant_count} allowed descendants · {source.isolated_files} isolated files</p>
            <p>Cursor: {source.cursor ?? "Not initialized"}</p>
            <p>Last success: {formatUtc(source.last_success_at)}</p>
            <p>Backlog {source.backlog} · Retry attempts {source.retry_count}</p>
            {source.recent_error_codes.map((code) => <p key={code}>Error: {code}</p>)}
            <button type="button" onClick={() => setPending(source.source_id)}>Sync Drive now</button>
          </li>
        ))}
      </ul>
      {pending ? (
        <ConfirmDialog
          label="Manual Drive sync confirmation"
          confirmLabel="Confirm sync"
          onConfirm={() => void onSync(pending).finally(() => setPending(null))}
          onCancel={() => setPending(null)}
        >
          <p>Start a durable Drive synchronization now?</p>
        </ConfirmDialog>
      ) : null}
      {scopePending ? (
        <ConfirmDialog
          label="Drive scope change confirmation"
          confirmLabel="Confirm scope change"
          onConfirm={() =>
            void onConfigure(rootFolderId, includeDescendants).finally(() =>
              setScopePending(false),
            )
          }
          onCancel={() => setScopePending(false)}
        >
          <p>Change the authorized read-only Drive scope?</p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
