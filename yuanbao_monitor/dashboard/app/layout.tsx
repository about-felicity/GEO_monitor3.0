import type { Metadata } from "next";
import "./globals.css";
import { SiteAuthGate } from "./SiteAuthGate";

export const metadata: Metadata = {
  title: "GEO Monitor · AI 品牌推荐诊断",
  description: "面向企业的多模型品牌推荐诊断、信源洞察与持续监测平台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body><SiteAuthGate>{children}</SiteAuthGate></body>
    </html>
  );
}
