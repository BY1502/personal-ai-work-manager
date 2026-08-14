import type {
  ActivityItem,
  DashboardSummary,
  PageResult,
  ProjectDetail,
  ProjectListItem,
  ProviderStatus,
  WorkItemSummary,
  WorkStatus,
} from "./types";

type UnknownRecord = Record<string, unknown>;

const STATUS_VALUES = new Set([
  "TODO",
  "IN_PROGRESS",
  "WAITING",
  "BLOCKED",
  "HOLD",
  "DONE",
]);

function record(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function string(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function status(value: unknown): WorkStatus {
  const resolved = string(value, "TODO").toUpperCase();
  return STATUS_VALUES.has(resolved) ? (resolved as WorkStatus) : "TODO";
}

export function normalizeWorkItem(value: unknown): WorkItemSummary {
  const item = record(value);
  const nested = record(item.work_item);
  const source = Object.keys(nested).length ? { ...item, ...nested } : item;
  const reasons = list(source.reasons)
    .map((reason) => optionalString(reason))
    .filter((reason): reason is string => Boolean(reason));
  return {
    projectId: optionalString(source.project_id) ?? undefined,
    projectName: string(source.project_name, "프로젝트 미지정"),
    workItemId: optionalString(source.work_item_id ?? source.id) ?? undefined,
    title: string(source.title ?? source.work_item_title, "이름 없는 업무"),
    status: status(source.status ?? source.current_status),
    nextAction: optionalString(source.next_action ?? source.recommended_action),
    waitingFor: optionalString(source.waiting_for ?? source.open_waiting),
    blockedReason: optionalString(source.blocked_reason ?? source.open_blocked),
    reason:
      optionalString(source.reason ?? source.explanation) ??
      (reasons.length ? reasons.join(" · ") : null),
  };
}

export function normalizeSummary(value: unknown): DashboardSummary {
  const data = record(value);
  return {
    currentWork: list(data.current_work ?? data.currentWork).map(normalizeWorkItem),
    waiting: list(data.waiting).map(normalizeWorkItem),
    blocked: list(data.blocked).map(normalizeWorkItem),
    nextActions: list(data.next_actions ?? data.nextActions).map(normalizeWorkItem),
  };
}

export function normalizeActivity(value: unknown): ActivityItem {
  const item = record(value);
  return {
    id: optionalString(item.activity_id ?? item.id) ?? undefined,
    projectName: string(item.project_name, "프로젝트 미지정"),
    workItemTitle: optionalString(item.work_item_title ?? item.title),
    summary: string(item.summary, "업무 기록"),
    occurredAt: optionalString(
      item.occurred_at ?? item.recorded_at_utc ?? item.created_at,
    ),
    occurredOn: optionalString(item.occurred_on ?? item.occurred_on_local),
  };
}

export function normalizeProject(value: unknown): ProjectListItem {
  const item = record(value);
  return {
    id: string(item.project_id ?? item.id),
    name: string(item.project_name ?? item.name, "이름 없는 프로젝트"),
    activeCount: number(
      item.active_count ?? item.active_work_count ?? item.current_work_count,
    ),
    statusLabel: optionalString(item.status_label ?? item.status),
    latestActivity: optionalString(item.latest_activity ?? item.latest_activity_at),
  };
}

export function normalizeProjectDetail(value: unknown): ProjectDetail {
  const data = record(value);
  const base = normalizeProject(data.project ?? data);
  return {
    ...base,
    currentWork: list(
      data.current_work ?? data.current_work_items ?? data.work_items,
    )
      .map(normalizeWorkItem)
      .filter((item) => item.status !== "DONE"),
    completedWork: list(
      data.completed_work ?? data.completed_work_items ?? data.work_items,
    )
      .map(normalizeWorkItem)
      .filter((item) => item.status === "DONE"),
    recentActivity: list(
      data.recent_activity ?? data.recent_activities ?? data.activities,
    ).map(
      normalizeActivity,
    ),
  };
}

export function normalizePage<T>(
  value: unknown,
  mapper: (item: unknown) => T,
  fallbackLimit: number,
  fallbackOffset: number,
): PageResult<T> {
  if (Array.isArray(value)) {
    return {
      items: value.map(mapper),
      total: value.length,
      limit: fallbackLimit,
      offset: fallbackOffset,
    };
  }
  const data = record(value);
  const items = list(data.items ?? data.results).map(mapper);
  return {
    items,
    total: number(data.total, items.length),
    limit: number(data.limit, fallbackLimit),
    offset: number(data.offset, fallbackOffset),
  };
}

export function normalizeProviderStatus(value: unknown): ProviderStatus {
  const data = record(value);
  const rawKind = string(data.provider ?? data.kind ?? data.provider_name, "unknown");
  const kind = ["local", "api", "deterministic"].includes(rawKind)
    ? (rawKind as ProviderStatus["kind"])
    : "unknown";
  const rawState = string(data.status ?? data.state, "UNKNOWN").toUpperCase();
  const state = [
    "READY",
    "LOADING",
    "DEGRADED",
    "UNAVAILABLE",
    "ERROR",
  ].includes(rawState)
    ? (rawState as ProviderStatus["state"])
    : "UNKNOWN";
  return {
    kind,
    label:
      optionalString(data.label) ??
      (kind === "local"
        ? "Local AI"
        : kind === "api"
          ? "API AI"
          : kind === "deterministic"
            ? "테스트 모드"
            : "AI 상태 확인 중"),
    model: optionalString(data.model ?? data.model_name),
    state,
    message: optionalString(data.message),
  };
}
