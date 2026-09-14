import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { purgeAdminUsers } from "@/lib/cp";

export const runtime = "nodejs";

export async function DELETE() {
  const session = await getAdminSession();
  if (!session) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }
  try {
    const result = await purgeAdminUsers();
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 502 },
    );
  }
}
