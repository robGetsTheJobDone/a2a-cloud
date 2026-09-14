import { NextResponse } from "next/server";
import {
  buildPostHogConfigScript,
  clientIpFromHeaders,
} from "@a2a/analytics/posthog";

export const dynamic = "force-dynamic";

export function GET(request: Request) {
  return new NextResponse(buildAnalyticsConfig("docs", request), {
    headers: {
      "Content-Type": "application/javascript; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function buildAnalyticsConfig(surface: string, request: Request): string {
  return buildPostHogConfigScript({
    apiKey: process.env.POSTHOG_KEY,
    apiUrl: process.env.POSTHOG_API_URL,
    scriptUrl: process.env.POSTHOG_SCRIPT_URL,
    surface,
    clientIp: clientIpFromHeaders(request.headers),
  });
}
