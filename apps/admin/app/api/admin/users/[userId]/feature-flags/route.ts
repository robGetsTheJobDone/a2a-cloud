import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { getAdminUserFeatureFlags, setAdminUserFeatureFlags } from "@/lib/cp";

export const runtime = "nodejs";

type RouteContext = { params: Promise<{ userId: string }> };

function userIdFrom(params: { userId: string }): number | null {
  const value = Number(params.userId);
  return Number.isInteger(value) && value > 0 ? value : null;
}

export async function GET(_req: NextRequest, context: RouteContext) {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const params = await context.params;
  const userId = userIdFrom(params);
  if (!userId) return NextResponse.json({ error: "invalid user id" }, { status: 400 });
  try {
    return NextResponse.json(await getAdminUserFeatureFlags(userId));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 502 },
    );
  }
}

export async function PUT(req: NextRequest, context: RouteContext) {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const params = await context.params;
  const userId = userIdFrom(params);
  if (!userId) return NextResponse.json({ error: "invalid user id" }, { status: 400 });
  try {
    const body = (await req.json()) as { enabled_keys?: unknown };
    const enabledKeys = Array.isArray(body.enabled_keys)
      ? body.enabled_keys.filter((item): item is string => typeof item === "string")
      : [];
    return NextResponse.json(await setAdminUserFeatureFlags(userId, enabledKeys));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 502 },
    );
  }
}
