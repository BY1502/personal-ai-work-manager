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
  AuthResponse,
  AuthUser,
  CalendarResolutionResponse,
  ConversationListResponse,
  ConversationCreateResponse,
  ConversationMessage,
  ConversationMessagesResponse,
  DashboardSummary,
  JarvisResponse,
  PageResult,
  ProjectDetail,
  ProjectListItem,
  ProviderStatus,
  RecoveryCodeResponse,
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

export const AUTH_REQUIRED_EVENT = "by:auth-required";

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
    if (isBrowser && explicit.pathname.startsWith("/api/")) {
      return [`${window.location.origin}${apiPath}`];
    }
    if (explicit.origin === API_BASE_URL || explicit.origin === "") {
      urls.add(trimmed);
    } else {
      urls.add(trimmed);
      for (const baseOrigin of REQUEST_BASE_ORIGINS) {
        urls.add(`${baseOrigin}${apiPath}`);
      }
    }
    return Array.from(urls);
  } catch {
    // not absolute URL
  }

  const apiPath = resolveApiPath(trimmed);
  if (isBrowser && apiPath.startsWith("/api/")) {
    // Authenticated browser traffic always uses the same-origin dashboard
    // proxy. This keeps the HttpOnly session cookie out of cross-origin
    // fallback requests.
    return [`${window.location.origin}${apiPath}`];
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
    public readonly retryAfterSeconds?: number,
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
            credentials: "include",
            cache: "no-store",
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
      const requestPathname = (() => {
        try {
          return new URL(requestUrl).pathname;
        } catch {
          return "";
        }
      })();
      const isCredentialEntry =
        requestPathname.endsWith("/auth/login") ||
        requestPathname.endsWith("/auth/register") ||
        requestPathname.endsWith("/auth/password/reset");
      const isRejectedCredential = [
        "INVALID_CREDENTIALS",
        "INVALID_RECOVERY_CODE",
      ].includes(problem.code || "");
      if (
        response.status === 401 &&
        isBrowser &&
        !isCredentialEntry &&
        !isRejectedCredential
      ) {
        window.dispatchEvent(new CustomEvent(AUTH_REQUIRED_EVENT));
      }
      const retryAfterHeader = response.headers.get("Retry-After");
      const retryAfterSeconds = retryAfterHeader
        ? Number.parseInt(retryAfterHeader, 10)
        : undefined;
      throw new ApiError(
        problem.message,
        response.status,
        problem.code,
        typeof retryAfterSeconds === "number" && Number.isFinite(retryAfterSeconds)
          ? retryAfterSeconds
          : undefined,
      );
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

export async function getCurrentUser(): Promise<AuthUser> {
  const body = (await request("/api/v1/auth/me")) as AuthResponse;
  return body.user;
}

export async function login(input: {
  username: string;
  password: string;
}): Promise<AuthUser> {
  const body = (await request("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(input),
  })) as AuthResponse;
  return body.user;
}

export async function register(input: {
  username: string;
  password: string;
  displayName?: string;
}): Promise<AuthResponse> {
  return (await request("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({
      username: input.username,
      password: input.password,
      display_name: input.displayName?.trim() || null,
    }),
  })) as AuthResponse;
}

export async function logout(): Promise<void> {
  await request("/api/v1/auth/logout", { method: "POST" });
}

export async function changePassword(input: {
  currentPassword: string;
  newPassword: string;
}): Promise<AuthResponse> {
  return (await request("/api/v1/auth/password/change", {
    method: "POST",
    body: JSON.stringify({
      current_password: input.currentPassword,
      new_password: input.newPassword,
    }),
  })) as AuthResponse;
}

export async function resetPassword(input: {
  username: string;
  recoveryCode: string;
  newPassword: string;
}): Promise<RecoveryCodeResponse> {
  return (await request("/api/v1/auth/password/reset", {
    method: "POST",
    body: JSON.stringify({
      username: input.username,
      recovery_code: input.recoveryCode,
      new_password: input.newPassword,
    }),
  })) as RecoveryCodeResponse;
}

export async function rotateRecoveryCode(input: {
  currentPassword: string;
}): Promise<RecoveryCodeResponse> {
  return (await request("/api/v1/auth/recovery-code/rotate", {
    method: "POST",
    body: JSON.stringify({ current_password: input.currentPassword }),
  })) as RecoveryCodeResponse;
}

export async function logoutAll(): Promise<void> {
  await request("/api/v1/auth/logout-all", { method: "POST" });
}

export async function getConversations(
  limit = 30,
  offset = 0,
): Promise<ConversationListResponse> {
  return (await request(
    `/api/v1/chat/conversations?limit=${limit}&offset=${offset}`,
  )) as ConversationListResponse;
}

export async function createConversation(input: {
  idempotencyKey: string;
  title?: string;
}): Promise<ConversationCreateResponse> {
  return (await request("/api/v1/chat/conversations", {
    method: "POST",
    headers: { "Idempotency-Key": input.idempotencyKey },
    body: JSON.stringify({ title: input.title?.trim() || null }),
  })) as ConversationCreateResponse;
}

export async function getConversationMessages(
  conversationId: string,
  limit = 100,
  beforeSequence?: number,
): Promise<ConversationMessagesResponse> {
  const before =
    typeof beforeSequence === "number"
      ? `&before_sequence=${encodeURIComponent(beforeSequence)}`
      : "";
  return (await request(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages?limit=${limit}${before}`,
  )) as ConversationMessagesResponse;
}

export async function getConversationHistory(
  conversationId: string,
  maxMessages = 500,
): Promise<ConversationMessage[]> {
  const messages: ConversationMessage[] = [];
  let beforeSequence: number | undefined;

  while (messages.length < maxMessages) {
    const page = await getConversationMessages(
      conversationId,
      Math.min(100, maxMessages - messages.length),
      beforeSequence,
    );
    messages.push(...page.items);
    if (!page.has_more || page.items.length === 0) break;
    const nextBefore = Math.min(
      ...page.items.map((message) => message.server_sequence),
    );
    if (nextBefore === beforeSequence) break;
    beforeSequence = nextBefore;
  }

  return messages;
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
  signal?: AbortSignal;
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
    signal: input.signal,
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

export async function resolveCalendarProposal(input: {
  proposalId: string;
  action: "APPROVE" | "REJECT";
  expectedVersion: number;
  idempotencyKey: string;
}): Promise<CalendarResolutionResponse> {
  return (await request(
    `/api/v1/calendar/proposals/${encodeURIComponent(input.proposalId)}/resolve`,
    {
      method: "POST",
      headers: { "Idempotency-Key": input.idempotencyKey },
      body: JSON.stringify({
        action: input.action,
        expected_version: input.expectedVersion,
      }),
    },
  )) as CalendarResolutionResponse;
}
