import { AdminSectionPage } from "@/components/AdminPage";
import { sections } from "@/lib/admin-data";

export const dynamic = "force-dynamic";

export default function OrganizationsPage() {
  return <AdminSectionPage section={sections.organizations} />;
}
