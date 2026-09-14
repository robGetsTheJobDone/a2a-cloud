import { NextResponse } from "next/server";
import { emptyAdminCookie, emptyOidcStateCookie } from "@/lib/auth";

export async function POST(request: Request) {
  void request;
  const response = new NextResponse(null, { status: 303, headers: { location: "/login" } });
  response.cookies.set(emptyAdminCookie.name, emptyAdminCookie.value, emptyAdminCookie.options);
  response.cookies.set(emptyOidcStateCookie.name, emptyOidcStateCookie.value, emptyOidcStateCookie.options);
  return response;
}
