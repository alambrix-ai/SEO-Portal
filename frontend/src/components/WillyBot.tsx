/**
 * Willy — Three.js assistant robot from the Willy asset pack.
 * Transparent canvas only (no circular chrome).
 */
import { useEffect, useId, useRef } from 'react'

type WillyBotProps = {
  /** Compact floating launcher size. */
  compact?: boolean
  className?: string
}

export function WillyBot({ compact = false, className }: WillyBotProps) {
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
        mod.mountWilly(canvas)
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
  }, [])

  return (
    <div
      className={`willy-bot${compact ? ' is-compact' : ''}${className ? ` ${className}` : ''}`}
      aria-hidden={compact ? true : undefined}
    >
      <div className="willy-fallback" hidden>
        <span className="willy-fallback-mark">Willy</span>
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
