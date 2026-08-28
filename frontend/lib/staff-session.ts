import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export type StaffSession = { id: string; organization_id: string; email: string; role: string };

/**
 * Keep staff pages behind the backend's OIDC session authority.  A cookie
 * presence check alone is not authorization, so an unavailable backend fails
 * closed instead of rendering an unauthenticated console.
 */
export async function requireStaffSession(): Promise<StaffSession> {
  const cookieStore = await cookies();
  const staffSession = cookieStore.get("staff_session")?.value;
  if (staffSession === undefined) redirect("/api/v1/auth/login");

  const backendUrl = process.env.BACKEND_API_URL;
  if (backendUrl === undefined) {
    throw new Error("BACKEND_API_URL is required to verify the staff OIDC session");
  }
  const csrf = cookieStore.get("staff_csrf")?.value;
  const response = await fetch(`${backendUrl.replace(/\/$/, "")}/api/v1/auth/me`, {
    cache: "no-store",
    headers: {
      Cookie: `staff_session=${encodeURIComponent(staffSession)}${csrf ? `; staff_csrf=${encodeURIComponent(csrf)}` : ""}`,
    },
  });
  if (response.status === 401 || response.status === 403) redirect("/api/v1/auth/login");
  if (!response.ok) throw new Error("Unable to verify the staff OIDC session");
  return (await response.json()) as StaffSession;
}
