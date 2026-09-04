# Retrieval operations

The first release uses `text-embedding-3-small` (1536 dimensions), pgvector
cosine search, PostgreSQL English full-text search, and reciprocal-rank fusion
(RRF, `k=60`). Configure `OPENAI_API_KEY` only through the deployment secret
manager. `EMBEDDING_MODEL` defaults to `text-embedding-3-small`; changing it
requires a new migration and a full re-embedding, not an in-place toggle.

Each candidate branch enforces organization, knowledge-base resource grant,
authorized active Drive source, current-version, and retrievable-state filters
inside its SQL query before ranking. A revoked version is never eligible. The
Reranker protocol is deliberately disabled by default (`RERANKER_ENABLED=false`)
until the fixed evaluation set demonstrates a quality improvement that justifies
its latency and cost.

## Publishing embeddings

Leave newly parsed versions in `PROCESSING`. Use
`DocumentIngestionService.publish_embeddings(version_id, provider)` with an
injected provider. It batches all chunks in one provider request, validates all
1536-dimensional vectors, then writes vectors, marks the version `RETRIEVABLE`,
and switches `Document.current_version_id` in the same database transaction.
If embedding fails or a vector is malformed, roll back the transaction; the
previous current version remains retrievable.

## Incident checks

1. Confirm the source is `ACTIVE`, the document version is `RETRIEVABLE`, and
   the document points to that version.
2. Confirm the requesting principal has a `knowledge.read` resource grant for
   the knowledge base in the same organization.
3. Check both branch query latency and candidate counts independently; never
   troubleshoot by temporarily removing SQL authorization predicates.
4. Re-embed failed `PROCESSING` versions only after correcting the provider
   cause. Do not mark a version retrievable manually.
