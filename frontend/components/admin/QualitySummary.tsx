import { AdminQualityStatus } from "../../lib/staff-api";
import { formatUtc } from "./format";

function Quality({ label, value }: { label: string; value: AdminQualityStatus | null }) {
  if (value === null) return <p>{label} quality has not been evaluated.</p>;
  return (
    <article>
      <h3>{label} quality {(value.quality_score * 100).toFixed(1)}%</h3>
      <p>{value.status} · {value.latency_ms} ms · ${value.estimated_cost.toFixed(4)}</p>
      <p>Completed {formatUtc(value.completed_at)}</p>
    </article>
  );
}

export function QualitySummary({
  rag,
  email,
}: {
  rag: AdminQualityStatus | null;
  email: AdminQualityStatus | null;
}) {
  return (
    <section aria-labelledby="quality-summary-heading">
      <h2 id="quality-summary-heading">Quality, latency, and cost</h2>
      <Quality label="RAG" value={rag} />
      <Quality label="Email" value={email} />
    </section>
  );
}
