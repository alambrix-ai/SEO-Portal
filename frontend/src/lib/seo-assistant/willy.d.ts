export type WillyMode = 'dock' | 'hero'

export function mountWilly(
  canvas: HTMLCanvasElement,
  options?: { mode?: WillyMode },
): void
