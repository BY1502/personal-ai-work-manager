import type { JarvisResponse } from "./types";

export const RUN_POLL_INTERVAL_MS = 2_000;
export const RUN_POLL_MAX_ATTEMPTS = 24;

export interface RunPollValue {
  status: string;
  result: JarvisResponse | null;
}

export class RunPollingTimeoutError extends Error {
  constructor() {
    super("BY의 응답 시간이 길어지고 있습니다.");
  }
}

export class RunPollingFailedError extends Error {
  constructor() {
    super("BY가 요청을 마치지 못했습니다.");
  }
}

export interface PollRunOptions {
  getRun: (statusUrl: string) => Promise<RunPollValue>;
  statusUrl: string;
  signal?: AbortSignal;
  intervalMs?: number;
  maxAttempts?: number;
  sleep?: (delayMs: number, signal?: AbortSignal) => Promise<void>;
}

export function abortableSleep(
  delayMs: number,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Polling cancelled", "AbortError"));
      return;
    }
    const timeout = globalThis.setTimeout(resolve, delayMs);
    signal?.addEventListener(
      "abort",
      () => {
        globalThis.clearTimeout(timeout);
        reject(new DOMException("Polling cancelled", "AbortError"));
      },
      { once: true },
    );
  });
}

export async function pollRun({
  getRun,
  statusUrl,
  signal,
  intervalMs = RUN_POLL_INTERVAL_MS,
  maxAttempts = RUN_POLL_MAX_ATTEMPTS,
  sleep = abortableSleep,
}: PollRunOptions): Promise<JarvisResponse> {
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (signal?.aborted) {
      throw new DOMException("Polling cancelled", "AbortError");
    }
    if (attempt > 0) await sleep(intervalMs, signal);
    const run = await getRun(statusUrl);
    if (run.result) return run.result;
    if (["FAILED", "INTERRUPTED_RETRYABLE"].includes(run.status)) {
      throw new RunPollingFailedError();
    }
  }
  throw new RunPollingTimeoutError();
}
