import { EmailQueue } from "../../../components/email/EmailQueue";
import { StaffAssist } from "../../../components/support/StaffAssist";
import { requireStaffSession } from "../../../lib/staff-session";

export default async function StaffEmailPage() {
  const staff = await requireStaffSession();
  return (
    <div className="staff-page-grid" aria-label="Email operations">
      <EmailQueue canSync={staff.role === "ADMIN"} />
      <StaffAssist />
    </div>
  );
}
