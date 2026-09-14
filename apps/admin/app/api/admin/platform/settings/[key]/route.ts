import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { setPlatformSetting } from "@/lib/cp";

export const runtime = "nodejs";

export async function PUT(
  request: NextRequest,
  context: { params: Promise<{ key: string }> },
) {
  const session = await getAdminSession();
  if (!session) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }
  let body: { value?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }
  if (!("value" in body)) {
    return NextResponse.json({ error: "missing value" }, { status: 400 });
  }
  const { key } = await context.params;
  try {
    const row = await setPlatformSetting(key, body.value, session.sub);
    return NextResponse.json(row);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 502 },
    );
  }
}
