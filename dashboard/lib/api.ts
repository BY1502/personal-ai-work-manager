import {
  normalizeActivity,
  normalizePage,
  normalizeProject,
  normalizeProjectDetail,
  normalizeProviderStatus,
  normalizeSummary,
} from "./normalize";
import type {
  ActivityItem,
  DashboardSummary,
  JarvisResponse,
  PageResult,
  ProjectDetail,
  ProjectListItem,
  ProviderStatus,
  ReportSnapshot,
} from "./types";

const configuredBase = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
const inferredBase =
  typeof window === "undefined"
    ? "http://127.0.0.1:8100"
    : `${window.location.protocol}//${window.location.hostname}:8100`;

const isBrowser = typeof window !== "undefined";

function toOrigin(rawUrl: string): string {
  try {
    return new URL(rawUrl).origin;
  } catch {
    return "";
  }
}

function sameOrigin(baseA: string, baseB: string): boolean {
  const a = toOrigin(baseA);
  const b = toOrigin(baseB);
  return a !== "" && b !== "" && a === b;
}

function normalizeUrl(raw: string): string {
  try {
    const url = new URL(raw);
    return `${url.origin}`.replace(/\/$/, "");
  } catch {
    return "";
  }
}

function isLocalHostname(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    hostname === "[::1]"
  );
}

function resolvedApiBase(): string {
  const inferred = normalizeUrl(inferredBase);
  if (!configuredBase) return inferred;

  const parsed = normalizeUrl(configuredBase);
  if (!parsed) return inferred;

  if (typeof window === "undefined") return parsed;

  const configuredHostname = new URL(parsed).hostname;
  const pageHostname = window.location.hostname;
  if (isLocalHostname(configuredHostname) && !isLocalHostname(pageHostname)) {
    return inferred;
  }
  if (
    window.location.protocol === "https:" &&
    !parsed.startsWith("https://") &&
    new URL(inferred).hostname === pageHostname
  ) {
    return inferred;
  }
  return parsed;
}

export const API_BASE_URL = resolvedApiBase();
const API_BASE_ORIGIN = toOrigin(API_BASE_URL);
const LOCAL_LOOPBACK_ORIGIN = "http://127.0.0.1:8100";

function hostOriginCandidates(): string[] {
  const candidates = [API_BASE_ORIGIN];
  if (isBrowser && inferredBase) {
    const inferredOrigin = toOrigin(inferredBase);
    if (inferredOrigin && !sameOrigin(API_BASE_URL, inferredBase)) {
      candidates.push(inferredOrigin);
    }
    const loopbackOrigin = toOrigin(LOCAL_LOOPBACK_ORIGIN);
    if (loopbackOrigin) {
      candidates.push(loopbackOrigin);
    }
    const localhostOrigin = toOrigin("http://localhost:8100");
    if (localhostOrigin) {
      candidates.push(localhostOrigin);
    }
  }
  return Array.from(new Set(candidates.filter(Boolean)));
}

const REQUEST_BASE_ORIGINS = hostOriginCandidates();

function resolveApiPath(rawPath: string): string {
  const trimmed = rawPath.trim();
  if (!trimmed) return "/health";
  if (trimmed.startsWith("/")) return trimmed;
  return `/${trimmed}`;
}

function requestTimeoutMs(init: RequestInit, isFallback: boolean): number {
  const method = init.method?.toUpperCase() ?? "GET";
  const baseTimeoutMs = method === "POST" ? 120_000 : 10_000;
  if (!isFallback) return baseTimeoutMs;
  return method === "POST" ? 8_000 : 2_000;
}

function isTimeoutError(error: unknown): error is DOMException {
  return (
    error instanceof DOMException &&
    (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

function buildRequestUrls(rawPath: string): string[] {
  const trimmed = rawPath.trim();
  if (!trimmed) {
    return [];
  }
  const urls = new Set<string>();

  try {
    const explicit = new URL(trimmed);
    const apiPath = `${explicit.pathname}${explicit.search}`;
    if (explicit.origin === API_BASE_URL || explicit.origin === "") {
      urls.add(trimmed);
    } else {
      urls.add(trimmed);
      for (const baseOrigin of REQUEST_BASE_ORIGINS) {
        urls.add(`${baseOrigin}${apiPath}`);
      }
    }
    if (isBrowser && explicit.pathname.startsWith("/api/")) {
      urls.add(explicit.href);
    }
    return Array.from(urls);
  } catch {
    // not absolute URL
  }

  const apiPath = resolveApiPath(trimmed);
  if (isBrowser && apiPath.startsWith("/api/")) {
    urls.add(`${window.location.origin}${apiPath}`);
  }
  for (const baseOrigin of REQUEST_BASE_ORIGINS) {
    urls.add(`${baseOrigin}${apiPath}`);
  }
  return Array.from(urls);
}

function withAbort<T>(
  task: (signal: AbortSignal) => Promise<T>,
  timeoutMs: number,
  parentSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), timeoutMs);
  const abortReason = new DOMException("request timed out", "AbortError");
  const externalSignal = parentSignal;
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort(externalSignal.reason);
    } else {
      externalSignal.addEventListener(
        "abort",
        () => controller.abort(externalSignal.reason),
        { once: true },
      );
    }
  }
  const request = task(controller.signal).finally(() =>
    globalThis.clearTimeout(timeout),
  );
  return Promise.race([
    request,
    new Promise<never>((_, reject) =>
      controller.signal.addEventListener("abort", () =>
        reject(controller.signal.reason ?? abortReason),
      ),
    ),
  ]);
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message);
  }
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError("서버 응답을 읽지 못했습니다.", response.status);
  }
}

function errorMessage(body: unknown): { message: string; code?: string } {
  const data = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
  const nested =
    data.error && typeof data.error === "object"
      ? (data.error as Record<string, unknown>)
      : {};
  return {
    message:
      (typeof nested.detail === "string" && nested.detail) ||
      (typeof data.detail === "string" && data.detail) ||
      "요청을 처리하지 못했습니다.",
    code:
      (typeof nested.code === "string" && nested.code) ||
      (typeof data.code === "string" && data.code) ||
      undefined,
  };
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let lastError: unknown;
  const requestUrls = buildRequestUrls(path);

  for (let index = 0; index < requestUrls.length; index += 1) {
    const requestUrl = requestUrls[index];
    const isFallback = index > 0;
    let response: Response;
    try {
      const timeoutMs = requestTimeoutMs(init ?? {}, isFallback);
      response = await withAbort(
    (signal) =>
      fetch(requestUrl, {
        ...init,
            headers: {
              "Content-Type": "application/json",
              ...init?.headers,
            },
        signal,
      }),
    timeoutMs,
    init?.signal || undefined,
  );
    } catch (error) {
      lastError = error;
      continue;
    }

    const body = await parseBody(response);
    if (!response.ok) {
      const problem = errorMessage(body);
      throw new ApiError(problem.message, response.status, problem.code);
    }
    return body;
  }

  if (requestUrls.length > 1) {
    console.debug("API request fallback failed", {
      base: API_BASE_URL,
      path,
      urls: requestUrls,
      error: lastError instanceof Error ? lastError.message : String(lastError),
    });
  }

  if (isTimeoutError(lastError)) {
    throw new ApiError(
      "요청이 응답하지 않아 중단했습니다.",
      0,
      "REQUEST_TIMEOUT",
    );
  }

  throw new ApiError("BY 서버에 연결할 수 없습니다.", 0, "NETWORK_ERROR");
}

export async function getSummary(): Promise<DashboardSummary> {
  return normalizeSummary(await request("/api/v1/dashboard/summary"));
}

export async function getActivities(
  limit = 8,
  offset = 0,
): Promise<PageResult<ActivityItem>> {
  const data = await request(
    `/api/v1/dashboard/activities?limit=${limit}&offset=${offset}`,
  );
  return normalizePage(data, normalizeActivity, limit, offset);
}

export async function getProjects(
  limit = 12,
  offset = 0,
): Promise<PageResult<ProjectListItem>> {
  const data = await request(
    `/api/v1/dashboard/projects?limit=${limit}&offset=${offset}`,
  );
  return normalizePage(data, normalizeProject, limit, offset);
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  return normalizeProjectDetail(
    await request(`/api/v1/dashboard/projects/${encodeURIComponent(projectId)}`),
  );
}

export async function getProviderStatus(): Promise<ProviderStatus> {
  return normalizeProviderStatus(await request("/api/v1/dashboard/provider"));
}

export async function getReports(limit = 20): Promise<ReportSnapshot[]> {
  const data = await request(`/api/v1/reports?limit=${limit}`);
  return Array.isArray(data) ? (data as ReportSnapshot[]) : [];
}

export async function getReport(reportId: string): Promise<ReportSnapshot> {
  return (await request(
    `/api/v1/reports/${encodeURIComponent(reportId)}`,
  )) as ReportSnapshot;
}

export type ChatRunResult =
  | { kind: "complete"; response: JarvisResponse }
  | { kind: "in_progress"; runId: string; statusUrl: string };

export async function createChatRun(input: {
  content: string;
  conversationId?: string | null;
  clientMessageId: string;
}): Promise<ChatRunResult> {
  const requestPayload = JSON.stringify({
    conversation_id: input.conversationId || null,
    client_message_id: input.clientMessageId,
    content: input.content,
  });
  const body = (await request("/api/v1/chat/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: requestPayload,
    signal: undefined,
  })) as Record<string, unknown>;

  const isInProgressResponse =
    String(body.status || body.code || "COMPLETED") === "RUN_IN_PROGRESS" &&
    Boolean(body.status_url);
  if (isInProgressResponse) {
    return {
      kind: "in_progress",
      runId: String(body.run_id || ""),
      statusUrl: String(body.status_url || `/api/v1/runs/${body.run_id}`),
    };
  }

  return { kind: "complete", response: body as unknown as JarvisResponse };
}

export async function getRun(statusUrl: string): Promise<{
  status: string;
  result: JarvisResponse | null;
}> {
  const normalizedPath = statusUrl.startsWith("http")
    ? (() => {
        try {
          const parsed = new URL(statusUrl);
          return `${parsed.pathname}${parsed.search}`;
        } catch {
          return statusUrl;
        }
      })()
    : statusUrl;
  const body = (await request(normalizedPath)) as Record<string, unknown>;
  return {
    status: String(body.status || ""),
    result: (body.result as JarvisResponse | null) || null,
  };
}

export async function resolveClarification(input: {
  clarificationId: string;
  action: "SELECT_EXISTING" | "CREATE_NEW" | "CANCEL";
  workItemId?: string;
  projectName?: string;
  workItemTitle?: string;
  idempotencyKey: string;
}): Promise<JarvisResponse> {
  return (await request(
    `/api/v1/clarifications/${encodeURIComponent(input.clarificationId)}/resolve`,
    {
      method: "POST",
      headers: { "Idempotency-Key": input.idempotencyKey },
      body: JSON.stringify({
        action: input.action,
        work_item_id: input.workItemId || null,
        project_name: input.projectName || null,
        work_item_title: input.workItemTitle || null,
      }),
    },
  )) as JarvisResponse;
}
