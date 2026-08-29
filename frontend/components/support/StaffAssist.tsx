"use client";

import { FormEvent, useState } from "react";

import { searchStaffKnowledge, StaffKnowledgeAnswer } from "../../lib/staff-api";

export function StaffAssist() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<StaffKnowledgeAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!question.trim()) return;
    setError(null);
    try {
      setAnswer(await searchStaffKnowledge(question.trim()));
    } catch {
      setAnswer(null);
      setError("Knowledge search is unavailable.");
    }
  }

  return (
    <aside aria-labelledby="staff-assist-heading">
      <h2 id="staff-assist-heading">Staff Assist</h2>
      <p>Internal reference only. Search results never send a customer message.</p>
      <form onSubmit={search}>
        <label htmlFor="staff-assist-query">Ask the knowledge base</label>
        <textarea
          id="staff-assist-query"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          maxLength={4000}
        />
        <button type="submit">Search</button>
      </form>
      {error ? <p role="alert">{error}</p> : null}
      {answer ? (
        <section aria-label="Staff Assist answer">
          <p>{answer.text}</p>
          <ul aria-label="Internal source details">
            {answer.citations.map((citation) => (
              <li key={citation.chunk_id}>
                <strong>{citation.title}</strong>
                {citation.section ? <span> — {citation.section}</span> : null}
                {citation.page_number ? <span> (page {citation.page_number})</span> : null}
                <p>Chunk {citation.chunk_id}</p>
                <p>Version {citation.document_version_id}</p>
                {citation.internal_drive_link ? (
                  <a href={citation.internal_drive_link}>Open internal source</a>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </aside>
  );
}
