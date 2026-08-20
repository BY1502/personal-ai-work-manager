export type WorkStatus =
  | "TODO"
  | "IN_PROGRESS"
  | "WAITING"
  | "BLOCKED"
  | "HOLD"
  | "DONE";

export interface WorkItemSummary {
  projectId?: string;
  projectName: string;
  workItemId?: string;
  title: string;
  status: WorkStatus;
  nextAction?: string | null;
  waitingFor?: string | null;
  blockedReason?: string | null;
  reason?: string | null;
}

export interface DashboardSummary {
  currentWork: WorkItemSummary[];
  waiting: WorkItemSummary[];
  blocked: WorkItemSummary[];
  nextActions: WorkItemSummary[];
}

export interface ActivityItem {
  id?: string;
  projectName: string;
  workItemTitle?: string | null;
  summary: string;
  occurredAt?: string | null;
  occurredOn?: string | null;
}

export interface ProjectListItem {
  id: string;
  name: string;
  activeCount: number;
  statusLabel?: string | null;
  latestActivity?: string | null;
}

export interface ProjectDetail extends ProjectListItem {
  currentWork: WorkItemSummary[];
  completedWork: WorkItemSummary[];
  recentActivity: ActivityItem[];
}

export interface ProviderStatus {
  kind: "local" | "api" | "deterministic" | "unknown";
  label: string;
  model?: string | null;
  state:
    | "READY"
    | "LOADING"
    | "DEGRADED"
    | "UNAVAILABLE"
    | "ERROR"
    | "UNKNOWN";
  message?: string | null;
}

export interface ClarificationCandidate {
  project_id: string;
  project_name: string;
  work_item_id: string;
  work_item_title: string;
  status: WorkStatus;
  waiting_for?: string | null;
  next_action?: string | null;
}

export interface Clarification {
  clarification_id: string;
  question: string;
  candidates: ClarificationCandidate[];
}

export interface CalendarProposal {
  proposal_id: string;
  action: "CREATE";
  status: string;
  version: number;
  title: string;
  start_at: string;
  end_at: string;
  timezone: string;
  requires_approval: boolean;
}

export interface CalendarResolutionResponse {
  status: string;
  display_response: string;
  proposal: CalendarProposal;
}

export interface ReportSectionItem {
  text: string;
}

export interface ReportSnapshot {
  report_id: string;
  report_type: "DAILY" | "WEEKLY" | "PROJECT" | "RANGE" | string;
  period: {
    start_date: string;
    end_date: string;
    timezone?: string;
  };
  project?: { project_name?: string; name?: string } | null;
  sections: Record<string, ReportSectionItem[]>;
  rendered_text?: string;
  freshness: "FRESH" | "STALE" | string;
  as_of_utc?: string;
}

export interface JarvisResponse {
  run_id: string;
  conversation_id: string;
  status: "COMPLETED" | "NEEDS_CLARIFICATION" | "FAILED";
  display_response: string;
  voice_response?: string;
  audio_url?: string | null;
  audio_duration_seconds?: number | null;
  clarification?: Clarification | null;
  data?: {
    query_type?: string;
    report?: ReportSnapshot;
    recommendations?: unknown;
    [key: string]: unknown;
  } | null;
}

export interface AuthUser {
  id: string;
  username: string;
  display_name: string;
  timezone: string;
  locale: string;
  is_owner: boolean;
}

export interface AuthResponse {
  user: AuthUser;
  legacy_data_claimed?: boolean;
  recovery_code?: string;
}

export interface RecoveryCodeResponse {
  recovery_code: string;
}

export interface ConversationListItem {
  id: string;
  title?: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
  is_default?: boolean;
  request_count?: number;
  last_message_preview?: string | null;
}

export interface ConversationCreateResponse {
  conversation: ConversationListItem;
  created: boolean;
}

export interface ConversationListResponse {
  items: ConversationListItem[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface ConversationMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  server_sequence: number;
  sequence_cursor?: number | null;
  created_at: string;
  response?: JarvisResponse | null;
  client_message_id?: string | null;
  run_id?: string | null;
  run_status?: string | null;
  status_url?: string | null;
}

export interface ConversationMessagesResponse {
  conversation_id: string;
  items: ConversationMessage[];
  has_more: boolean;
  next_before_sequence?: number | null;
}

export type MessageState = "sending" | "polling" | "complete" | "error";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  state: MessageState;
  retryContent?: string;
  clientMessageId?: string;
  clarification?: Clarification | null;
  calendarProposal?: CalendarProposal | null;
  report?: ReportSnapshot | null;
  audioUrl?: string | null;
}

export interface PageResult<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
