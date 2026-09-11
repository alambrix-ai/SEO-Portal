/**
 * Willy - Three.js assistant robot from the Willy asset pack.
 * Transparent canvas only (no circular chrome).
 *
 * `mode="hero"` (login stage): larger framing; neck and eyes follow the pointer.
 * `mode="dock"` (default): compact launcher with wave.
 */
import { useEffect, useId, useRef } from 'react'

type WillyBotProps = {
  /** Compact floating launcher size. */
  compact?: boolean
  /** dock = assistant launcher; hero = login stage with neck tracking. */
  mode?: 'dock' | 'hero'
  className?: string
}

export function WillyBot({
  compact = false,
  mode = 'dock',
  className,
}: WillyBotProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const canvasId = useId().replace(/:/g, '')

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || canvas.dataset.mounted === '1') return
    let cancelled = false

    void import('@/lib/seo-assistant/willy.js')
      .then((mod) => {
        if (cancelled || !canvasRef.current) return
        if (!mod.mountWilly) return
        canvas.dataset.mounted = '1'
        mod.mountWilly(canvas, { mode })
      })
      .catch(() => {
        const stage = canvas.parentElement
        if (stage) stage.hidden = true
        const fallback = stage?.parentElement?.querySelector<HTMLElement>('.willy-fallback')
        if (fallback) fallback.hidden = false
      })

    return () => {
      cancelled = true
    }
  }, [mode])

  return (
    <div
      className={`willy-bot${compact ? ' is-compact' : ''}${mode === 'hero' ? ' is-hero' : ''}${className ? ` ${className}` : ''}`}
      aria-hidden={compact || mode === 'hero' ? true : undefined}
    >
      <div className="willy-fallback" hidden>
        <span className="willy-fallback-mark" />
      </div>
      <div className="willy-bot-stage">
        <canvas
          ref={canvasRef}
          id={`willy-${canvasId}`}
          className="willy-bot-canvas"
          data-willy=""
        />
      </div>
    </div>
  )
}
