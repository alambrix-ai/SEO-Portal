/**
 * Three.js SEO Assistant sculpture.
 *
 * Scene code: `src/lib/seo-assistant/scene.js`
 * Vendor Three.js (MIT): `public/vendor/three/`
 */
import { useEffect, useId, useRef } from 'react'

type AssistantBotProps = {
  /** Compact launcher size for the floating dock. */
  compact?: boolean
  className?: string
}

export function AssistantBot({ compact = false, className }: AssistantBotProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const canvasId = useId().replace(/:/g, '')

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || canvas.dataset.mounted === '1') return
    let cancelled = false

    void import('@/lib/seo-assistant/scene.js')
      .then((mod) => {
        if (cancelled || !canvasRef.current) return
        if (!mod.mountAgent) return
        canvas.dataset.mounted = '1'
        mod.mountAgent(canvas)
      })
      .catch(() => {
        const wrap = canvas.parentElement
        const container = wrap?.parentElement
        if (wrap) wrap.hidden = true
        const toggle = container?.querySelector<HTMLElement>('.motion-toggle')
        if (toggle) toggle.hidden = true
        const fallback = container?.querySelector<HTMLElement>('.model-fallback')
        if (fallback) fallback.hidden = false
      })

    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div
      className={`assistant-bot${compact ? ' is-compact' : ''}${className ? ` ${className}` : ''}`}
      aria-label="SEO Assistant robot"
    >
      {compact ? null : (
        <button type="button" className="motion-toggle" aria-label="Pause 3D animation">
          Ⅱ
        </button>
      )}
      <div className="model-fallback" hidden>
        <div className="assistant-bot-fallback-mark" aria-hidden="true">
          AI
        </div>
        {compact ? null : <p>3D preview needs WebGL. The chat still works.</p>}
      </div>
      <div className="assistant-bot-stage">
        <canvas
          ref={canvasRef}
          id={`agent-sculpture-${canvasId}`}
          className="assistant-bot-canvas"
          aria-hidden={compact ? true : undefined}
          aria-label={compact ? undefined : 'Animated SEO assistant robot'}
        />
      </div>
    </div>
  )
}
