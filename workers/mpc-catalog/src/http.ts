/**
 * A polite JSON fetcher: one request at a time with a small gap between
 * them, and exponential backoff on rate limits and server errors.
 */

export const USER_AGENT = "proxy-scaler-catalog/0.1.0 (+https://proxy-scaler.com)";

const MAX_ATTEMPTS = 6;
const MAX_RETRY_AFTER_MS = 60_000;
// Drive reports quota trouble as a 403 with one of these reasons.
const RATE_LIMIT_REASONS = new Set(["rateLimitExceeded", "userRateLimitExceeded"]);

/** A non-retryable HTTP failure (a 404, a 403 that isn't a rate limit, …). */
export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

export interface FetcherOptions {
  gapMs: number;
  backoffMs: number;
  /** Abort a request that hasn't answered within this long (then retry). */
  timeoutMs: number;
}

const sleep = (ms: number) => (ms > 0 ? new Promise<void>((r) => setTimeout(r, ms)) : Promise.resolve());

async function isRateLimited403(response: Response): Promise<boolean> {
  try {
    const body = (await response.clone().json()) as {
      error?: { errors?: { reason?: string }[] };
    };
    return (body.error?.errors ?? []).some((e) => RATE_LIMIT_REASONS.has(e.reason ?? ""));
  } catch {
    return false;
  }
}

function retryAfterMs(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  if (!header) return null;
  const seconds = Number(header);
  return Number.isFinite(seconds) ? Math.min(seconds * 1000, MAX_RETRY_AFTER_MS) : null;
}

/** Strips the API key from a URL so it can appear in logs and errors. */
export function redact(url: string): string {
  return url.replace(/([?&]key=)[^&]*/, "$1REDACTED");
}

export class Fetcher {
  private lastStart = 0;
  requests = 0;

  constructor(private readonly options: FetcherOptions) {}

  async getJson<T>(url: string): Promise<T> {
    let lastProblem = "";
    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      if (attempt > 0) {
        // 1x, 2x, 4x, … the base delay, with up to 25% jitter.
        const base = this.options.backoffMs * 2 ** (attempt - 1);
        await sleep(base + Math.random() * base * 0.25);
      }
      await sleep(this.lastStart + this.options.gapMs - Date.now());
      this.lastStart = Date.now();
      this.requests++;
      let response: Response;
      try {
        response = await fetch(url, {
          headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
          signal: AbortSignal.timeout(this.options.timeoutMs),
        });
        // The timeout covers the body too: a stalled read aborts as well.
        if (response.ok) return (await response.json()) as T;
      } catch (err) {
        const name = (err as Error).name;
        lastProblem =
          name === "TimeoutError" || name === "AbortError"
            ? `no answer within ${this.options.timeoutMs} ms`
            : `network error: ${(err as Error).message}`;
        continue;
      }
      const retryable =
        response.status === 429 ||
        response.status >= 500 ||
        (response.status === 403 && (await isRateLimited403(response)));
      lastProblem = `HTTP ${response.status}`;
      if (!retryable) {
        const detail = (await response.text().catch(() => "")).slice(0, 300);
        throw new HttpError(response.status, `${lastProblem} from ${redact(url)}: ${detail}`);
      }
      await response.body?.cancel();
      const wait = retryAfterMs(response);
      if (wait !== null) await sleep(wait);
    }
    throw new Error(`gave up after ${MAX_ATTEMPTS} attempts (${lastProblem}) at ${redact(url)}`);
  }
}
