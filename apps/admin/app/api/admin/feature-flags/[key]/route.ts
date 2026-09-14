import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { deleteAdminFeatureFlag } from "@/lib/cp";

export const runtime = "nodejs";

type RouteContext = { params: Promise<{ key: string }> };

export async function DELETE(_req: Request, context: RouteContext) {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const params = await context.params;
  const key = params.key.trim();
  if (!key) return NextResponse.json({ error: "invalid feature flag key" }, { status: 400 });
  try {
    await deleteAdminFeatureFlag(key);
    return new NextResponse(null, { status: 204 });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 502 },
    );
  }
}
