/**
 * Three.js SEO Assistant sculpture.
 *
 * Scene code: `src/lib/seo-assistant/scene.js`
 * Vendor Three.js (MIT): `public/vendor/three/`
 */
import { useEffect, useRef } from 'react'

export function AssistantBot() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

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
    <div className="assistant-bot" aria-label="SEO Assistant robot">
      <button type="button" className="motion-toggle" aria-label="Pause 3D animation">
        Ⅱ
      </button>
      <div className="model-fallback" hidden>
        <div className="assistant-bot-fallback-mark" aria-hidden="true">
          AI
        </div>
        <p>3D preview needs WebGL. The chat still works.</p>
      </div>
      <div className="assistant-bot-stage">
        <canvas
          ref={canvasRef}
          id="agent-sculpture"
          className="assistant-bot-canvas"
          aria-label="Animated SEO assistant robot"
        />
      </div>
    </div>
  )
}
