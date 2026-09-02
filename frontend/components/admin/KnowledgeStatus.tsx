"use client";

import { useState } from "react";

import { AdminKnowledgeStatus } from "../../lib/staff-api";
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
        <div role="dialog" aria-label="Manual Drive sync confirmation">
          <p>Start a durable Drive synchronization now?</p>
          <button type="button" onClick={() => void onSync(pending).finally(() => setPending(null))}>
            Confirm sync
          </button>
          <button type="button" onClick={() => setPending(null)}>Cancel</button>
        </div>
      ) : null}
      {scopePending ? (
        <div role="dialog" aria-label="Drive scope change confirmation">
          <p>Change the authorized read-only Drive scope?</p>
          <button
            type="button"
            onClick={() =>
              void onConfigure(rootFolderId, includeDescendants).finally(() =>
                setScopePending(false),
              )
            }
          >
            Confirm scope change
          </button>
          <button type="button" onClick={() => setScopePending(false)}>
            Cancel
          </button>
        </div>
      ) : null}
    </section>
  );
}
