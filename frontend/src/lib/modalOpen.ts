/** Shared body class so overlays dim the full shell (including the topbar). */

const CLASS = 'modal-open'
let depth = 0

export function pushModalOpen(): void {
  depth += 1
  document.body.classList.add(CLASS)
}

export function popModalOpen(): void {
  depth = Math.max(0, depth - 1)
  if (depth === 0) document.body.classList.remove(CLASS)
}
