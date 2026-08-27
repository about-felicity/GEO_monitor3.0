import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "模型情报台 · 多模型监控",
  description: "按模型、问题与日期统一查看回答、信源排行和关键词洞察的本地监控面板",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
