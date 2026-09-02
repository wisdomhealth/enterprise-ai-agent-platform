"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { EmailReviewPanel } from "../../../../components/email/EmailReviewPanel";
import { StaffAssist } from "../../../../components/support/StaffAssist";
import { EmailDetail, getEmailDetail } from "../../../../lib/staff-api";

export default function StaffEmailDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [item, setItem] = useState<EmailDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void getEmailDetail(id)
      .then(setItem)
      .catch(() => setError("Unable to load this email."));
  }, [id]);

  if (error) return <p role="alert">{error}</p>;
  if (!item) return <p>Loading email…</p>;
  return (
    <section aria-label="Email review detail">
      <EmailReviewPanel item={item} />
      <StaffAssist />
    </section>
  );
}
