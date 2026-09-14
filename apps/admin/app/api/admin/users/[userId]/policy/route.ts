import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { getAdminUserPolicy, setAdminUserPolicy } from "@/lib/cp";

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
    return NextResponse.json(await getAdminUserPolicy(userId));
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
    const body = await req.json();
    return NextResponse.json(await setAdminUserPolicy(userId, body));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 502 },
    );
  }
}
