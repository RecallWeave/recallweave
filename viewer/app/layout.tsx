import type { Metadata } from "next";
import "./globals.css";

const NEUTRAL_PUBLIC_ORIGIN = "https://recallweave.example";

function publicOrigin(): URL {
  const configured = process.env.NEXT_PUBLIC_RECALLWEAVE_ORIGIN;
  if (!configured) return new URL(NEUTRAL_PUBLIC_ORIGIN);
  try {
    const origin = new URL(configured);
    if (origin.protocol === "https:" || (
      origin.protocol === "http:"
      && ["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname)
    )) {
      return new URL(origin.origin);
    }
  } catch {
    // Fall back to a neutral, reserved origin when a fork is misconfigured.
  }
  return new URL(NEUTRAL_PUBLIC_ORIGIN);
}

export function generateMetadata(): Metadata {
  return {
    metadataBase: publicOrigin(),
    title: "RecallWeave Atlas — See the shape of what you know",
    description:
      "A local-first visual explorer for evidence-cited knowledge graphs built from Obsidian vaults.",
    icons: { icon: "/favicon.svg" },
    openGraph: {
      title: "RecallWeave Atlas",
      description: "See the shape of what you know.",
      type: "website",
      images: [
        {
          url: "/og.png",
          width: 1729,
          height: 910,
          alt: "RecallWeave Atlas knowledge graph",
        },
      ],
    },
    twitter: {
      card: "summary_large_image",
      title: "RecallWeave Atlas",
      description: "A local-first visual explorer for your Obsidian knowledge graph.",
      images: ["/og.png"],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
