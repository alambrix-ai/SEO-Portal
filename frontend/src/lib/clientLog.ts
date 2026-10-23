/**
 * Client-side error reporting for the console.
 *
 * Uses the browser console (captured in support tooling / device logs). Never
 * writes emails, tokens, or raw stack traces as user-visible copy — callers
 * show a friendly message separately.
 */

const EMAIL = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi
const BEARER = /\b(bearer\s+)[a-z0-9._-]+/gi
const JWT = /\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b/g

function scrub(text: string): string {
  return text
    .replace(EMAIL, '[email]')
    .replace(BEARER, '$1[redacted]')
    .replace(JWT, '[redacted-token]')
    .slice(0, 500)
}

export type ClientLogContext = {
  where?: string
  route?: string
}

/** Report a client failure without PII. Safe to call from error boundaries. */
export function reportClientError(error: unknown, context: ClientLogContext = {}): void {
  const name =
    error instanceof Error && error.name ? scrub(error.name) : 'Error'
  const message =
    error instanceof Error && error.message
      ? scrub(error.message)
      : scrub(String(error ?? 'unknown'))
  const route =
    context.route ||
    (typeof window !== 'undefined' ? window.location.pathname : '') ||
    ''
  // Structured so support can grep device / browser logs. No stack in the
  // object we print in production builds — stacks can include query strings.
  const payload = {
    source: 'synaprank-console',
    where: context.where ?? 'client',
    route: scrub(route),
    name,
    message,
  }
  if (import.meta.env.DEV && error instanceof Error && error.stack) {
    console.error('[synaprank]', payload, scrub(error.stack))
  } else {
    console.error('[synaprank]', payload)
  }
}
