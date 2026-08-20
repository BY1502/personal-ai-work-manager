"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  ApiError,
  createChatRun,
  getActivities,
  getProject,
  getProjects,
  getProviderStatus,
  getReport,
  getReports,
  getRun,
  getSummary,
  resolveClarification,
  resolveCalendarProposal,
} from "@/lib/api";
import { pollRun } from "@/lib/polling";
import type {
  ActivityItem,
  CalendarProposal,
  ChatMessage,
  Clarification,
  DashboardSummary,
  JarvisResponse,
  PageResult,
  ProjectDetail,
  ProjectListItem,
  ProviderStatus,
  ReportSnapshot,
  WorkItemSummary,
  WorkStatus,
} from "@/lib/types";

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

const EMPTY_SUMMARY: DashboardSummary = {
  currentWork: [],
  waiting: [],
  blocked: [],
  nextActions: [],
};

const INITIAL_MESSAGE: ChatMessage = {
  id: "jarvis-welcome",
  role: "assistant",
  state: "complete",
  content:
    "안녕하세요. 오늘 한 일, 기다리는 답변, 다음에 해야 할 업무를 편하게 말씀해주세요.",
};

const QUICK_PROMPTS = [
  "지금 뭐부터 해야 돼?",
  "오늘 뭐 했어?",
  "대기 중인 거 있어?",
  "이번 주 업무보고 만들어줘.",
];

const STATUS_LABEL: Record<WorkStatus, string> = {
  TODO: "할 일",
  IN_PROGRESS: "진행 중",
  WAITING: "회신 대기",
  BLOCKED: "막힘",
  HOLD: "보류",
  DONE: "완료",
};

const REPORT_LABEL: Record<string, string> = {
  DAILY: "오늘 업무보고",
  WEEKLY: "이번 주 업무보고",
  PROJECT: "프로젝트 보고",
  RANGE: "기간별 보고",
};

const REPORT_SECTIONS = [
  ["major_work", "주요 수행 업무"],
  ["completed_work", "완료 업무"],
  ["in_progress_work", "진행 중 업무"],
  ["issues", "이슈 / 대기사항"],
  ["next_actions", "다음 업무"],
] as const;

function createId(prefix: string): string {
  const value =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${value}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function friendlyError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "GOOGLE_CALENDAR_NOT_CONFIGURED") {
      return "Google Calendar 연결이 필요합니다. 로컬 설정을 확인해주세요.";
    }
    if (error.code === "CALENDAR_EXECUTION_UNCERTAIN") {
      return "등록 결과가 불확실해 중복 생성을 막았습니다. Google Calendar를 확인해주세요.";
    }
    if (error.code === "GOOGLE_CALENDAR_FAILED") {
      return "Google Calendar 연결이 원활하지 않습니다. 잠시 후 다시 시도해주세요.";
    }
    if (
      error.code === "EXTRACTION_TIMEOUT" ||
      error.code === "REQUEST_TIMEOUT" ||
      error.status === 504
    ) {
      return "생각하는 데 시간이 조금 더 필요했습니다. 같은 요청으로 다시 시도해주세요.";
    }
    if (error.code === "EXTRACTION_CONCURRENCY_EXCEEDED" || error.code === "DATABASE_BUSY") {
      return "요청이 한꺼번에 많이 들어와서 지연 중입니다. 잠깐 뒤에 다시 시도해주세요.";
    }
    if (error.status === 503 && !error.code) {
      return "요청이 많아 서버가 과부하 상태입니다. 잠깐 뒤에 다시 시도해주세요.";
    }
    if (error.code === "BACKEND_OVERLOADED" && error.status === 503) {
      return "요청이 많아 서버가 잠시 과부하 상태입니다. 잠깐 뒤에 다시 시도해주세요.";
    }
    if (error.code === "EXTRACTION_PROVIDER_FAILED" || error.code === "DATABASE_ERROR") {
      return "AI 연결이 원활하지 않습니다. 잠시 후 다시 시도해주세요.";
    }
    if (error.code === "BACKEND_PROXY_ERROR" || error.status === 502) {
      return "BY 서버에 일시적으로 접근할 수 없습니다. 잠깐 뒤에 다시 시도해주세요.";
    }
    if (error.code === "VERSION_CONFLICT" || error.status === 409) {
      return "업무 상태가 방금 변경되었습니다. 최신 상태를 확인한 뒤 다시 시도해주세요.";
    }
    if (
      error.code === "DETERMINISTIC_VALIDATION_FAILED" ||
      error.status === 422
    ) {
      return "업무 내용을 안전하게 정리하지 못했습니다. 표현을 조금 바꿔 다시 말씀해주세요.";
    }
    if (error.status === 404) {
      return "이전 대화 정보를 찾지 못해 새로 시작했습니다. 다시 한 번 시도해주세요.";
    }
    if (error.status === 0) return "BY 서버에 연결할 수 없습니다.";
  }
  return "요청을 마치지 못했습니다. 잠시 후 다시 시도해주세요.";
}

function updateMessage(
  messages: ChatMessage[],
  id: string,
  changes: Partial<ChatMessage>,
): ChatMessage[] {
  return messages.map((message) =>
    message.id === id ? { ...message, ...changes } : message,
  );
}

function reportPrompt(
  kind: "daily" | "weekly" | "project",
  projectName?: string,
): string {
  if (kind === "daily") return "오늘 업무보고 만들어줘.";
  if (kind === "weekly") return "이번 주 업무보고 만들어줘.";
  return `${projectName} 프로젝트 보고서 만들어줘.`;
}

export default function Dashboard() {
  const [summary, setSummary] = useState<DashboardSummary>(EMPTY_SUMMARY);
  const [activities, setActivities] = useState<PageResult<ActivityItem>>({
    items: [],
    total: 0,
    limit: 6,
    offset: 0,
  });
  const [projects, setProjects] = useState<PageResult<ProjectListItem>>({
    items: [],
    total: 0,
    limit: 6,
    offset: 0,
  });
  const [reports, setReports] = useState<ReportSnapshot[]>([]);
  const [provider, setProvider] = useState<ProviderStatus>({
    kind: "unknown",
    label: "AI 상태 확인 중",
    state: "UNKNOWN",
  });
  const [dashboardLoading, setDashboardLoading] = useState(true);
  const [dashboardError, setDashboardError] = useState<string | null>(null);
  const [sectionErrors, setSectionErrors] = useState({
    summary: false,
    activities: false,
    projects: false,
    reports: false,
  });
  const [activityPage, setActivityPage] = useState(0);
  const [projectPage, setProjectPage] = useState(0);
  const [selectedProject, setSelectedProject] = useState<ProjectDetail | null>(
    null,
  );
  const [projectLoading, setProjectLoading] = useState(false);
  const [selectedReport, setSelectedReport] = useState<ReportSnapshot | null>(
    null,
  );
  const [reportLoading, setReportLoading] = useState(false);
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([INITIAL_MESSAGE]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [resolvingClarification, setResolvingClarification] = useState<
    string | null
  >(null);
  const [clarificationError, setClarificationError] = useState<string | null>(
    null,
  );
  const [calendarResolution, setCalendarResolution] = useState<string | null>(
    null,
  );
  const [calendarError, setCalendarError] = useState<string | null>(null);
  const [newWorkClarification, setNewWorkClarification] = useState<
    string | null
  >(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [newWorkTitle, setNewWorkTitle] = useState("");
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(
    null,
  );
  const [isStandalone, setIsStandalone] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const activePollRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef(false);
  const clarificationKeysRef = useRef(new Map<string, string>());

  const isChatBusy = messages.some(
    (message) => message.state === "sending" || message.state === "polling",
  );

  const loadDashboard = useCallback(
    async (options?: { quiet?: boolean; activityOffset?: number; projectOffset?: number }) => {
      if (!options?.quiet) setDashboardLoading(true);
      const activityOffset = options?.activityOffset ?? 0;
      const projectOffset = options?.projectOffset ?? 0;
      const results = await Promise.allSettled([
        getSummary(),
        getActivities(6, activityOffset),
        getProjects(6, projectOffset),
        getReports(20),
        getProviderStatus(),
      ]);
      if (results[0].status === "fulfilled") setSummary(results[0].value);
      if (results[1].status === "fulfilled") setActivities(results[1].value);
      if (results[2].status === "fulfilled") setProjects(results[2].value);
      if (results[3].status === "fulfilled") {
        const refreshedReports = results[3].value;
        setReports(refreshedReports);
        setSelectedReport((current) => {
          if (!current) return refreshedReports[0] || null;
          return (
            refreshedReports.find(
              (report) => report.report_id === current.report_id,
            ) || current
          );
        });
      }
      if (results[4].status === "fulfilled") setProvider(results[4].value);

      setSectionErrors({
        summary: results[0].status === "rejected",
        activities: results[1].status === "rejected",
        projects: results[2].status === "rejected",
        reports: results[3].status === "rejected",
      });

      const coreFailed = results.slice(0, 3).every(
        (result) => result.status === "rejected",
      );
      setDashboardError(
        coreFailed ? "현재 업무 현황을 불러오지 못했습니다." : null,
      );
      setDashboardLoading(false);
    },
    [],
  );

  const reloadFromStart = useCallback(() => {
    setActivityPage(0);
    setProjectPage(0);
    return loadDashboard({ activityOffset: 0, projectOffset: 0 });
  }, [loadDashboard]);

  useEffect(() => {
    const installable = (event: Event) => {
      event.preventDefault();
      setInstallPrompt(event as BeforeInstallPromptEvent);
    };
    window.addEventListener("beforeinstallprompt", installable);
    setIsStandalone(
      window.matchMedia("(display-mode: standalone)").matches ||
        ("standalone" in window.navigator &&
          Boolean((window.navigator as Navigator & { standalone?: boolean }).standalone)),
    );
    void navigator.serviceWorker?.register("/sw.js", { scope: "/" });
    const storedConversation = window.localStorage.getItem(
      "jarvis-conversation-id",
    );
    if (storedConversation) setConversationId(storedConversation);
    void loadDashboard();
    return () => {
      activePollRef.current?.abort();
      window.removeEventListener("beforeinstallprompt", installable);
    };
  }, [loadDashboard]);

  const installBy = useCallback(async () => {
    if (!installPrompt) return;
    await installPrompt.prompt();
    await installPrompt.userChoice;
    setInstallPrompt(null);
  }, [installPrompt]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const applyResponse = useCallback(
    (assistantId: string, response: JarvisResponse) => {
      setConversationId(response.conversation_id);
      window.localStorage.setItem(
        "jarvis-conversation-id",
        response.conversation_id,
      );
      const report = response.data?.report || null;
      const calendarData = response.data?.calendar;
      const calendarProposal =
        calendarData &&
        typeof calendarData === "object" &&
        "proposal" in calendarData &&
        calendarData.proposal &&
        typeof calendarData.proposal === "object"
          ? (calendarData.proposal as CalendarProposal)
          : null;
      setMessages((current) =>
        updateMessage(current, assistantId, {
          state: "complete",
          content: report
            ? `${REPORT_LABEL[report.report_type] || "업무보고"}를 정리했습니다.`
            : response.display_response,
          clarification: response.clarification || null,
          calendarProposal,
          report,
          audioUrl: response.audio_url || null,
        }),
      );
      if (report) setSelectedReport(report);
      void loadDashboard({ quiet: true, activityOffset: 0, projectOffset: 0 });
      if (selectedProject?.id) {
        void getProject(selectedProject.id)
          .then(setSelectedProject)
          .catch(() => setSelectedProject(null));
      }
      setActivityPage(0);
      setProjectPage(0);
    },
    [loadDashboard, selectedProject?.id],
  );

  const sendMessage = useCallback(
    async (
      rawContent: string,
      retry?: { assistantId: string; clientMessageId: string },
    ) => {
      const content = rawContent.trim();
      if (!content || isChatBusy || inFlightRef.current) return;
      inFlightRef.current = true;
      const clientMessageId = retry?.clientMessageId || createId("message");
      const assistantId = retry?.assistantId || createId("assistant");

      setProvider((current) =>
        current.kind === "local" || current.kind === "api"
          ? { ...current, state: "LOADING" }
          : current,
      );

      if (retry) {
        setMessages((current) =>
          updateMessage(current, assistantId, {
            content: "BY가 업무 내용을 다시 확인하고 있습니다…",
            state: "sending",
          }),
        );
      } else {
        const userMessage: ChatMessage = {
          id: createId("user"),
          role: "user",
          content,
          state: "complete",
        };
        const pendingMessage: ChatMessage = {
          id: assistantId,
          role: "assistant",
          content: "BY가 업무 내용을 정리하고 있습니다…",
          state: "sending",
          retryContent: content,
          clientMessageId,
        };
      setMessages((current) => [...current, userMessage, pendingMessage]);
      setInput("");
      }

      try {
        let result = null;
        let currentConversationId = conversationId;
        let retriedWithNewConversation = false;
        for (let attempt = 0; ; attempt += 1) {
          const retryDelays = [700, 1400, 2800, 5600, 10000, 15000];
          try {
            result = await createChatRun({
              content,
              conversationId: currentConversationId,
              clientMessageId,
            });
            break;
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status === 503 &&
          !error.code &&
          attempt < 5
        ) {
          await sleep(retryDelays[attempt]);
          continue;
        }
        if (
          error instanceof ApiError &&
          error.status === 404 &&
          !retriedWithNewConversation
        ) {
              retriedWithNewConversation = true;
              currentConversationId = null;
              setConversationId(null);
              window.localStorage.removeItem("jarvis-conversation-id");
              await sleep(0);
              continue;
            }
            if (
              attempt < 5 &&
              error instanceof ApiError &&
              (error.code === "EXTRACTION_CONCURRENCY_EXCEEDED" ||
                error.code === "DATABASE_BUSY" ||
                error.code === "EXTRACTION_TIMEOUT" ||
                error.code === "REQUEST_TIMEOUT" ||
                error.code === "NETWORK_ERROR" ||
                error.code === "BACKEND_PROXY_ERROR" ||
                error.status === 502)
            ) {
              await sleep(retryDelays[attempt]);
              continue;
            }
            throw error;
          }
        }

        if (result.kind === "complete") {
          applyResponse(assistantId, result.response);
          return;
        }

        setMessages((current) =>
          updateMessage(current, assistantId, {
            state: "polling",
          content: "BY가 업무 내용을 정리하고 있습니다…",
          }),
        );
        activePollRef.current?.abort();
        const controller = new AbortController();
        activePollRef.current = controller;
        const response = await pollRun({
          getRun,
          statusUrl: result.statusUrl,
          signal: controller.signal,
        });
        applyResponse(assistantId, response);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setMessages((current) =>
          updateMessage(current, assistantId, {
            content: friendlyError(error),
            state: "error",
          }),
        );
        void getProviderStatus().then(setProvider).catch(() => {
          setProvider((current) => ({ ...current, state: "ERROR" }));
        });
      } finally {
        inFlightRef.current = false;
      }
    },
    [applyResponse, conversationId, isChatBusy],
  );

  function submitChat(event: FormEvent) {
    event.preventDefault();
    void sendMessage(input);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (input.trim()) void sendMessage(input);
    }
  }

  function stableClarificationKey(actionKey: string): string {
    const existing = clarificationKeysRef.current.get(actionKey);
    if (existing) return existing;
    const created = createId("clarification");
    clarificationKeysRef.current.set(actionKey, created);
    return created;
  }

  async function resolveChoice(
    messageId: string,
    clarification: Clarification,
    input: {
      action: "SELECT_EXISTING" | "CREATE_NEW";
      workItemId?: string;
      projectName?: string;
      workItemTitle?: string;
    },
  ) {
    const actionKey = [
      clarification.clarification_id,
      input.action,
      input.workItemId || input.projectName,
      input.workItemTitle,
    ].join(":");
    setResolvingClarification(actionKey);
    setClarificationError(null);
    try {
      const response = await resolveClarification({
        clarificationId: clarification.clarification_id,
        ...input,
        idempotencyKey: stableClarificationKey(actionKey),
      });
      setNewWorkClarification(null);
      setNewProjectName("");
      setNewWorkTitle("");
      applyResponse(messageId, response);
    } catch (error) {
      setClarificationError(friendlyError(error));
    } finally {
      setResolvingClarification(null);
    }
  }

  async function resolveCalendar(
    messageId: string,
    proposal: CalendarProposal,
    action: "APPROVE" | "REJECT",
  ) {
    const actionKey = `calendar:${proposal.proposal_id}:${action}`;
    setCalendarResolution(actionKey);
    setCalendarError(null);
    try {
      const result = await resolveCalendarProposal({
        proposalId: proposal.proposal_id,
        action,
        expectedVersion: proposal.version,
        idempotencyKey: stableClarificationKey(actionKey),
      });
      setMessages((current) =>
        updateMessage(current, messageId, {
          content: result.display_response,
          calendarProposal: null,
        }),
      );
    } catch (error) {
      setCalendarError(friendlyError(error));
    } finally {
      setCalendarResolution(null);
    }
  }

  async function openProject(project: ProjectListItem) {
    setProjectLoading(true);
    try {
      setSelectedProject(await getProject(project.id));
    } catch {
      setDashboardError("프로젝트 상세를 불러오지 못했습니다.");
    } finally {
      setProjectLoading(false);
    }
  }

  async function openReport(report: ReportSnapshot) {
    setReportLoading(true);
    try {
      setSelectedReport(await getReport(report.report_id));
    } catch {
      setDashboardError("보고서를 불러오지 못했습니다.");
    } finally {
      setReportLoading(false);
    }
  }

  function createRangeReport(event: FormEvent) {
    event.preventDefault();
    if (!rangeStart || !rangeEnd || rangeStart > rangeEnd) return;
    void sendMessage(`${rangeStart}부터 ${rangeEnd}까지 업무보고 만들어줘.`);
  }

  const providerCopy = useMemo(() => {
    const sourceLabel =
      provider.kind === "api"
        ? "API AI"
        : provider.kind === "local"
          ? "Local AI"
          : provider.label;
    if (provider.state === "LOADING") return `${sourceLabel}에 연결 중`;
    if (["ERROR", "UNAVAILABLE"].includes(provider.state)) {
      return `${sourceLabel} 연결에 실패했습니다`;
    }
    if (provider.state === "DEGRADED") {
      return provider.message || `${sourceLabel} 상태를 확인해주세요`;
    }
    return [provider.label, provider.model].filter(Boolean).join(" · ");
  }, [provider]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#jarvis-chat" aria-label="BY 대화로 이동">
          <span className="brand-mark" aria-hidden="true">BY</span>
          <span>
            <strong>BY</strong>
            <small>나의 AI 업무 매니저</small>
          </span>
        </a>
        <nav className="topnav" aria-label="주요 메뉴">
          <a href="#now">지금의 업무</a>
          <a href="#projects">프로젝트</a>
          <a href="#reports">보고서</a>
        </nav>
        <div className="topbar-tools">
          {installPrompt && !isStandalone && (
            <button className="install-button" type="button" onClick={() => void installBy()}>
              앱으로 설치
            </button>
          )}
          <div className={`provider-status provider-${provider.state.toLowerCase()}`}>
            <span aria-hidden="true" />
            {providerCopy}
          </div>
        </div>
      </header>

      {dashboardError && (
        <div className="global-alert" role="alert">
          <span>{dashboardError}</span>
          <button type="button" onClick={() => void reloadFromStart()}>
            다시 불러오기
          </button>
        </div>
      )}

      <section className="dashboard-grid" aria-label="BY 업무 대시보드">
        <section className="chat-panel" id="jarvis-chat">
          <div className="panel-heading chat-heading">
            <div>
              <span className="eyebrow">BY CHAT</span>
              <h1>무엇을 정리해드릴까요?</h1>
            </div>
            <span className="private-note">이 공간의 대화는 나만 볼 수 있어요</span>
          </div>

          <div className="message-list" aria-live="polite" aria-label="BY 대화">
            {messages.map((message) => (
              <ChatBubble
                key={message.id}
                message={message}
                busy={resolvingClarification}
                clarificationError={clarificationError}
                calendarBusy={calendarResolution}
                calendarError={calendarError}
                newWorkClarification={newWorkClarification}
                newProjectName={newProjectName}
                newWorkTitle={newWorkTitle}
                onOpenNewWork={(id) => {
                  setNewWorkClarification(id);
                  setClarificationError(null);
                }}
                onProjectName={setNewProjectName}
                onWorkTitle={setNewWorkTitle}
                onResolve={(clarification, choice) =>
                  void resolveChoice(message.id, clarification, choice)
                }
                onResolveCalendar={(proposal, action) =>
                  void resolveCalendar(message.id, proposal, action)
                }
                onRetry={() => {
                  if (message.retryContent && message.clientMessageId) {
                    void sendMessage(message.retryContent, {
                      assistantId: message.id,
                      clientMessageId: message.clientMessageId,
                    });
                  }
                }}
              />
            ))}
            <div ref={chatEndRef} />
          </div>

          <div className="quick-prompts" aria-label="빠른 질문">
            {QUICK_PROMPTS.map((prompt) => (
              <button
                type="button"
                key={prompt}
                disabled={isChatBusy}
                onClick={() => void sendMessage(prompt)}
              >
                {prompt}
              </button>
            ))}
          </div>

          <form className="composer" onSubmit={submitChat}>
            <label className="sr-only" htmlFor="jarvis-input">
              업무 내용이나 질문 입력
            </label>
            <textarea
              id="jarvis-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleComposerKeyDown}
              placeholder="오늘 한 일이나 궁금한 업무를 편하게 말씀해주세요"
              rows={2}
              maxLength={10000}
            />
            <button
              className="send-button"
              type="submit"
              disabled={!input.trim() || isChatBusy}
              aria-label="BY에게 보내기"
            >
              <span>보내기</span>
              <b aria-hidden="true">↗</b>
            </button>
          </form>
          <p className="composer-hint">Enter로 보내기 · Shift + Enter로 줄바꿈</p>
        </section>

        <aside className="context-rail" id="now">
          <section className="now-card">
            <div className="section-title-row">
              <div>
                <span className="eyebrow">RIGHT NOW</span>
                <h2>지금의 업무</h2>
              </div>
              <button
                className="text-button"
                type="button"
                onClick={() => void reloadFromStart()}
              >
                새로고침
              </button>
            </div>
            <div className="status-counts" aria-label="업무 상태 요약">
              <StatusCount label="진행 중" count={summary.currentWork.length} tone="active" />
              <StatusCount label="회신 대기" count={summary.waiting.length} tone="waiting" />
              <StatusCount label="막힘" count={summary.blocked.length} tone="blocked" />
            </div>
            <div className="now-work-list">
              {sectionErrors.summary ? (
                <SectionError onRetry={() => void reloadFromStart()} />
              ) : dashboardLoading ? (
                <LoadingRows count={2} />
              ) : summary.currentWork.length ? (
                summary.currentWork.slice(0, 2).map((item, index) => (
                  <WorkCard key={item.workItemId || `${item.title}-${index}`} item={item} />
                ))
              ) : (
                <EmptyState compact text="현재 진행 중인 업무가 없습니다." />
              )}
            </div>
            {(summary.waiting.length > 0 || summary.blocked.length > 0) && (
              <div className="exception-preview">
                {summary.waiting.slice(0, 1).map((item, index) => (
                  <CompactStateRow
                    key={item.workItemId || `waiting-${index}`}
                    item={item}
                    label="Waiting"
                  />
                ))}
                {summary.blocked.slice(0, 1).map((item, index) => (
                  <CompactStateRow
                    key={item.workItemId || `blocked-${index}`}
                    item={item}
                    label="Blocked"
                  />
                ))}
              </div>
            )}
          </section>

          <section className="next-card">
            <div className="section-title-row">
              <div>
                <span className="eyebrow">NEXT</span>
                <h2>다음에 할 일</h2>
              </div>
              <button
                className="ask-link"
                type="button"
                onClick={() => void sendMessage("지금 뭐부터 해야 돼?")}
              >
                BY에게 묻기
              </button>
            </div>
            {sectionErrors.summary ? (
              <SectionError onRetry={() => void reloadFromStart()} />
            ) : dashboardLoading ? (
              <LoadingRows count={1} />
            ) : summary.nextActions.length ? (
              <ol className="next-list">
                {summary.nextActions.slice(0, 3).map((item, index) => (
                  <li key={item.workItemId || `${item.title}-${index}`}>
                    <span className="rank">{index + 1}</span>
                    <div>
                      <strong>{item.nextAction || item.title}</strong>
                      <p>{item.projectName} · {item.title}</p>
                      {item.reason && <small>{item.reason}</small>}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState compact text="추천할 수 있는 다음 업무가 없습니다." />
            )}
          </section>

          <section className="activity-card">
            <div className="section-title-row">
              <div>
                <span className="eyebrow">MEMORY</span>
                <h2>최근 업무 기록</h2>
              </div>
              <span className="result-count">{activities.total}건</span>
            </div>
            {sectionErrors.activities ? (
              <SectionError onRetry={() => void reloadFromStart()} />
            ) : dashboardLoading ? (
              <LoadingRows count={3} />
            ) : activities.items.length ? (
              <div className="activity-timeline">
                {activities.items.map((activity, index) => (
                  <ActivityRow key={activity.id || `${activity.summary}-${index}`} item={activity} />
                ))}
              </div>
            ) : (
              <EmptyState compact text="아직 기록된 활동이 없습니다." />
            )}
            <Pagination
              page={activityPage}
              pageSize={6}
              total={activities.total}
              onPage={(page) => {
                setActivityPage(page);
                void loadDashboard({
                  quiet: true,
                  activityOffset: page * 6,
                  projectOffset: projectPage * 6,
                });
              }}
            />
          </section>
        </aside>
      </section>

      <section className="lower-grid">
        <section className="projects-section" id="projects">
          <div className="section-heading">
            <div>
              <span className="eyebrow">PROJECTS</span>
              <h2>진행 흐름 살펴보기</h2>
              <p>복잡한 보드 없이, 지금 필요한 맥락만 확인합니다.</p>
            </div>
          </div>
          <div className="project-browser">
            <div className="project-list" aria-label="프로젝트 목록">
              {sectionErrors.projects ? (
                <SectionError onRetry={() => void reloadFromStart()} />
              ) : dashboardLoading ? (
                <LoadingRows count={3} />
              ) : projects.items.length ? (
                projects.items.map((project) => (
                  <button
                    type="button"
                    className={selectedProject?.id === project.id ? "selected" : ""}
                    key={project.id}
                    onClick={() => void openProject(project)}
                  >
                    <span className="project-initial" aria-hidden="true">
                      {project.name.slice(0, 1)}
                    </span>
                    <span>
                      <strong>{project.name}</strong>
                      <small>활성 업무 {project.activeCount}개</small>
                    </span>
                    <b aria-hidden="true">›</b>
                  </button>
                ))
              ) : (
                <EmptyState text="업무를 기록하면 프로젝트가 여기에 나타납니다." />
              )}
              <Pagination
                page={projectPage}
                pageSize={6}
                total={projects.total}
                onPage={(page) => {
                  setProjectPage(page);
                  void loadDashboard({
                    quiet: true,
                    activityOffset: activityPage * 6,
                    projectOffset: page * 6,
                  });
                }}
              />
            </div>
            <ProjectDetailView project={selectedProject} loading={projectLoading} />
          </div>
        </section>

        <section className="reports-section" id="reports">
          <div className="section-heading report-heading">
            <div>
              <span className="eyebrow">REPORTS</span>
              <h2>기록을 보고서로 정리하기</h2>
              <p>BY가 같은 업무의 흐름을 모아 읽기 쉽게 정리합니다.</p>
            </div>
            <div className="report-actions">
              <button type="button" onClick={() => void sendMessage(reportPrompt("daily"))}>
                오늘
              </button>
              <button type="button" onClick={() => void sendMessage(reportPrompt("weekly"))}>
                이번 주
              </button>
              <select
                aria-label="프로젝트 보고서 만들 프로젝트"
                defaultValue=""
                onChange={(event) => {
                  if (event.target.value) void sendMessage(reportPrompt("project", event.target.value));
                  event.target.value = "";
                }}
              >
                <option value="" disabled>프로젝트 보고</option>
                {projects.items.map((project) => (
                  <option value={project.name} key={project.id}>{project.name}</option>
                ))}
              </select>
            </div>
          </div>
          <form className="range-form" onSubmit={createRangeReport}>
            <span>기간별 보고</span>
            <label>
              <span className="sr-only">시작일</span>
              <input type="date" value={rangeStart} onChange={(event) => setRangeStart(event.target.value)} />
            </label>
            <i>–</i>
            <label>
              <span className="sr-only">종료일</span>
              <input type="date" value={rangeEnd} onChange={(event) => setRangeEnd(event.target.value)} />
            </label>
            <button type="submit" disabled={!rangeStart || !rangeEnd || rangeStart > rangeEnd}>
              보고서 만들기
            </button>
          </form>
          <div className="report-browser">
            <div className="report-history" aria-label="최근 보고서">
              <h3>최근 보고서</h3>
              {sectionErrors.reports ? (
                <SectionError onRetry={() => void reloadFromStart()} />
              ) : reports.length ? reports.slice(0, 8).map((report) => (
                <button
                  type="button"
                  key={report.report_id}
                  className={selectedReport?.report_id === report.report_id ? "selected" : ""}
                  onClick={() => void openReport(report)}
                >
                  <span>
                    <strong>{REPORT_LABEL[report.report_type] || "업무보고"}</strong>
                    <small>{formatPeriod(report)}</small>
                  </span>
                  {report.freshness === "STALE" && <em>업데이트 필요</em>}
                </button>
              )) : <EmptyState compact text="아직 만든 보고서가 없습니다." />}
            </div>
            <ReportDocument report={selectedReport} loading={reportLoading} />
          </div>
        </section>
      </section>

      <footer>
        <span>BY</span>
        <p>Structured Memory를 기준으로 현재 업무를 보여드립니다.</p>
      </footer>
    </main>
  );
}

function ChatBubble({
  message,
  busy,
  clarificationError,
  calendarBusy,
  calendarError,
  newWorkClarification,
  newProjectName,
  newWorkTitle,
  onOpenNewWork,
  onProjectName,
  onWorkTitle,
  onResolve,
  onResolveCalendar,
  onRetry,
}: {
  message: ChatMessage;
  busy: string | null;
  clarificationError: string | null;
  calendarBusy: string | null;
  calendarError: string | null;
  newWorkClarification: string | null;
  newProjectName: string;
  newWorkTitle: string;
  onOpenNewWork: (clarificationId: string) => void;
  onProjectName: (value: string) => void;
  onWorkTitle: (value: string) => void;
  onResolve: (
    clarification: Clarification,
    choice: {
      action: "SELECT_EXISTING" | "CREATE_NEW";
      workItemId?: string;
      projectName?: string;
      workItemTitle?: string;
    },
  ) => void;
  onResolveCalendar: (
    proposal: CalendarProposal,
    action: "APPROVE" | "REJECT",
  ) => void;
  onRetry: () => void;
}) {
  const isPending = message.state === "sending" || message.state === "polling";
  return (
    <article className={`message message-${message.role}`}>
      {message.role === "assistant" && <span className="assistant-avatar">J</span>}
      <div className="bubble-wrap">
        <span className="message-author">
          {message.role === "assistant" ? "BY" : "나"}
        </span>
        <div className={`message-bubble ${isPending ? "is-pending" : ""}`}>
          {isPending && <span className="thinking-dot" aria-hidden="true" />}
          <p>{message.content}</p>
          {message.audioUrl && (
            <audio
              className="message-audio"
              controls
              preload="none"
              src={message.audioUrl}
              aria-label="BY 음성 응답"
            >
              <track
                kind="captions"
                srcLang="ko"
                label="한국어"
                default
                src={`data:text/vtt;charset=utf-8,${encodeURIComponent(
                  `WEBVTT\\n\\n00:00.000 --> 99:59.000\\n${message.content}`,
                )}`}
              />
            </audio>
          )}
          {message.state === "error" && (
            <button className="retry-button" type="button" onClick={onRetry}>
              같은 요청으로 다시 시도
            </button>
          )}
          {message.clarification && (
            <div className="clarification-box">
              <strong>{message.clarification.question}</strong>
              <div className="clarification-options">
                {message.clarification.candidates.map((candidate) => {
                  const key = `${message.clarification?.clarification_id}:SELECT_EXISTING:${candidate.work_item_id}:`;
                  return (
                    <button
                      type="button"
                      key={candidate.work_item_id}
                      disabled={Boolean(busy)}
                      onClick={() =>
                        onResolve(message.clarification!, {
                          action: "SELECT_EXISTING",
                          workItemId: candidate.work_item_id,
                        })
                      }
                    >
                      <span>{candidate.project_name}</span>
                      <strong>{candidate.work_item_title}</strong>
                      {candidate.waiting_for && <small>{candidate.waiting_for}</small>}
                      {busy === key && <i>반영 중…</i>}
                    </button>
                  );
                })}
                <button
                  type="button"
                  className="new-work-button"
                  disabled={Boolean(busy)}
                  onClick={() => onOpenNewWork(message.clarification!.clarification_id)}
                >
                  <span aria-hidden="true">＋</span>
                  <strong>새로운 업무 / 다른 업무</strong>
                  <small>업무를 직접 지정합니다</small>
                </button>
              </div>
              {newWorkClarification === message.clarification.clarification_id && (
                <div className="new-work-form">
                  <label>
                    <span>프로젝트</span>
                    <input
                      value={newProjectName}
                      onChange={(event) => onProjectName(event.target.value)}
                      placeholder="예: 예측매니저"
                      maxLength={200}
                    />
                  </label>
                  <label>
                    <span>업무</span>
                    <input
                      value={newWorkTitle}
                      onChange={(event) => onWorkTitle(event.target.value)}
                      placeholder="예: 로그인 제거 문의"
                      maxLength={300}
                    />
                  </label>
                  <button
                    type="button"
                    disabled={!newProjectName.trim() || !newWorkTitle.trim() || Boolean(busy)}
                    onClick={() =>
                      onResolve(message.clarification!, {
                        action: "CREATE_NEW",
                        projectName: newProjectName.trim(),
                        workItemTitle: newWorkTitle.trim(),
                      })
                    }
                  >
                    이 업무로 이어가기
                  </button>
                </div>
              )}
              {clarificationError && <p className="inline-error" role="alert">{clarificationError}</p>}
            </div>
          )}
          {message.calendarProposal && (
            <div className="calendar-approval-box">
              <span>Google Calendar</span>
              <strong>{message.calendarProposal.title}</strong>
              <small>{formatCalendarTime(message.calendarProposal.start_at)}</small>
              <div>
                <button
                  type="button"
                  disabled={Boolean(calendarBusy)}
                  onClick={() =>
                    onResolveCalendar(message.calendarProposal!, "APPROVE")
                  }
                >
                  {calendarBusy ===
                  `calendar:${message.calendarProposal.proposal_id}:APPROVE`
                    ? "등록 중…"
                    : "일정 등록"}
                </button>
                <button
                  type="button"
                  className="calendar-reject-button"
                  disabled={Boolean(calendarBusy)}
                  onClick={() =>
                    onResolveCalendar(message.calendarProposal!, "REJECT")
                  }
                >
                  취소
                </button>
              </div>
              {calendarError && (
                <p className="inline-error" role="alert">{calendarError}</p>
              )}
            </div>
          )}
          {message.report && <ReportDocument report={message.report} compact />}
        </div>
      </div>
    </article>
  );
}

function formatCalendarTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "long",
    day: "numeric",
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

function StatusCount({ label, count, tone }: { label: string; count: number; tone: string }) {
  return (
    <div className={`status-count tone-${tone}`}>
      <span>{label}</span>
      <strong>{count}</strong>
    </div>
  );
}

function WorkCard({ item }: { item: WorkItemSummary }) {
  return (
    <article className="work-card">
      <div className="work-card-top">
        <span>{item.projectName}</span>
        <StatusPill status={item.status} />
      </div>
      <h3>{item.title}</h3>
      {(item.nextAction || item.waitingFor || item.blockedReason) && (
        <div className="work-next">
          <small>
            {item.blockedReason ? "막힌 이유" : item.waitingFor ? "기다리는 내용" : "다음 작업"}
          </small>
          <p>{item.blockedReason || item.waitingFor || item.nextAction}</p>
        </div>
      )}
    </article>
  );
}

function StatusPill({ status }: { status: WorkStatus }) {
  return <span className={`status-pill status-${status.toLowerCase()}`}>{STATUS_LABEL[status]}</span>;
}

function CompactStateRow({
  item,
  label,
}: {
  item: WorkItemSummary;
  label: "Waiting" | "Blocked";
}) {
  const description =
    label === "Waiting" ? item.waitingFor : item.blockedReason;
  return (
    <article className={`compact-state compact-${label.toLowerCase()}`}>
      <span>{label}</span>
      <div>
        <strong>{item.title}</strong>
        <small>{item.projectName}{description ? ` · ${description}` : ""}</small>
      </div>
    </article>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  return (
    <article className="activity-row">
      <time>{formatActivityTime(item)}</time>
      <span className="timeline-mark" aria-hidden="true" />
      <div>
        <small>{item.projectName}</small>
        <p>{item.summary}</p>
      </div>
    </article>
  );
}

function ProjectDetailView({ project, loading }: { project: ProjectDetail | null; loading: boolean }) {
  if (loading) return <div className="project-detail"><LoadingRows count={4} /></div>;
  if (!project) {
    return (
      <div className="project-detail project-placeholder">
        <span className="placeholder-orbit" aria-hidden="true">J</span>
        <h3>프로젝트를 선택해주세요</h3>
        <p>진행 중인 업무와 최근 기록을 한눈에 볼 수 있습니다.</p>
      </div>
    );
  }
  return (
    <div className="project-detail">
      <div className="project-detail-head">
        <div>
          <span className="eyebrow">PROJECT FOCUS</span>
          <h3>{project.name}</h3>
        </div>
        <span>활성 업무 {project.currentWork.length}개</span>
      </div>
      <div className="project-columns">
        <div>
          <h4>현재 업무</h4>
          {project.currentWork.length ? project.currentWork.slice(0, 4).map((item, index) => (
            <div className="detail-work" key={item.workItemId || `${item.title}-${index}`}>
              <StatusPill status={item.status} />
              <strong>{item.title}</strong>
              {item.nextAction && <small>다음: {item.nextAction}</small>}
            </div>
          )) : <EmptyState compact text="진행 중인 업무가 없습니다." />}
        </div>
        <div>
          <h4>최근 활동</h4>
          {project.recentActivity.length ? project.recentActivity.slice(0, 4).map((item, index) => (
            <div className="mini-activity" key={item.id || `${item.summary}-${index}`}>
              <span aria-hidden="true" />
              <div><strong>{item.summary}</strong><small>{formatActivityTime(item)}</small></div>
            </div>
          )) : <EmptyState compact text="최근 활동이 없습니다." />}
        </div>
      </div>
      {project.completedWork.length > 0 && (
        <div className="completed-strip">
          <strong>최근 완료</strong>
          <span>{project.completedWork.slice(0, 3).map((item) => item.title).join(" · ")}</span>
        </div>
      )}
    </div>
  );
}

function ReportDocument({
  report,
  loading = false,
  compact = false,
}: {
  report: ReportSnapshot | null;
  loading?: boolean;
  compact?: boolean;
}) {
  if (loading) return <article className="report-document"><LoadingRows count={5} /></article>;
  if (!report) {
    return (
      <article className="report-document report-placeholder">
        <span aria-hidden="true">✦</span>
        <h3>업무 흐름을 보고서로 모아보세요</h3>
        <p>오늘, 이번 주, 프로젝트 또는 원하는 기간을 선택할 수 있습니다.</p>
      </article>
    );
  }
  return (
    <article className={`report-document ${compact ? "report-compact" : ""}`}>
      {report.freshness === "STALE" && (
        <div className="stale-warning" role="status">
          일부 업무 기록이 수정되어 이 보고서는 최신 상태가 아닙니다.
        </div>
      )}
      <header>
        <span className="eyebrow">{REPORT_LABEL[report.report_type] || "업무보고"}</span>
        <h3>{report.project?.project_name || report.project?.name || REPORT_LABEL[report.report_type] || "업무보고"}</h3>
        <time>{formatPeriod(report)}</time>
      </header>
      <div className="report-sections">
        {REPORT_SECTIONS.map(([key, label]) => {
          const items = report.sections?.[key] || [];
          return (
            <section key={key}>
              <h4>{label}</h4>
              {items.length ? (
                <ul>{items.map((item, index) => <li key={`${key}-${index}`}>{item.text}</li>)}</ul>
              ) : (
                <p className="report-empty">해당 내용이 없습니다.</p>
              )}
            </section>
          );
        })}
      </div>
    </article>
  );
}

function Pagination({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
}) {
  if (total <= pageSize) return null;
  const pages = Math.ceil(total / pageSize);
  return (
    <div className="pagination" aria-label="페이지 이동">
      <button type="button" disabled={page === 0} onClick={() => onPage(page - 1)}>이전</button>
      <span>{page + 1} / {pages}</span>
      <button type="button" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>다음</button>
    </div>
  );
}

function LoadingRows({ count }: { count: number }) {
  return (
    <div className="loading-rows" role="status" aria-label="불러오는 중">
      {Array.from({ length: count }, (_, index) => <span key={index} />)}
    </div>
  );
}

function EmptyState({ text, compact = false }: { text: string; compact?: boolean }) {
  return <div className={`empty-state ${compact ? "empty-compact" : ""}`}><span aria-hidden="true">·</span><p>{text}</p></div>;
}

function SectionError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="section-error" role="alert">
      <p>내용을 불러오지 못했습니다.</p>
      <button type="button" onClick={onRetry}>다시 불러오기</button>
    </div>
  );
}

function formatActivityTime(item: ActivityItem): string {
  if (!item.occurredAt) return item.occurredOn || "최근";
  const date = new Date(item.occurredAt);
  if (Number.isNaN(date.getTime())) return item.occurredOn || "최근";
  const now = new Date();
  const sameDay = date.toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" }) === now.toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" });
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const dayLabel = sameDay
    ? "오늘"
    : date.toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" }) === yesterday.toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" })
      ? "어제"
      : date.toLocaleDateString("ko-KR", { month: "short", day: "numeric", timeZone: "Asia/Seoul" });
  return `${dayLabel} ${date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Seoul" })}`;
}

function formatPeriod(report: ReportSnapshot): string {
  const start = report.period?.start_date || "";
  const end = report.period?.end_date || start;
  return start === end ? start : `${start} – ${end}`;
}
