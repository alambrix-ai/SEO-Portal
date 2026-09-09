/**
 * Navigation and interface icons.
 *
 * Thin-stroke line icons at 1.5, per the design system — the same geometry the
 * design uses for each nav item, so the sidebar reads as it was drawn.
 */
import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function Icon({ size = 18, children, ...rest }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

export function DashboardIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.5" y="2.5" width="6.5" height="6.5" rx="0.5" />
      <rect x="11" y="2.5" width="6.5" height="6.5" rx="0.5" />
      <rect x="2.5" y="11" width="6.5" height="6.5" rx="0.5" />
      <rect x="11" y="11" width="6.5" height="6.5" rx="0.5" />
    </Icon>
  )
}

export function OnboardingIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10" cy="10" r="7.2" />
      <path d="M13 7L11 11L7 13L9 9L13 7Z" />
    </Icon>
  )
}

export function AgentsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="5" y="5" width="10" height="10" rx="0.5" />
      <line x1="10" y1="1.5" x2="10" y2="5" />
      <line x1="10" y1="15" x2="10" y2="18.5" />
      <line x1="1.5" y1="10" x2="5" y2="10" />
      <line x1="15" y1="10" x2="18.5" y2="10" />
    </Icon>
  )
}

export function TechnicalIcon(props: IconProps) {
  // A gauge: the screen is the site's health reading.
  return (
    <Icon {...props}>
      <path d="M3 16a9 9 0 0 1 18 0" />
      <line x1="12" y1="16" x2="16.5" y2="10.5" />
      <circle cx="12" cy="16" r="1.4" />
    </Icon>
  )
}

export function SeoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="8.5" cy="8.5" r="5.5" />
      <line x1="12.7" y1="12.7" x2="18" y2="18" />
    </Icon>
  )
}

export function OffPageIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="7" cy="13" r="3.2" />
      <circle cx="13" cy="7" r="3.2" />
      <line x1="9" y1="11" x2="11" y2="9" />
    </Icon>
  )
}

export function AdsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10" cy="10" r="7.2" />
      <circle cx="10" cy="10" r="4" />
      <circle cx="10" cy="10" r="0.8" fill="currentColor" />
    </Icon>
  )
}

export function ConnectorsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="6" y="7" width="8" height="6" rx="0.5" />
      <line x1="8" y1="7" x2="8" y2="3.5" />
      <line x1="12" y1="7" x2="12" y2="3.5" />
      <line x1="10" y1="13" x2="10" y2="17" />
    </Icon>
  )
}

export function ApprovalsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="3" width="14" height="14" rx="0.5" />
      <path d="M6.5 10.2L9 12.7L13.7 7.5" />
    </Icon>
  )
}

export function ReportsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <line x1="3" y1="17" x2="17" y2="17" />
      <rect x="4.5" y="11" width="3" height="6" />
      <rect x="8.5" y="7" width="3" height="10" />
      <rect x="12.5" y="3" width="3" height="14" />
    </Icon>
  )
}

export function AdminIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 2.5L16.5 5V10C16.5 14 13.7 16.5 10 17.5C6.3 16.5 3.5 14 3.5 10V5L10 2.5Z" />
    </Icon>
  )
}

export function BellIcon(props: IconProps) {
  return (
    <Icon size={16} {...props}>
      <path d="M5 14V9a5 5 0 0 1 10 0v5l1.5 2h-13L5 14Z" />
      <path d="M8.3 17.5a1.7 1.7 0 0 0 3.4 0" />
    </Icon>
  )
}

export function LockIcon(props: IconProps) {
  return (
    <Icon size={14} {...props}>
      <rect x="4.5" y="9" width="11" height="8" rx="1" />
      <path d="M7 9V6.5a3 3 0 0 1 6 0V9" />
    </Icon>
  )
}

export function CheckIcon(props: IconProps) {
  return (
    <Icon size={14} {...props}>
      <path d="M4 10.5L8 14.5L16 5.5" />
    </Icon>
  )
}

export function ExternalIcon(props: IconProps) {
  return (
    <Icon size={14} {...props}>
      <path d="M11 4h5v5" />
      <path d="M16 4L9 11" />
      <path d="M14 12v4H4V6h4" />
    </Icon>
  )
}
