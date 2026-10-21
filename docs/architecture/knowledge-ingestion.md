# Knowledge ingestion

## Runtime flow

Google Drive remains the authorized source and PostgreSQL remains the ingestion
system of record:

```text
authorized Drive download
  -> local parser
  -> application ParsedSection values
  -> structure detection and LlamaIndex SentenceSplitter
  -> application DocumentChunk rows
  -> LlamaIndex OpenAIEmbedding adapter
  -> vector validation and atomic publication
```

The application does not use a LlamaIndex ingestion database or vector store.
SQLAlchemy persists chunks and 1536-dimensional pgvector values in the existing
schema. Job leases, task deduplication, organization/source authorization, document
versions, retry transitions, and publication fencing remain application-owned.

## PDF parsing and page semantics

`llama-index-readers-file==0.5.6` supplies the local `PDFReader`. The application
writes the already-authorized bytes to a process-local temporary file, asks the
reader for one document per PDF page, converts each result to `ParsedSection`, and
removes the temporary directory immediately. No PDF is uploaded to LlamaCloud,
LlamaParse, or another parsing service.

`page_number` is the one-based physical page position in the PDF file. It is not a
printed page label such as `iv` or `A-3`. Empty text is not assigned a fabricated
page number or heading. PDF errors are recorded with safe codes:

- `PDF_ENCRYPTED`: the local reader cannot decrypt the file;
- `PDF_CORRUPT`: the bytes are not a readable PDF;
- `PDF_EMPTY`: the PDF contains no pages;
- `PDF_OCR_REQUIRED`: pages exist but contain no usable extracted text.

There is no automatic OCR. Image-only or otherwise non-extractable PDFs must be OCRed
outside the platform and uploaded as new Drive files. DOCX parsing remains local and
keeps its existing heading-derived section contract.

## Chunking

The application first preserves parser page boundaries and detects existing English,
numbered, and Chinese heading forms. It then runs one LlamaIndex `SentenceSplitter`
per semantic section, so a chunk never crosses a page or detected section. The
section path is prepended to chunk text; this useful heading context is included in
embedding input. The Drive link, organization/document/version IDs, and other private
source identifiers are not included. The file title remains available through the
`Document` record and citations but is not repeated in every embedding input.

Defaults:

```dotenv
KNOWLEDGE_CHUNK_SIZE=500
KNOWLEDGE_CHUNK_OVERLAP=64
```

Chunk UUIDs remain deterministic UUIDv5 values derived from the document-version ID
and ordinal. Existing JSON chunk metadata records `llama-index-sentence-v1`, the
effective size/overlap, parser version, physical page range, and available section
path. No configuration snapshot, fingerprint, or schema field is added.

## Embeddings and publication

`llama-index-embeddings-openai==0.5.2` supplies `OpenAIEmbedding`. The adapter calls
its asynchronous batch API for both document text and query text, preserving input
order and the existing application `EmbeddingProvider` interface.

```dotenv
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_EMBEDDING_DIMENSIONS=1536
OPENAI_EMBEDDING_BATCH_SIZE=100
OPENAI_EMBEDDING_MAX_RETRIES=2
OPENAI_REQUEST_TIMEOUT_SECONDS=30
```

The adapter rejects a mismatched result count, wrong dimensions, and NaN or infinite
values before persistence. A new version stays `PROCESSING` until all chunks and
vectors validate. Embeddings, `RETRIEVABLE`, and the document's
`current_version_id` switch flush in one caller-owned transaction. Failure leaves the
previous current version available.

## Local verification and a test PDF

Install the exact tested integrations and run the synthetic tests:

```bash
cd backend
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest tests/unit/knowledge/test_parsers.py \
  tests/unit/knowledge/test_chunking.py \
  tests/unit/rag/test_embedding_adapter.py
```

These tests generate multi-page, Chinese, encrypted, empty, and no-text PDFs in
memory and use fake embedding results; they do not call paid APIs. To exercise the
real UI path, upload a non-sensitive test PDF to the configured authorized Drive
root, trigger synchronization in the staff UI, and wait for the source status and
parse job to succeed. A configured OpenAI key is required for that real embedding
step.

## Replacement and rollback

There is no automatic old-chunk rewrite and no force-reimport switch. Follow the
[Drive replacement workflow](../runbooks/drive-sync.md#replace-previously-ingested-files-from-the-ui):
delete/trash old files, sync and wait for version-scoped chunk cleanup, then upload
replacement files with new Drive IDs and sync again.

No database migration is part of this change. To roll back, stop API/worker/beat
processes, deploy the previous application and dependency set, and restart them.
Already published rows remain schema-compatible. Do not reverse a rollback by
changing version states or `current_version_id` manually; restore service and use the
normal Drive synchronization workflow.
