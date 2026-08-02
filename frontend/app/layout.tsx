import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SignalGuard",
  description: "Risk-management middleware between a signal source and a broker.",
  // Served from public/ rather than the app/icon convention: without it every
  // page load logs a 404 for /favicon.ico in the browser console, which trains
  // whoever is debugging this dashboard to ignore console errors.
  icons: { icon: "/icon.svg" },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
