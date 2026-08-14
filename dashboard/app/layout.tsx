import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const forwardedHost = requestHeaders.get("x-forwarded-host");
  const host = forwardedHost || requestHeaders.get("host") || "localhost:3001";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ||
    (host.startsWith("localhost") || host.startsWith("127.0.0.1")
      ? "http"
      : "https");
  const origin = process.env.NEXT_PUBLIC_SITE_URL || `${protocol}://${host}`;
  const socialImage = new URL("/og.png", origin).toString();

  return {
    metadataBase: new URL(origin),
    applicationName: "BY",
    manifest: "/manifest.webmanifest",
    appleWebApp: {
      capable: true,
      title: "BY",
      statusBarStyle: "black-translucent",
    },
    icons: {
      icon: "/by-icon.svg",
      apple: "/by-icon.svg",
    },
    title: "BY · 나의 AI 업무 매니저",
    description:
      "대화로 업무를 기록하고, 지금 할 일과 업무 흐름을 한눈에 확인하는 개인 AI 업무 매니저입니다.",
    openGraph: {
      title: "BY · 나의 AI 업무 매니저",
      description:
        "대화로 업무를 기록하고, 지금 할 일과 업무 흐름을 한눈에 확인하세요.",
      type: "website",
      locale: "ko_KR",
      images: [
        {
          url: socialImage,
          width: 1200,
          height: 630,
          alt: "BY 개인 AI 업무 매니저 대시보드",
        },
      ],
    },
    twitter: {
      card: "summary_large_image",
      title: "BY · 나의 AI 업무 매니저",
      description: "대화로 업무를 기록하고 현재 업무 흐름을 확인하세요.",
      images: [socialImage],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
