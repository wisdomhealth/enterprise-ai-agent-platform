import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export type ApplicationCapability = "email" | "support";

export type EffectiveApplicationAccess = {
  capability: ApplicationCapability;
  effective_allowed: boolean;
};

export function hasApplicationAccess(
  access: readonly EffectiveApplicationAccess[],
  capability: ApplicationCapability,
): boolean {
  return access.some(
    (item) => item.capability === capability && item.effective_allowed,
  );
}

export async function requireApplicationAccess(
  capability: ApplicationCapability,
): Promise<void> {
  const cookieStore = await cookies();
  const staffSession = cookieStore.get("staff_session")?.value;
  if (staffSession === undefined) redirect("/api/v1/auth/login");

  const backendUrl = process.env.BACKEND_API_URL;
  if (backendUrl === undefined) {
    throw new Error("BACKEND_API_URL is required to verify application access");
  }
  const response = await fetch(
    `${backendUrl.replace(/\/$/, "")}/api/v1/staff/application-access`,
    {
      cache: "no-store",
      headers: { Cookie: `staff_session=${encodeURIComponent(staffSession)}` },
    },
  );
  if (response.status === 401 || response.status === 403) {
    redirect("/api/v1/auth/login");
  }
  if (!response.ok) throw new Error("Unable to verify application access");
  const access = (await response.json()) as EffectiveApplicationAccess[];
  if (!hasApplicationAccess(access, capability)) redirect("/");
}
