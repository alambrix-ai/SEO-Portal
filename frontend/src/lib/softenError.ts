/**
 * Last-line defence: never show stack-shaped or status-code text to operators.
 * Backend already softens most messages; this covers anything that still slips through.
 */
export function softenErrorMessage(
  message: string,
  fallback = 'Something went wrong. Please try again.',
): string {
  const text = (message || '').trim()
  if (!text) return fallback
  const lower = text.toLowerCase()
  if (/\b(429|rate limit|quota)\b/i.test(text)) {
    return 'This service is temporarily limiting requests. Wait a minute and try again.'
  }
  if (/\b(401|unauthorized|invalid api key)\b/i.test(text)) {
    return 'Those credentials were rejected. Check the key or password and try again.'
  }
  if (/\b(403|forbidden)\b/i.test(text)) {
    return 'Access was refused. Check that this account has permission.'
  }
  if (
    /\b(traceback|exception|nonetype|econn|etimedout|after \d+ attempts|returned \d{3})\b/i.test(
      text,
    ) ||
    text.length > 280 ||
    text.startsWith('{') ||
    /^\w+Error:/.test(text)
  ) {
    return fallback
  }
  if (lower.includes('network is unreachable') || lower.includes('connection refused')) {
    return 'Could not reach the other service. Try again shortly.'
  }
  return text
}
