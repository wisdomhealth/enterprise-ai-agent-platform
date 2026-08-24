# RAG evaluation

Run the deterministic local baseline with:

```bash
scripts/run-rag-evals --dataset backend/tests/fixtures/evals/regression.jsonl --provider fake
```

Each JSONL record carries a dataset version and includes a case ID, question,
answerability label, authoritative document IDs, expected claims, forbidden
document IDs, and tags. A completed run records the dataset identity plus the
document-version set, chunking, embedding, retrieval, prompt, and LLM versions.
It also records Recall@10, citation mapping/support, abstention, answer and
claim groundedness, retrieval/model/end-to-end latency, tokens, and estimated
cost.

The output separates hard security gates from quality targets. The script exits
nonzero only when a hard gate fails: unauthorized candidates, forbidden
documents, or citations not mapping to that run's retrieved set. Recall@10
(0.85), citation support (0.95), and abstention (0.90) are quality targets for
product improvement; they are reported without being represented as security
release gates.

`acceptance.jsonl` is a held-out dataset. It may be run for milestone or release
acceptance, but its labels must never be used for prompt construction,
retrieval, model tuning, or daily regression-driven adjustment. The evaluation
runner sends only each question to the answer service; labels are applied only
after an answer has been produced.

Staff Assist calls `POST /api/v1/staff/knowledge/search` with a staff session
and `{ "question": "..." }`. It uses the staff citation projection, is read
only, and does not create outbox events, workflow state, customer messages, or
delivery intents.
