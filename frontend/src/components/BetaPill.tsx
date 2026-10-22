/**
 * "Beta": this connector type has not yet passed a live check against a real
 * account (Portal Admin -> Verification). Verified connectors show nothing.
 */
import type { ConnectorVerification } from '@/api/types'

export const BETA_TOOLTIP = 'Not yet confirmed against a live account. It may not work as expected.'

export function BetaPill({ verification }: { verification?: ConnectorVerification | null }) {
  if (!verification || verification.verified || !verification.label) return null
  const tip = verification.tooltip || BETA_TOOLTIP
  return (
    <span className="beta-pill" title={tip} aria-label={`${verification.label}: ${tip}`}>
      {verification.label}
    </span>
  )
}
