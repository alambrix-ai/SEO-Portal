/**
 * The second step of every sign-in and sign-up: type the code that was mailed.
 *
 * Shared by both screens because the rules are the server's, not the screen's:
 * how long a code lasts, how soon another can be sent, how many digits it has.
 * All three come from `/auth/policy`, so the countdowns here always match what
 * the API will actually do.
 *
 * The input is one field, not a row of boxes. Boxes look neater and behave
 * worse: pasting a code from a mail client is the most common way people enter
 * one, and it is exactly what a split input breaks.
 */
import { useEffect, useRef, useState } from 'react'

interface CodeEntryProps {
  /** Where the code went - shown back to the user so a typo is obvious. */
  email: string
  /** Digits expected, from the API policy. */
  length: number
  /** Seconds until another code may be requested. */
  resendIn: number
  busy: boolean
  error: string
  submitLabel: string
  busyLabel: string
  onSubmit: (code: string) => void
  onResend: () => void
  onChangeEmail: () => void
}

export function CodeEntry({
  email,
  length,
  resendIn,
  busy,
  error,
  submitLabel,
  busyLabel,
  onSubmit,
  onResend,
  onChangeEmail,
}: CodeEntryProps) {
  const [code, setCode] = useState('')
  const [countdown, setCountdown] = useState(resendIn)
  const input = useRef<HTMLInputElement>(null)

  // The code field is the only thing on the screen, so focus belongs here
  // rather than making everyone reach for the mouse first.
  useEffect(() => {
    input.current?.focus()
  }, [])

  // Restart whenever the server issues a new window, including after a resend.
  useEffect(() => {
    setCountdown(resendIn)
  }, [resendIn])

  useEffect(() => {
    if (countdown <= 0) return
    const timer = window.setInterval(() => {
      setCountdown((current) => (current > 0 ? current - 1 : 0))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [countdown])

  const complete = code.length === length

  const accept = (raw: string) => {
    // Strip whatever the mail client or keyboard added. A code pasted as
    // "123 456" is the same code.
    const digits = raw.replace(/\D/g, '').slice(0, length)
    setCode(digits)
    return digits
  }

  return (
    <form
      className="auth-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (complete && !busy) onSubmit(code)
      }}
    >
      <div className="code-sent">
        We sent a {length}-digit code to <strong>{email}</strong>
      </div>

      <div className="field">
        <label htmlFor="otp-code">Sign-in code</label>
        <input
          id="otp-code"
          ref={input}
          className="input code-input"
          // Lets browsers and mobile keyboards offer the code from the SMS or
          // mail app instead of making the user switch back and forth.
          autoComplete="one-time-code"
          inputMode="numeric"
          pattern="[0-9]*"
          maxLength={length}
          placeholder={'0'.repeat(length)}
          value={code}
          onChange={(event) => accept(event.target.value)}
          onPaste={(event) => {
            // Handled here so a pasted "123 456" submits immediately rather
            // than sitting there looking complete but one keystroke short.
            event.preventDefault()
            const digits = accept(event.clipboardData.getData('text'))
            if (digits.length === length && !busy) onSubmit(digits)
          }}
        />
      </div>

      {error ? <div className="auth-error">{error}</div> : null}

      <button type="submit" className="btn btn-primary btn-block" disabled={busy || !complete}>
        {busy ? busyLabel : submitLabel}
      </button>

      <div className="code-meta">
        <button type="button" className="link-button" onClick={onChangeEmail} disabled={busy}>
          Use a different address
        </button>
        <button
          type="button"
          className="link-button"
          onClick={onResend}
          disabled={busy || countdown > 0}
        >
          {countdown > 0 ? `Send another in ${countdown}s` : 'Send another code'}
        </button>
      </div>
    </form>
  )
}
