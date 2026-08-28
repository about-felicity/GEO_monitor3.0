import { redirect } from "next/navigation";
import { DiagnosisDashboard } from "../DiagnosisDashboard";

const REPORT_KEY_PATTERN = /^[a-f0-9]{32}$/;

export const dynamic = "force-dynamic";
export const revalidate = 0;

function publicRootPath() {
  const configured = String(process.env.VITE_MONITOR_BASE_PATH || "/geo").trim();
  const basePath = configured ? `/${configured.replace(/^\/+|\/+$/g, "")}` : "";
  return `${basePath}/` || "/";
}

function publicRootUrl() {
  const origin = String(process.env.MONITOR_PUBLIC_ORIGIN || "").trim().replace(/\/+$/, "");
  return origin ? `${origin}${publicRootPath()}` : publicRootPath();
}

export default async function CustomerDashboard({ params }: { params: Promise<{ customer: string }> }) {
  const { customer } = await params;
  const reportKey = customer.toLowerCase();
  const rootPath = publicRootUrl();

  if (!REPORT_KEY_PATTERN.test(reportKey)) redirect(rootPath);

  let shouldRedirect = false;
  try {
    const apiPort = process.env.DOUBAO_DASHBOARD_PORT || "8765";
    const response = await fetch(`http://127.0.0.1:${apiPort}/api/diagnosis/${reportKey}`, {
      cache: "no-store",
    });

    if (response.status === 404) shouldRedirect = true;
    if (response.ok) {
      const payload = await response.json();
      if (payload?.task?.status !== "completed") shouldRedirect = true;
    }
  } catch (reason) {
    // A transient internal API failure should not turn a valid completed report
    // into a false 404. The client performs the same validation after recovery.
    console.error("Unable to validate diagnosis report before render", reason);
  }

  if (shouldRedirect) redirect(rootPath);

  return <DiagnosisDashboard customerSlug={reportKey} />;
}
