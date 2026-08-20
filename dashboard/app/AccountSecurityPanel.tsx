"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  changePassword,
  logoutAll,
  rotateRecoveryCode,
} from "@/lib/api";
import type { AuthUser } from "@/lib/types";

function securityError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "INVALID_CREDENTIALS" || error.status === 401) {
      return "현재 비밀번호를 다시 확인해주세요.";
    }
    if (error.status === 422) return "새 비밀번호 형식을 확인해주세요.";
    if (error.status === 0 || error.status === 502) {
      return "BY 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.";
    }
  }
  return "요청을 마치지 못했습니다. 잠시 후 다시 시도해주세요.";
}

export default function AccountSecurityPanel({
  user,
  onClose,
  onUserUpdated,
  onRecoveryCode,
  onLogoutAll,
}: {
  user: AuthUser;
  onClose: () => void;
  onUserUpdated: (user: AuthUser) => void;
  onRecoveryCode: (code: string) => void;
  onLogoutAll: () => void;
}) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [rotateConfirm, setRotateConfirm] = useState(false);
  const [rotateBusy, setRotateBusy] = useState(false);
  const [recoveryPassword, setRecoveryPassword] = useState("");
  const [logoutConfirm, setLogoutConfirm] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !passwordBusy && !rotateBusy && !logoutBusy) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [logoutBusy, onClose, passwordBusy, rotateBusy]);

  async function submitPassword(event: FormEvent) {
    event.preventDefault();
    setPasswordMessage(null);
    setPasswordError(null);
    if (newPassword !== confirmPassword) {
      setPasswordError("새 비밀번호가 서로 일치하지 않습니다.");
      return;
    }
    if (newPassword.length < 10 || passwordBusy) return;
    setPasswordBusy(true);
    try {
      const result = await changePassword({ currentPassword, newPassword });
      if (!mountedRef.current) return;
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordMessage("비밀번호를 변경했습니다. 다른 기기의 로그인은 해제되었습니다.");
      onUserUpdated(result.user);
      if (result.recovery_code) onRecoveryCode(result.recovery_code);
    } catch (error) {
      if (mountedRef.current) setPasswordError(securityError(error));
    } finally {
      if (mountedRef.current) setPasswordBusy(false);
    }
  }

  async function rotateCode() {
    if (!rotateConfirm || !recoveryPassword || rotateBusy) return;
    setRotateBusy(true);
    setActionError(null);
    try {
      const result = await rotateRecoveryCode({
        currentPassword: recoveryPassword,
      });
      if (!mountedRef.current) return;
      setRotateConfirm(false);
      setRecoveryPassword("");
      onRecoveryCode(result.recovery_code);
    } catch (error) {
      if (mountedRef.current) setActionError(securityError(error));
    } finally {
      if (mountedRef.current) setRotateBusy(false);
    }
  }

  async function signOutEverywhere() {
    if (!logoutConfirm || logoutBusy) return;
    setLogoutBusy(true);
    setActionError(null);
    try {
      await logoutAll();
      if (mountedRef.current) onLogoutAll();
    } catch (error) {
      if (mountedRef.current) {
        setActionError(securityError(error));
        setLogoutBusy(false);
      }
    }
  }

  return (
    <div className="dialog-backdrop account-dialog-backdrop" role="presentation">
      <section
        className="account-security-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="security-title"
      >
        <header>
          <div>
            <span className="dialog-kicker">ACCOUNT</span>
            <h2 id="security-title">계정과 보안</h2>
            <p>{user.display_name || user.username}님의 로그인 정보를 관리합니다.</p>
          </div>
          <button type="button" className="dialog-close" onClick={onClose} aria-label="닫기">
            ×
          </button>
        </header>

        <form className="security-section password-change-form" onSubmit={submitPassword}>
          <div className="security-section-heading">
            <h3>비밀번호 변경</h3>
            <p>변경하면 현재 기기를 제외한 기존 로그인이 해제됩니다.</p>
          </div>
          <label>
            <span>현재 비밀번호</span>
            <input
              type="password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              autoComplete="current-password"
              maxLength={128}
              required
            />
          </label>
          <div className="password-pair">
            <label>
              <span>새 비밀번호</span>
              <input
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                autoComplete="new-password"
                minLength={10}
                maxLength={128}
                required
              />
            </label>
            <label>
              <span>새 비밀번호 확인</span>
              <input
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
                minLength={10}
                maxLength={128}
                required
              />
            </label>
          </div>
          {passwordError && <p className="security-error" role="alert">{passwordError}</p>}
          {passwordMessage && <p className="security-success" role="status">{passwordMessage}</p>}
          <button
            type="submit"
            className="secondary-button"
            disabled={
              passwordBusy ||
              !currentPassword ||
              newPassword.length < 10 ||
              !confirmPassword
            }
          >
            {passwordBusy ? "변경 중…" : "비밀번호 변경"}
          </button>
        </form>

        <section className="security-section compact-security-section">
          <div className="security-section-heading">
            <h3>복구 코드</h3>
            <p>새 코드를 발급하면 이전 코드는 즉시 사용할 수 없습니다.</p>
          </div>
          {rotateConfirm ? (
            <div className="inline-confirm">
              <p>새 복구 코드를 발급할까요?</p>
              <label>
                <span>현재 비밀번호</span>
                <input
                  type="password"
                  value={recoveryPassword}
                  onChange={(event) => setRecoveryPassword(event.target.value)}
                  autoComplete="current-password"
                  maxLength={128}
                  required
                />
              </label>
              <div>
                <button type="button" className="secondary-button" onClick={() => void rotateCode()} disabled={rotateBusy || !recoveryPassword}>
                  {rotateBusy ? "발급 중…" : "새 코드 발급"}
                </button>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setRotateConfirm(false);
                    setRecoveryPassword("");
                  }}
                  disabled={rotateBusy}
                >
                  취소
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="secondary-button" onClick={() => setRotateConfirm(true)}>
              복구 코드 재발급
            </button>
          )}
        </section>

        <section className="security-section compact-security-section danger-section">
          <div className="security-section-heading">
            <h3>모든 기기에서 로그아웃</h3>
            <p>현재 브라우저를 포함한 모든 로그인 세션을 종료합니다.</p>
          </div>
          {logoutConfirm ? (
            <div className="inline-confirm">
              <p>모든 기기에서 정말 로그아웃할까요?</p>
              <div>
                <button type="button" className="danger-button" onClick={() => void signOutEverywhere()} disabled={logoutBusy}>
                  {logoutBusy ? "로그아웃 중…" : "모두 로그아웃"}
                </button>
                <button type="button" className="text-button" onClick={() => setLogoutConfirm(false)} disabled={logoutBusy}>
                  취소
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="danger-button" onClick={() => setLogoutConfirm(true)}>
              모든 기기에서 로그아웃
            </button>
          )}
        </section>
        {actionError && <p className="security-error panel-error" role="alert">{actionError}</p>}
      </section>
    </div>
  );
}
