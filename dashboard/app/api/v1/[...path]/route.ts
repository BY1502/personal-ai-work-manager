import { NextRequest, NextResponse } from "next/server";

type PathParams = {
  params: {
    path: string[];
  };
};

const BACKEND_BASE_URL =
  process.env.BACKEND_INTERNAL_URL?.trim() ||
  process.env.BACKEND_BASE_URL?.trim() ||
  "http://backend:8000";

function buildBackendUrl(segments: string[]): string {
  const normalized = segments
    .map((segment) => segment.trim())
    .filter((segment) => segment.length > 0)
    .map((segment) =>
      segment === "." || segment === ".." ? encodeURIComponent(segment) : segment,
    );
  const path =
    normalized.length > 0 ? `/api/v1/${normalized.join("/")}` : "/api/v1";
  return `${BACKEND_BASE_URL}${path}`;
}

function stripHopByHopHeaders(headers: Headers): Headers {
  const filtered = new Headers(headers);
  const stripKeys = [
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "connection-token",
  ];
  for (const key of stripKeys) {
    filtered.delete(key);
    filtered.delete(key.toUpperCase());
  }
  return filtered;
}

function sanitizeRequestHeaders(source: Headers): Headers {
  const headers = stripHopByHopHeaders(new Headers(source));
  headers.delete("host");
  headers.delete("content-length");
  return headers;
}

async function proxy(method: string, request: NextRequest, path: string[]): Promise<NextResponse> {
  const url = new URL(buildBackendUrl(path));
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.append(key, value);
  });

  const headers = sanitizeRequestHeaders(request.headers);
  const init: RequestInit = {
    method,
    headers,
    cache: "no-store",
    redirect: "follow",
    signal: request.signal,
  };

  if (method !== "GET" && method !== "HEAD") {
    init.body = await request.text();
  }

  try {
    const response = await fetch(url.toString(), init);
    const responseBody = await response.arrayBuffer();
    const proxyHeaders = stripHopByHopHeaders(new Headers(response.headers));
    proxyHeaders.set("Cache-Control", "private, no-store");
    return new NextResponse(responseBody, {
      status: response.status,
      statusText: response.statusText,
      headers: proxyHeaders,
    });
  } catch (error) {
    console.error("BY backend proxy request failed", {
      error: error instanceof Error ? error.name : "UnknownError",
    });
    return NextResponse.json(
      {
        error: {
          code: "BACKEND_PROXY_ERROR",
          detail: "BY backend is unavailable",
        },
      },
      {
        status: 502,
        headers: { "Cache-Control": "private, no-store" },
      },
    );
  }
}

export async function GET(request: NextRequest, { params }: PathParams) {
  return proxy("GET", request, params.path);
}

export async function POST(request: NextRequest, { params }: PathParams) {
  return proxy("POST", request, params.path);
}

export async function PUT(request: NextRequest, { params }: PathParams) {
  return proxy("PUT", request, params.path);
}

export async function PATCH(request: NextRequest, { params }: PathParams) {
  return proxy("PATCH", request, params.path);
}

export async function DELETE(request: NextRequest, { params }: PathParams) {
  return proxy("DELETE", request, params.path);
}

export async function OPTIONS(request: NextRequest, { params }: PathParams) {
  return proxy("OPTIONS", request, params.path);
}
