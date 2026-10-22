// The browser's own wording for a dropped connection or a non-JSON reply
// ("Failed to fetch", "Unexpected token '<' … is not valid JSON"), across
// Chrome, Firefox and Safari. Same patterns and sentences as useResource's
// friendlyLoadError, kept here so this module imports nothing.
const NETWORK_TEXT =
  /failed to fetch|networkerror|network request failed|load failed|network error|err_(network|internet|connection)/i
const BAD_REPLY_TEXT =
  /is not valid json|json\.parse|json parse error|in json at position|unexpected token|unexpected end of (json|data|input)|did not match the expected pattern|<!doctype|<html/i
const NETWORK_MESSAGE = 'We could not reach the server. Check your internet connection and try again.'
const BAD_REPLY_MESSAGE = 'The server sent a reply we could not read. Please try again in a moment.'

// Pydantic's own validation wording ("String should have at most 200
// characters", "Field required", "Value error, ..."), mapped to plain English.
// Checked in order; the first match wins.
const PYDANTIC_MESSAGES: ReadonlyArray<readonly [RegExp, (match: RegExpExecArray) => string]> = [
  [/^field required$/i, () => 'This is required.'],
  [/^string should have at least (\d+) characters?$/i, (m) => `Use at least ${m[1]} characters.`],
  [/^string should have at most (\d+) characters?$/i, (m) => `Use ${m[1]} characters or fewer.`],
  [/^(list|set|tuple) should have at least (\d+) items?/i, (m) => `Choose at least ${m[2]}.`],
  [/^(list|set|tuple) should have at most (\d+) items?/i, (m) => `Choose ${m[2]} or fewer.`],
  [/^string should match pattern/i, () => 'This is not in the expected format.'],
  [/^value is not a valid email address/i, () => 'Enter a valid email address.'],
  [/^(input should be a valid url|url scheme should be|url should have)/i, () => 'Enter a web address starting with https://.'],
  [/^input should be a valid (integer|number|decimal)/i, () => 'Enter a number.'],
  [/^input should be greater than or equal to (-?[\d.]+)$/i, (m) => `Enter ${m[1]} or more.`],
  [/^input should be greater than (-?[\d.]+)$/i, (m) => `Enter a number above ${m[1]}.`],
  [/^input should be less than or equal to (-?[\d.]+)$/i, (m) => `Enter ${m[1]} or less.`],
  [/^input should be less than (-?[\d.]+)$/i, (m) => `Enter a number below ${m[1]}.`],
  [/^input should be a valid (date|datetime|time)/i, () => 'Enter a valid date.'],
  [/^input should be a valid boolean/i, () => 'Choose yes or no.'],
  [/^input should be (a valid string|a valid list|a valid dictionary|an object)/i, () => 'This value is not valid.'],
  [/^input should be ['"]/i, () => 'Choose one of the listed options.'],
  [/^extra inputs are not permitted$/i, () => 'This field is not accepted here.'],
]

/** Plain English for a Pydantic validation message, or null when it is not one. */
function plainValidationMessage(text: string): string | null {
  // "Value error, <sentence>" wraps a message the API wrote itself: keep the sentence.
  const valueError = /^(value|assertion) error,\s*(.+)$/i.exec(text)
  if (valueError) return valueError[2] ?? null
  for (const [pattern, toText] of PYDANTIC_MESSAGES) {
    const match = pattern.exec(text)
    if (match) return toText(match)
  }
  return null
}

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
  if (NETWORK_TEXT.test(text)) return NETWORK_MESSAGE
  if (BAD_REPLY_TEXT.test(text)) return BAD_REPLY_MESSAGE
  // The plain sentence still goes through the checks below (length, stack-shaped text).
  const plain = plainValidationMessage(text)
  if (plain && plain !== text) return softenErrorMessage(plain, fallback)
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
