import { EmailQueue } from "../../../components/email/EmailQueue";
import { StaffAssist } from "../../../components/support/StaffAssist";

export default function StaffEmailPage() {
  return (
    <section aria-label="Email operations">
      <EmailQueue />
      <StaffAssist />
    </section>
  );
}
