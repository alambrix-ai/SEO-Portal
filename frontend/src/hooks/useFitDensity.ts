/**
 * Measure a container and expose fluid density tokens so layouts can
 * auto-adjust continuously (not only at fixed breakpoints).
 */
import { useEffect, useRef, useState } from 'react'

export type FitDensity = {
  /** 0.78 – 1.2 scale derived from width × height */
  scale: number
  widthBand: 'narrow' | 'medium' | 'wide'
  heightBand: 'short' | 'mid' | 'tall'
  width: number
  height: number
}

const DEFAULT: FitDensity = {
  scale: 1,
  widthBand: 'wide',
  heightBand: 'tall',
  width: 1280,
  height: 800,
}

function measure(width: number, height: number): FitDensity {
  const w = Math.max(1, width)
  const h = Math.max(1, height)
  // Blend width and height so both skinny and short viewports compress.
  const scale = Math.min(1.18, Math.max(0.78, Math.min(w / 1180, h / 780) * 1.02))
  const widthBand = w < 760 ? 'narrow' : w < 1080 ? 'medium' : 'wide'
  const heightBand = h < 620 ? 'short' : h < 780 ? 'mid' : 'tall'
  return { scale, widthBand, heightBand, width: w, height: h }
}

export function useFitDensity<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [fit, setFit] = useState<FitDensity>(DEFAULT)

  useEffect(() => {
    const node = ref.current
    if (!node || typeof ResizeObserver === 'undefined') return

    const apply = (width: number, height: number) => {
      const next = measure(width, height)
      setFit(next)
      node.style.setProperty('--fit-scale', String(next.scale))
      node.style.setProperty('--fit-w', `${Math.round(width)}px`)
      node.style.setProperty('--fit-h', `${Math.round(height)}px`)
      node.dataset.fitW = next.widthBand
      node.dataset.fitH = next.heightBand
    }

    apply(node.clientWidth, node.clientHeight)
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const { width, height } = entry.contentRect
      apply(width, height)
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return { ref, fit }
}
