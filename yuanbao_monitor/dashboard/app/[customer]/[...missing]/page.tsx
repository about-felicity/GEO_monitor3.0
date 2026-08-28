import { redirect } from "next/navigation";

export default function MissingDiagnosisRoute() {
  const configured = String(process.env.VITE_MONITOR_BASE_PATH || "/geo").trim();
  const basePath = configured ? `/${configured.replace(/^\/+|\/+$/g, "")}` : "";
  const rootPath = `${basePath}/` || "/";
  const origin = String(process.env.MONITOR_PUBLIC_ORIGIN || "").trim().replace(/\/+$/, "");
  redirect(origin ? `${origin}${rootPath}` : rootPath);
}
