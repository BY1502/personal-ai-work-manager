"use client";

import { useState, type FormEvent } from "react";
import { ApiError, login, register, resetPassword } from "@/lib/api";
import type { AuthUser } from "@/lib/types";
import RecoveryCodeDialog from "./RecoveryCodeDialog";

type AuthMode = "login" | "register" | "reset";

export interface AuthenticatedResult {
  user: AuthUser;
  recoveryCode?: string;
}

function authErrorMessage(error: unknown, mode: AuthMode): string {
  if (error instanceof ApiError) {
    if (
      error.code === "TOO_MANY_LOGIN_ATTEMPTS" ||
      error.code === "TOO_MANY_RECOVERY_ATTEMPTS" ||
      error.status === 429
    ) {
      const minutes = error.retryAfterSeconds
        ? Math.max(1, Math.ceil(error.retryAfterSeconds / 60))
        : null;
      const prefix = mode === "reset" ? "복구 요청" : "로그인 시도";
      return minutes
        ? `${prefix}가 많아 잠시 제한되었습니다. 약 ${minutes}분 뒤 다시 시도해주세요.`
        : `${prefix}가 많아 잠시 제한되었습니다. 잠시 후 다시 시도해주세요.`;
    }
    if (mode === "reset" && (error.status === 401 || error.status === 404)) {
      return "아이디, 복구 코드, 새 비밀번호를 다시 확인해주세요.";
    }
    if (error.code === "INVALID_CREDENTIALS" || error.status === 401) {
      return "아이디 또는 비밀번호를 다시 확인해주세요.";
    }
    if (error.status === 409) return "이미 사용 중인 아이디입니다.";
    if (error.status === 403 && mode === "register") {
      return "현재는 새 계정을 만들 수 없습니다. 관리자에게 문의해주세요.";
    }
    if (error.status === 422) {
      if (mode === "register") return "아이디와 비밀번호 형식을 확인해주세요.";
      if (mode === "reset") return "복구 코드와 새 비밀번호 형식을 확인해주세요.";
      return "입력한 내용을 확인해주세요.";
    }
    if (error.status === 0 || error.status === 502) {
      return "BY 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.";
    }
  }
  return "요청을 마치지 못했습니다. 잠시 후 다시 시도해주세요.";
}

export default function AuthGate({
  onAuthenticated,
}: {
  onAuthenticated: (result: AuthenticatedResult) => void;
}) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [recoveryCodeInput, setRecoveryCodeInput] = useState("");
  const [newRecoveryCode, setNewRecoveryCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!username.trim() || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === "reset") {
        if (!recoveryCodeInput.trim()) return;
        const result = await resetPassword({
          username: username.trim(),
          recoveryCode: recoveryCodeInput.trim(),
          newPassword: password,
        });
        setPassword("");
        setRecoveryCodeInput("");
        setNewRecoveryCode(result.recovery_code);
        return;
      }

      if (mode === "login") {
        const user = await login({ username: username.trim(), password });
        setPassword("");
        onAuthenticated({ user });
        return;
      }

      const result = await register({
        username: username.trim(),
        password,
        displayName,
      });
      setPassword("");
      onAuthenticated({
        user: result.user,
        recoveryCode: result.recovery_code,
      });
    } catch (caught) {
      setError(authErrorMessage(caught, mode));
    } finally {
      setBusy(false);
    }
  }

  function changeMode(nextMode: AuthMode) {
    if (nextMode === mode) return;
    setMode(nextMode);
    setPassword("");
    setRecoveryCodeInput("");
    setError(null);
  }

  const isReset = mode === "reset";

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <div className="auth-brand" aria-label="BY">
          <span className="brand-mark" aria-hidden="true">BY</span>
          <span>
            <strong>BY</strong>
            <small>나의 AI 업무 매니저</small>
          </span>
        </div>
        <div className="auth-copy">
          <span className="eyebrow">PRIVATE WORKSPACE</span>
          <h1 id="auth-title">
            {mode === "login"
              ? "다시 만나서 반가워요."
              : mode === "register"
                ? "나만의 업무 공간을 만들어요."
                : "비밀번호를 다시 설정해요."}
          </h1>
          <p>
            {isReset
              ? "가입할 때 보관한 복구 코드와 새 비밀번호를 입력해주세요."
              : "로그인하면 나의 업무 기억과 대화를 안전하게 이어갈 수 있습니다."}
          </p>
        </div>
        {!isReset ? (
          <div className="auth-tabs" role="tablist" aria-label="계정 메뉴">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              className={mode === "login" ? "selected" : ""}
              onClick={() => changeMode("login")}
            >
              로그인
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "register"}
              className={mode === "register" ? "selected" : ""}
              onClick={() => changeMode("register")}
            >
              계정 만들기
            </button>
          </div>
        ) : (
          <button className="auth-back" type="button" onClick={() => changeMode("login")}>
            ← 로그인으로 돌아가기
          </button>
        )}
        <form className="auth-form" onSubmit={submit}>
          {mode === "register" && (
            <label>
              <span>이름 <small>선택</small></span>
              <input
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                autoComplete="name"
                maxLength={80}
                placeholder="BY가 부를 이름"
              />
            </label>
          )}
          <label>
            <span>아이디</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              minLength={isReset ? 1 : 3}
              maxLength={64}
              required
              placeholder="아이디를 입력하세요"
            />
          </label>
          {isReset && (
            <label>
              <span>복구 코드</span>
              <input
                value={recoveryCodeInput}
                onChange={(event) => setRecoveryCodeInput(event.target.value)}
                autoComplete="one-time-code"
                autoCapitalize="characters"
                spellCheck={false}
                maxLength={128}
                required
                placeholder="보관한 복구 코드를 입력하세요"
              />
            </label>
          )}
          <label>
            <span>{isReset ? "새 비밀번호" : "비밀번호"}</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              minLength={mode === "login" ? 1 : 10}
              maxLength={128}
              required
              placeholder={mode === "login" ? "비밀번호를 입력하세요" : "10자 이상 입력하세요"}
            />
          </label>
          {error && <p className="auth-error" role="alert">{error}</p>}
          <button className="auth-submit" type="submit" disabled={busy}>
            {busy
              ? "확인하고 있어요…"
              : mode === "login"
                ? "BY 시작하기"
                : mode === "register"
                  ? "내 업무 공간 만들기"
                  : "새 비밀번호로 변경"}
          </button>
        </form>
        {mode === "login" && (
          <button className="forgot-password" type="button" onClick={() => changeMode("reset")}>
            비밀번호를 잊었나요?
          </button>
        )}
        <p className="auth-privacy">계정별 업무 기록과 대화는 서로 분리됩니다.</p>
      </section>
      {newRecoveryCode && (
        <RecoveryCodeDialog
          code={newRecoveryCode}
          title="새 복구 코드가 발급되었습니다."
          description="이전 복구 코드는 더 이상 사용할 수 없습니다. 새 코드를 지금 안전한 곳에 보관해주세요."
          onClose={() => {
            setNewRecoveryCode(null);
            changeMode("login");
          }}
        />
      )}
    </main>
  );
}
