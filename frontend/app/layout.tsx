import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SignalGuard",
  description: "Risk-management middleware between a signal source and a broker.",
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
