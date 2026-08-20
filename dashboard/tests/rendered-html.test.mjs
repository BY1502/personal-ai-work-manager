import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const dashboardUrl = new URL("../app/Dashboard.tsx", import.meta.url);
const authGateUrl = new URL("../app/AuthGate.tsx", import.meta.url);
const accountSecurityUrl = new URL("../app/AccountSecurityPanel.tsx", import.meta.url);
const recoveryDialogUrl = new URL("../app/RecoveryCodeDialog.tsx", import.meta.url);
const apiUrl = new URL("../lib/api.ts", import.meta.url);
const proxyUrl = new URL("../app/api/v1/[...path]/route.ts", import.meta.url);
const serviceWorkerUrl = new URL("../public/sw.js", import.meta.url);

test("dashboard gates private UI and clears it on logout or expired auth", async () => {
  const dashboard = await readFile(dashboardUrl, "utf8");
  assert.match(dashboard, /authStatus === "anonymous"/);
  assert.match(dashboard, /<AuthGate/);
  assert.match(dashboard, /clearPrivateUi\(\)/);
  assert.match(dashboard, /clearConversationHint\(window\.localStorage, previousUser\.id\)/);
  assert.match(dashboard, /getConversationHistory\(selectedId\)/);
  assert.match(dashboard, /historyToChatMessages\(history\)/);
  assert.match(dashboard, /createConversation\(\{ idempotencyKey \}\)/);
  assert.match(dashboard, /conversationCreateInFlightRef\.current/);
  assert.match(dashboard, /switchConversation\(event\.target\.value\)/);
  assert.match(dashboard, /const requestUserId = userRef\.current\.id/);
  assert.match(dashboard, /signal: requestController\.signal/);
  assert.ok(
    (dashboard.match(/if \(!requestIsCurrent\(\)\) return;/g) || []).length >= 5,
    "chat retries and polling must guard every account/conversation boundary",
  );
  assert.ok(
    (dashboard.match(/const actionUserId = userRef\.current\?\.id/g) || []).length >= 4,
    "secondary async actions must capture the active user",
  );
  assert.doesNotMatch(dashboard, /setItem\(\s*"jarvis-conversation-id"/);
});

test("login and registration forms use password-manager friendly fields", async () => {
  const authGate = await readFile(authGateUrl, "utf8");
  assert.match(authGate, /autoComplete="username"/);
  assert.match(authGate, /"current-password" : "new-password"/);
  assert.match(authGate, /mode === "login" \? 1 : 10/);
  assert.match(authGate, /autoComplete="one-time-code"/);
  assert.match(authGate, /TOO_MANY_LOGIN_ATTEMPTS/);
  assert.match(authGate, /계정별 업무 기록과 대화는 서로 분리됩니다/);
});

test("security controls use password-manager fields and recovery code stays ephemeral", async () => {
  const [dashboard, authGate, accountSecurity, recoveryDialog] = await Promise.all([
    readFile(dashboardUrl, "utf8"),
    readFile(authGateUrl, "utf8"),
    readFile(accountSecurityUrl, "utf8"),
    readFile(recoveryDialogUrl, "utf8"),
  ]);
  assert.match(accountSecurity, /autoComplete="current-password"/);
  assert.match(accountSecurity, /autoComplete="new-password"/);
  assert.match(accountSecurity, /logoutAll\(\)/);
  assert.match(accountSecurity, /rotateRecoveryCode\(\{/);
  assert.match(accountSecurity, /currentPassword: recoveryPassword/);
  assert.match(recoveryDialog, /navigator\.clipboard\.writeText\(code\)/);
  assert.match(recoveryDialog, /복구 코드를 안전한 곳에 보관했습니다/);
  assert.match(recoveryDialog, /disabled=\{!storageConfirmed\}/);
  assert.match(recoveryDialog, /if \(storageConfirmed\) onClose\(\)/);
  assert.match(recoveryDialog, /event\.key === "Escape"/);
  assert.match(recoveryDialog, /event\.preventDefault\(\)/);
  assert.match(recoveryDialog, /event\.stopImmediatePropagation\(\)/);
  assert.match(recoveryDialog, /window\.addEventListener\("keydown", onKeyDown, true\)/);
  assert.doesNotMatch(recoveryDialog, /if \(event\.key === "Escape"\) onClose\(\)/);
  assert.doesNotMatch(recoveryDialog, /dialog-backdrop[^\n]*onClick/);
  assert.doesNotMatch(
    `${dashboard}\n${authGate}\n${accountSecurity}\n${recoveryDialog}`,
    /localStorage\.(?:setItem|getItem)\([^\n]*recovery/i,
  );
});

test("API calls send cookies and the service worker never handles API requests", async () => {
  const [api, proxy, serviceWorker] = await Promise.all([
    readFile(apiUrl, "utf8"),
    readFile(proxyUrl, "utf8"),
    readFile(serviceWorkerUrl, "utf8"),
  ]);
  assert.match(api, /credentials: "include"/);
  assert.match(api, /cache: "no-store"/);
  assert.match(api, /return \[`\$\{window\.location\.origin\}\$\{apiPath\}`\]/);
  assert.match(serviceWorker, /url\.pathname\.startsWith\("\/api\/"\)/);
  assert.match(serviceWorker, /event\.request\.mode !== "navigate"/);
  assert.match(proxy, /detail: "BY backend is unavailable"/);
  assert.match(proxy, /"Cache-Control": "private, no-store"/);
  assert.doesNotMatch(proxy, /detail: error instanceof Error/);
});
