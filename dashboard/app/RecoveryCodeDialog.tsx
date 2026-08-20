"use client";

import { useEffect, useId, useRef, useState } from "react";

export default function RecoveryCodeDialog({
  code,
  title = "복구 코드를 보관해주세요.",
  description = "비밀번호를 잊었을 때 필요합니다. 이 코드는 지금 한 번만 표시됩니다.",
  onClose,
}: {
  code: string;
  title?: string;
  description?: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [storageConfirmed, setStorageConfirmed] = useState(false);
  const dialogRef = useRef<HTMLElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const confirmationId = useId();

  useEffect(() => {
    dialogRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, []);

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="dialog-backdrop">
      <section
        ref={dialogRef}
        className="recovery-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
      >
        <span className="dialog-kicker">ONE-TIME CODE</span>
        <h2 id={titleId}>{title}</h2>
        <p id={descriptionId}>{description}</p>
        <code>{code}</code>
        <div className="recovery-actions">
          <button type="button" className="secondary-button" onClick={() => void copyCode()}>
            {copied ? "복사했습니다" : "코드 복사"}
          </button>
          <span className="copy-status" role="status" aria-live="polite">
            {copied ? "복구 코드가 클립보드에 복사되었습니다." : ""}
          </span>
        </div>
        <label className="recovery-confirmation" htmlFor={confirmationId}>
          <input
            id={confirmationId}
            type="checkbox"
            checked={storageConfirmed}
            onChange={(event) => setStorageConfirmed(event.target.checked)}
          />
          <span>복구 코드를 안전한 곳에 보관했습니다.</span>
        </label>
        <button
          type="button"
          className="primary-button recovery-close"
          disabled={!storageConfirmed}
          onClick={() => {
            if (storageConfirmed) onClose();
          }}
        >
          확인하고 닫기
        </button>
        <small>
          BY는 이 코드를 브라우저에 저장하지 않습니다. 배경 클릭이나 Esc 키로는 이 창이 닫히지 않습니다.
        </small>
      </section>
    </div>
  );
}
