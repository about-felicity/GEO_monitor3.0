"use client";

import { useEffect, useState, type ReactNode } from "react";
import { EnterpriseBrandMark } from "./EnterpriseBrand";

const AUTH_CHECK_TIMEOUT_MS = 5000;

function basePath() {
  const configured = String(import.meta.env.VITE_MONITOR_BASE_PATH || "").trim().replace(/\/$/, "");
  if (configured || typeof window === "undefined") return configured;
  return window.location.pathname === "/geo" || window.location.pathname.startsWith("/geo/") ? "/geo" : "";
}

function isPublicCompletedReportPath(pathname: string, base: string) {
  const escapedBase = base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${escapedBase}/(?:[a-f0-9]{32}|paid-[a-f0-9]{24})/?$`, "i").test(pathname);
}

function adminLoginUrl() {
  const base = basePath();
  const adminPath = `${base}/admin` || "/admin";
  const next = `${window.location.pathname}${window.location.search}`;
  return `${adminPath}?next=${encodeURIComponent(next)}`;
}

export function SiteAuthGate({ children }: { children: ReactNode }) {
  const [allowed, setAllowed] = useState(false);

  useEffect(() => {
    const base = basePath();
    const adminPath = `${base}/admin` || "/admin";
    if (isPublicCompletedReportPath(window.location.pathname, base)) {
      setAllowed(true);
      return;
    }
    if (window.location.pathname === adminPath || window.location.pathname.startsWith(`${adminPath}/`)) {
      setAllowed(true);
      return;
    }
    const controller = new AbortController();
    let redirected = false;
    const redirectToLogin = () => {
      if (redirected) return;
      redirected = true;
      controller.abort();
      window.location.replace(adminLoginUrl());
    };
    const timeout = window.setTimeout(redirectToLogin, AUTH_CHECK_TIMEOUT_MS);
    void (async () => {
      try {
        const response = await fetch(`${base}/api/admin/session?_=${Date.now()}`, {
          cache: "no-store",
          credentials: "include",
          signal: controller.signal,
        });
        const payload = await response.json();
        if (response.ok && payload.authenticated) {
          window.clearTimeout(timeout);
          setAllowed(true);
          return;
        }
      } catch (_) {
        // Redirect below so the login page can show a useful connection error.
      }
      redirectToLogin();
    })();
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, []);

  if (!allowed) {
    return <main className="admin-shell site-auth-shell"><div className="admin-login-card site-auth-card" aria-live="polite">
      <EnterpriseBrandMark size={58} />
      <small>SECURE ACCESS</small>
      <h1>正在验证访问身份</h1>
      <p>通常只需几秒。若网络校验未响应，系统会自动进入登录页。</p>
      <span className="site-auth-progress"><i /></span>
      <button type="button" onClick={() => window.location.replace(adminLoginUrl())}>立即前往登录</button>
    </div></main>;
  }
  return children;
}
