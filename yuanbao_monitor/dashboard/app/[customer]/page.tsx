import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { DiagnosisDashboard } from "../DiagnosisDashboard";
import { PaidCustomerDashboard } from "../PaidCustomerDashboard";

const REPORT_KEY_PATTERN = /^[a-f0-9]{32}$/;
const PAID_MONITOR_PATTERN = /^paid-[a-f0-9]{24}$/;

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

  const isPaidMonitor = PAID_MONITOR_PATTERN.test(reportKey);
  if (!isPaidMonitor && !REPORT_KEY_PATTERN.test(reportKey)) redirect(rootPath);

  let shouldRedirect = false;
  try {
    const apiPort = process.env.DOUBAO_DASHBOARD_PORT || "8765";
    const requestHeaders = await headers();
    const cookie = requestHeaders.get("cookie") || "";
    const endpoint = isPaidMonitor ? `/api/paid-monitor/${reportKey}` : `/api/diagnosis/${reportKey}`;
    const response = await fetch(`http://127.0.0.1:${apiPort}${endpoint}`, {
      cache: "no-store",
      headers: cookie ? { Cookie: cookie } : undefined,
    });

    if (response.status === 404) shouldRedirect = true;
    if (response.ok) {
      const payload = await response.json();
      if (isPaidMonitor ? !payload?.access_allowed : payload?.task?.status !== "completed") shouldRedirect = true;
    }
  } catch (reason) {
    // A transient internal API failure should not turn a valid completed report
    // into a false 404. The client performs the same validation after recovery.
    console.error("Unable to validate diagnosis report before render", reason);
  }

  if (shouldRedirect) redirect(rootPath);

  return isPaidMonitor
    ? <PaidCustomerDashboard customerSlug={reportKey} />
    : <DiagnosisDashboard customerSlug={reportKey} />;
}
