import { AdminSectionPage } from "@/components/AdminPage";
import { sections } from "@/lib/admin-data";

export const dynamic = "force-dynamic";

export default function DeploymentsPage() {
  return <AdminSectionPage section={sections.deployments} />;
}
