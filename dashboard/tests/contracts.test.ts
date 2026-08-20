import assert from "node:assert/strict";
import test from "node:test";
import {
  ApiError,
  changePassword,
  createConversation,
  getConversationHistory,
  login,
  logout,
  logoutAll,
  resetPassword,
  rotateRecoveryCode,
} from "../lib/api";
import {
  WELCOME_MESSAGE,
  chooseConversationId,
  clearConversationHint,
  conversationStorageKey,
  findRestorableRun,
  historyToChatMessages,
  readConversationHint,
  storeConversationHint,
} from "../lib/chat-history";
import type { ConversationMessage } from "../lib/types";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("all auth requests include credentials and logout is a POST", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), init });
    if (String(input).endsWith("/auth/login")) {
      return jsonResponse({
        user: {
          id: "user-a",
          username: "alpha",
          display_name: "Alpha",
          timezone: "Asia/Seoul",
          locale: "ko-KR",
        },
      });
    }
    return new Response(null, { status: 204 });
  };

  try {
    const user = await login({ username: "alpha", password: "long-password" });
    await logout();
    assert.equal(user.id, "user-a");
    assert.equal(calls.length, 2);
    assert.ok(calls.every((call) => call.init?.credentials === "include"));
    assert.equal(calls[1].init?.method, "POST");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("conversation hint is user-scoped and unavailable hints choose latest", () => {
  assert.notEqual(conversationStorageKey("user-a"), conversationStorageKey("user-b"));
  const conversations = [
    {
      id: "latest",
      created_at: "2026-08-20T09:00:00Z",
      updated_at: "2026-08-20T10:00:00Z",
      message_count: 2,
      last_message_preview: "최근 대화",
    },
    {
      id: "older",
      created_at: "2026-08-19T09:00:00Z",
      updated_at: "2026-08-19T10:00:00Z",
      message_count: 4,
      last_message_preview: "이전 대화",
    },
  ];
  assert.equal(chooseConversationId("older", conversations), "older");
  assert.equal(chooseConversationId("another-user-value", conversations), "latest");
  assert.equal(chooseConversationId(null, []), null);

  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
  };
  storeConversationHint(storage, "user-a", "conversation-a");
  storeConversationHint(storage, "user-b", "conversation-b");
  clearConversationHint(storage, "user-a");
  assert.equal(readConversationHint(storage, "user-a"), null);
  assert.equal(readConversationHint(storage, "user-b"), "conversation-b");
});

test("refresh history is server ordered, enriched, and never duplicates welcome", () => {
  const clarification = {
    clarification_id: "clarification-1",
    question: "어떤 업무인가요?",
    candidates: [],
  };
  const history: ConversationMessage[] = [
    {
      id: "assistant-1",
      role: "assistant",
      content: "어떤 업무인지 알려주세요.",
      server_sequence: 1,
      sequence_cursor: 3,
      created_at: "2026-08-20T09:00:02Z",
      response: {
        run_id: "run-1",
        conversation_id: "conversation-1",
        status: "NEEDS_CLARIFICATION",
        display_response: "어떤 업무인지 알려주세요.",
        clarification,
      },
    },
    {
      id: "user-1",
      role: "user",
      content: "아까 물어본 거 답 왔어.",
      server_sequence: 1,
      sequence_cursor: 2,
      created_at: "2026-08-20T09:00:01Z",
    },
    {
      id: "user-1",
      role: "user",
      content: "중복 복사본",
      server_sequence: 1,
      sequence_cursor: 2,
      created_at: "2026-08-20T09:00:01Z",
    },
  ];

  const restored = historyToChatMessages(history);
  assert.deepEqual(restored.map((message) => message.role), ["user", "assistant"]);
  assert.equal(restored[0].content, "아까 물어본 거 답 왔어.");
  assert.equal(restored[1].clarification?.clarification_id, "clarification-1");
  assert.equal(restored.some((message) => message.id === WELCOME_MESSAGE.id), false);

  const empty = historyToChatMessages([]);
  assert.equal(empty.length, 1);
  assert.equal(empty[0].id, WELCOME_MESSAGE.id);
});

test("an in-progress server run can resume after refresh", () => {
  const pending = findRestorableRun([
    {
      id: "user-pending",
      role: "user",
      content: "오늘 설치 가이드 수정했어.",
      server_sequence: 7,
      sequence_cursor: 14,
      created_at: "2026-08-20T09:00:01Z",
      run_id: "run-pending",
      run_status: "INTERPRETING",
      status_url: "/api/v1/runs/run-pending",
      client_message_id: "original-client-message",
    },
  ]);
  assert.deepEqual(pending, {
    runId: "run-pending",
    statusUrl: "/api/v1/runs/run-pending",
    content: "오늘 설치 가이드 수정했어.",
    clientMessageId: "original-client-message",
    failed: false,
  });
});

test("history pagination is combined without putting content in browser storage", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (input, init) => {
    assert.equal(init?.credentials, "include");
    const url = String(input);
    calls.push(url);
    if (url.includes("before_sequence=3")) {
      return jsonResponse({
        conversation_id: "conversation-1",
        items: [
          {
            id: "message-1",
            role: "user",
            content: "first",
            server_sequence: 1,
            created_at: "2026-08-20T09:00:00Z",
          },
        ],
        has_more: false,
      });
    }
    return jsonResponse({
      conversation_id: "conversation-1",
      items: [
        {
          id: "message-3",
          role: "assistant",
          content: "third",
          server_sequence: 3,
          created_at: "2026-08-20T09:02:00Z",
        },
      ],
      has_more: true,
    });
  };

  try {
    const history = await getConversationHistory("conversation-1", 10);
    assert.equal(calls.length, 2);
    assert.deepEqual(history.map((message) => message.id), ["message-3", "message-1"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("account security APIs use the exact contracts without exposing recovery codes elsewhere", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit; body: unknown }> = [];
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, init, body });
    if (url.endsWith("/password/change")) {
      return jsonResponse({
        recovery_code: "ONE-TIME-RECOVERY",
        user: {
          id: "user-a",
          username: "alpha",
          display_name: "Alpha",
          timezone: "Asia/Seoul",
          locale: "ko-KR",
          is_owner: true,
        },
      });
    }
    if (url.endsWith("/logout-all")) return new Response(null, { status: 204 });
    return jsonResponse({ recovery_code: "ONE-TIME-RECOVERY" });
  };

  try {
    const changed = await changePassword({
      currentPassword: "current-password",
      newPassword: "replacement-password",
    });
    const reset = await resetPassword({
      username: "alpha",
      recoveryCode: "OLD-RECOVERY",
      newPassword: "reset-password",
    });
    const rotated = await rotateRecoveryCode({
      currentPassword: "current-password",
    });
    await logoutAll();

    assert.equal(changed.user.id, "user-a");
    assert.equal(changed.recovery_code, "ONE-TIME-RECOVERY");
    assert.equal(reset.recovery_code, "ONE-TIME-RECOVERY");
    assert.equal(rotated.recovery_code, "ONE-TIME-RECOVERY");
    assert.ok(calls.every((call) => call.init?.credentials === "include"));
    assert.deepEqual(calls[0].body, {
      current_password: "current-password",
      new_password: "replacement-password",
    });
    assert.deepEqual(calls[1].body, {
      username: "alpha",
      recovery_code: "OLD-RECOVERY",
      new_password: "reset-password",
    });
    assert.deepEqual(calls[2].body, {
      current_password: "current-password",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("conversation creation always sends a stable idempotency key", async () => {
  const originalFetch = globalThis.fetch;
  let capturedHeaders: HeadersInit | undefined;
  globalThis.fetch = async (_input, init) => {
    capturedHeaders = init?.headers;
    return jsonResponse({
      conversation: {
        id: "conversation-new",
        title: null,
        is_default: false,
        created_at: "2026-08-20T09:00:00Z",
        updated_at: "2026-08-20T09:00:00Z",
        request_count: 0,
        message_count: 0,
        last_message_preview: null,
      },
      created: true,
    }, 201);
  };

  try {
    const result = await createConversation({
      idempotencyKey: "conversation-create-stable-1",
    });
    assert.equal(result.conversation.id, "conversation-new");
    const headers = new Headers(capturedHeaders);
    assert.equal(headers.get("Idempotency-Key"), "conversation-create-stable-1");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("login rate limiting preserves generic retry timing", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    jsonResponse(
      { error: { code: "TOO_MANY_LOGIN_ATTEMPTS", detail: "try later" } },
      429,
    );
  // Response helper cannot add Retry-After, so replace it for this assertion.
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        error: { code: "TOO_MANY_LOGIN_ATTEMPTS", detail: "try later" },
      }),
      {
        status: 429,
        headers: {
          "Content-Type": "application/json",
          "Retry-After": "900",
        },
      },
    );

  try {
    await assert.rejects(
      () => login({ username: "unknown", password: "wrong-password" }),
      (error: unknown) =>
        error instanceof ApiError &&
        error.code === "TOO_MANY_LOGIN_ATTEMPTS" &&
        error.retryAfterSeconds === 900,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
