import type {
  ChatMessage,
  ConversationListItem,
  ConversationMessage,
  JarvisResponse,
} from "./types";

export const WELCOME_MESSAGE: ChatMessage = {
  id: "by-welcome",
  role: "assistant",
  state: "complete",
  content:
    "안녕하세요. 오늘 한 일, 기다리는 답변, 다음에 해야 할 업무를 편하게 말씀해주세요.",
};

export function conversationStorageKey(userId: string): string {
  return `by.active-conversation.${userId}`;
}

type ConversationHintStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function readConversationHint(
  storage: ConversationHintStorage,
  userId: string,
): string | null {
  return storage.getItem(conversationStorageKey(userId));
}

export function storeConversationHint(
  storage: ConversationHintStorage,
  userId: string,
  conversationId: string,
): void {
  storage.setItem(conversationStorageKey(userId), conversationId);
}

export function clearConversationHint(
  storage: ConversationHintStorage,
  userId: string,
): void {
  storage.removeItem(conversationStorageKey(userId));
}

export function chooseConversationId(
  storedConversationId: string | null,
  conversations: ConversationListItem[],
): string | null {
  if (
    storedConversationId &&
    conversations.some((conversation) => conversation.id === storedConversationId)
  ) {
    return storedConversationId;
  }
  return conversations[0]?.id ?? null;
}

function calendarProposal(response: JarvisResponse) {
  const calendar = response.data?.calendar;
  if (
    calendar &&
    typeof calendar === "object" &&
    "proposal" in calendar &&
    calendar.proposal &&
    typeof calendar.proposal === "object"
  ) {
    return calendar.proposal as ChatMessage["calendarProposal"];
  }
  return null;
}

/**
 * Convert server-owned history to render state. The browser never persists
 * message bodies; localStorage only keeps a user-scoped conversation hint.
 */
export function historyToChatMessages(
  source: ConversationMessage[],
): ChatMessage[] {
  const ordered = source
    .map((message, sourceIndex) => ({ message, sourceIndex }))
    .sort((left, right) => {
      const leftCursor = left.message.sequence_cursor;
      const rightCursor = right.message.sequence_cursor;
      if (typeof leftCursor === "number" && typeof rightCursor === "number") {
        return leftCursor - rightCursor;
      }
      if (left.message.server_sequence !== right.message.server_sequence) {
        return left.message.server_sequence - right.message.server_sequence;
      }
      if (left.message.role !== right.message.role) {
        return left.message.role === "user" ? -1 : 1;
      }
      return left.sourceIndex - right.sourceIndex;
    })
    .map(({ message }) => message)
    .filter(
      (message, index, items) =>
        items.findIndex((candidate) => candidate.id === message.id) === index,
    );

  if (ordered.length === 0) return [{ ...WELCOME_MESSAGE }];

  return ordered.map((message) => {
    const response = message.role === "assistant" ? message.response : null;
    return {
      id: `history-${message.id}`,
      role: message.role,
      content: message.content,
      state: "complete",
      clarification: response?.clarification ?? null,
      calendarProposal: response ? calendarProposal(response) : null,
      report: response?.data?.report ?? null,
      audioUrl: response?.audio_url ?? null,
    } satisfies ChatMessage;
  });
}

const PENDING_RUN_STATUSES = new Set([
  "RUN_IN_PROGRESS",
  "RECEIVED",
  "INTERPRETING",
  "PLANNED",
  "APPLYING",
]);

export interface RestorableRun {
  runId: string;
  statusUrl: string;
  content: string;
  clientMessageId: string | null;
  failed: boolean;
}

export function findRestorableRun(
  source: ConversationMessage[],
): RestorableRun | null {
  const latestUserRun = [...source]
    .filter((message) => message.role === "user" && Boolean(message.run_id))
    .sort((left, right) => {
      const leftCursor = left.sequence_cursor ?? left.server_sequence * 2;
      const rightCursor = right.sequence_cursor ?? right.server_sequence * 2;
      return rightCursor - leftCursor;
    })[0];

  if (!latestUserRun?.run_id || !latestUserRun.run_status) return null;
  const normalizedStatus = latestUserRun.run_status.toUpperCase();
  if (
    !PENDING_RUN_STATUSES.has(normalizedStatus) &&
    normalizedStatus !== "FAILED" &&
    normalizedStatus !== "INTERRUPTED_RETRYABLE"
  ) {
    return null;
  }

  const hasAssistantResult = source.some((message) => {
    if (message.role !== "assistant") return false;
    if (message.run_id && message.run_id === latestUserRun.run_id) return true;
    return message.server_sequence === latestUserRun.server_sequence;
  });
  if (hasAssistantResult) return null;

  return {
    runId: latestUserRun.run_id,
    statusUrl:
      latestUserRun.status_url || `/api/v1/runs/${latestUserRun.run_id}`,
    content: latestUserRun.content,
    clientMessageId: latestUserRun.client_message_id || null,
    failed: ["FAILED", "INTERRUPTED_RETRYABLE"].includes(normalizedStatus),
  };
}
