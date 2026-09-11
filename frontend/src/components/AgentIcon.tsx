/**
 * Agent marks — one distinctive tile per agent.
 *
 * Each slug gets its own warm brand gradient (cream / brown / plum / sage /
 * gold from the marketing site) and a filled glyph that says what the agent
 * does, so the Agents grid reads as a set of characters rather than twelve
 * identical grey squares.
 */
import type { CSSProperties, ReactNode, SVGProps } from 'react'

type GlyphProps = SVGProps<SVGSVGElement> & { size?: number }

type Theme = {
  from: string
  to: string
  ink: string
  glow: string
}

const AGENT_THEMES: Record<string, Theme> = {
  aeo_qa_injector: {
    from: '#5b3e58',
    to: '#8a5f80',
    ink: '#fff8f0',
    glow: 'rgba(91, 62, 88, 0.35)',
  },
  backlink_node_discovery: {
    from: '#966b3f',
    to: '#c09060',
    ink: '#fff8f0',
    glow: 'rgba(150, 107, 63, 0.35)',
  },
  click_fraud_controller: {
    from: '#3d2a18',
    to: '#7a5632',
    ink: '#f6efe6',
    glow: 'rgba(61, 42, 24, 0.4)',
  },
  competitor_link_monitor: {
    from: '#4a3247',
    to: '#7d9a83',
    ink: '#faf8f4',
    glow: 'rgba(125, 154, 131, 0.35)',
  },
  digital_pr_outreach: {
    from: '#a87848',
    to: '#d4b56a',
    ink: '#1a1a1a',
    glow: 'rgba(212, 181, 106, 0.4)',
  },
  dynamic_creative_optimizer: {
    from: '#865a78',
    to: '#c09060',
    ink: '#fff8f0',
    glow: 'rgba(134, 90, 120, 0.35)',
  },
  first_party_audience_modeler: {
    from: '#7d9a83',
    to: '#5b3e58',
    ink: '#faf8f4',
    glow: 'rgba(125, 154, 131, 0.35)',
  },
  knowledge_graph_schema: {
    from: '#5c4026',
    to: '#966b3f',
    ink: '#f6efe6',
    glow: 'rgba(92, 64, 38, 0.4)',
  },
  on_page_seo_sync: {
    from: '#7a5632',
    to: '#7d9a83',
    ink: '#fff8f0',
    glow: 'rgba(122, 86, 50, 0.35)',
  },
  predictive_budget_engine: {
    from: '#d4b56a',
    to: '#966b3f',
    ink: '#1a1a1a',
    glow: 'rgba(212, 181, 106, 0.4)',
  },
  referral_spam_guard: {
    from: '#3d2a18',
    to: '#5b3e58',
    ink: '#f6efe6',
    glow: 'rgba(61, 42, 24, 0.4)',
  },
  technical_seo_auditor: {
    from: '#5b3e58',
    to: '#966b3f',
    ink: '#fff8f0',
    glow: 'rgba(91, 62, 88, 0.35)',
  },
}

const FALLBACK_THEME: Theme = {
  from: '#efe8dc',
  to: '#e0d5c4',
  ink: '#5c4026',
  glow: 'rgba(150, 107, 63, 0.2)',
}

function Glyph({
  size = 20,
  children,
  ...rest
}: GlyphProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

function AnswerIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        fillOpacity="0.22"
        d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7A2.5 2.5 0 0 1 17.5 15H9.2L4 19.2V5.5Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7A2.5 2.5 0 0 1 17.5 15H9.2L4 19.2V5.5Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M9.2 8.2a2 2 0 0 1 3.8.7c0 1.4-1.9 1.5-1.9 2.9"
      />
      <circle cx="11.1" cy="13.6" r="0.9" fill="currentColor" />
    </Glyph>
  )
}

function NodesIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        d="M7.2 8.2 15.2 6.2M7 15.2 15.4 8.4M9.4 16.4l5.2.8"
      />
      <circle cx="5.5" cy="7.5" r="2.6" fill="currentColor" fillOpacity="0.28" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="18" cy="5.5" r="2.4" fill="currentColor" fillOpacity="0.45" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="17" cy="17" r="2.5" fill="currentColor" fillOpacity="0.28" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="7.2" cy="16.2" r="2.3" fill="currentColor" fillOpacity="0.55" stroke="currentColor" strokeWidth="1.5" />
    </Glyph>
  )
}

function ClickShieldIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        fillOpacity="0.25"
        d="M12 2.8 19 5.4v5.8c0 4-2.9 7.5-7 8.8-4.1-1.3-7-4.8-7-8.8V5.4L12 2.8Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
        d="M12 2.8 19 5.4v5.8c0 4-2.9 7.5-7 8.8-4.1-1.3-7-4.8-7-8.8V5.4L12 2.8Z"
      />
      <path
        fill="currentColor"
        d="M10.1 8.2 15.4 13.6l-2.4.4-.8 2.5L10.1 8.2Z"
      />
    </Glyph>
  )
}

function WatchLinkIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M9.2 7.2 11 5.4a3.4 3.4 0 0 1 4.8 4.8l-1.8 1.8"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M14.8 13.8 13 15.6a3.4 3.4 0 0 1-4.8-4.8l1.2-1.2"
      />
      <path
        fill="currentColor"
        fillOpacity="0.28"
        stroke="currentColor"
        strokeWidth="1.5"
        d="M3.2 18.2c2-3 6.2-3 8.2 0-2 3-6.2 3-8.2 0Z"
      />
      <circle cx="7.3" cy="18.2" r="1.2" fill="currentColor" />
    </Glyph>
  )
}

function OutreachIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        fillOpacity="0.3"
        d="M3.5 9.2 16.5 4.5v15L3.5 14.8V9.2Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
        d="M3.5 9.2 16.5 4.5v15L3.5 14.8V9.2Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M6.8 12.2v4.2a1.8 1.8 0 0 0 3.5.3v-3"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M18.8 9.4a3 3 0 0 1 0 5.2"
      />
    </Glyph>
  )
}

function CreativeIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <rect
        x="3"
        y="7"
        width="12"
        height="10"
        rx="2"
        fill="currentColor"
        fillOpacity="0.28"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <path stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" d="M6 10.2h3.2M6 13.8h5.5" />
      <circle cx="17.2" cy="6.2" r="3.2" fill="currentColor" fillOpacity="0.45" stroke="currentColor" strokeWidth="1.5" />
      <path stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" d="M17.2 4.6v3.2M15.6 6.2h3.2" />
      <path stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" d="M17.2 14.2v4" />
    </Glyph>
  )
}

function AudienceIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <circle cx="9" cy="8" r="3.2" fill="currentColor" fillOpacity="0.35" stroke="currentColor" strokeWidth="1.6" />
      <path
        fill="currentColor"
        fillOpacity="0.22"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        d="M3.5 18.5c0-3 2.4-5.2 5.5-5.2s5.5 2.2 5.5 5.2"
      />
      <circle cx="16.8" cy="7.2" r="2.4" fill="currentColor" fillOpacity="0.45" stroke="currentColor" strokeWidth="1.5" />
      <path
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        d="M15.4 13.2c2.2.2 4 2 4 4.4"
      />
    </Glyph>
  )
}

function SchemaIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        d="M8.2 4C5.6 4 6.2 10.2 3.5 12c2.7 1.8 2.1 8 4.7 8"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        d="M15.8 4c2.6 0 2 6.2 4.7 8-2.7 1.8-2.1 8-4.7 8"
      />
      <circle cx="12" cy="12" r="2.6" fill="currentColor" fillOpacity="0.4" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="12" cy="12" r="0.9" fill="currentColor" />
    </Glyph>
  )
}

function PageSyncIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        fillOpacity="0.22"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
        d="M5.5 9.2V4h8.2L17.5 7.8v5"
      />
      <path stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" d="M13.7 4v3.8H17.5" />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M5 16.2a4 4 0 0 0 6.8 2.2M19 13.8a4 4 0 0 0-6.8-2.2"
      />
      <path stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" d="M5 13.2v3.2h3.2M19 16.8v-3.2h-3.2" />
    </Glyph>
  )
}

function ForecastIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" d="M4 19V5M4 19h16" />
      <path
        fill="currentColor"
        fillOpacity="0.2"
        d="M6.5 15.5 10.2 11l3 2.4 4.8-6.2V19H6.5Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M6.5 15.5 10.2 11l3 2.4 4.8-6.2"
      />
      <path stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" d="M15 7.2h3.2V10" />
    </Glyph>
  )
}

function SpamShieldIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        fillOpacity="0.25"
        d="M12 2.8 19 5.4v5.8c0 4-2.9 7.5-7 8.8-4.1-1.3-7-4.8-7-8.8V5.4L12 2.8Z"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
        d="M12 2.8 19 5.4v5.8c0 4-2.9 7.5-7 8.8-4.1-1.3-7-4.8-7-8.8V5.4L12 2.8Z"
      />
      <path stroke="currentColor" strokeWidth="2" strokeLinecap="round" d="M8.6 14.2 15.4 8.2" />
      <circle cx="12" cy="11.2" r="1.1" fill="currentColor" />
    </Glyph>
  )
}

function AuditIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        fillOpacity="0.2"
        d="M4.2 16.2a8.5 8.5 0 1 1 15.6 0"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        d="M4.2 16.2a8.5 8.5 0 1 1 15.6 0"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        d="M12 16.2 16.2 9.4"
      />
      <circle cx="12" cy="16.2" r="1.3" fill="currentColor" />
      <path stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" d="M12 5.2v1.8M5.8 8.4l1.3.8M18.2 8.4l-1.3.8" />
    </Glyph>
  )
}

function GenericAgentIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <rect
        x="5.5"
        y="7"
        width="13"
        height="11"
        rx="2.5"
        fill="currentColor"
        fillOpacity="0.28"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <circle cx="9.2" cy="12" r="1.2" fill="currentColor" />
      <circle cx="14.8" cy="12" r="1.2" fill="currentColor" />
      <path stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" d="M12 4.5v2.5M8.5 5.2 10 7M15.5 5.2 14 7" />
    </Glyph>
  )
}

const AGENT_ICONS: Record<string, (props: GlyphProps) => ReactNode> = {
  aeo_qa_injector: AnswerIcon,
  backlink_node_discovery: NodesIcon,
  click_fraud_controller: ClickShieldIcon,
  competitor_link_monitor: WatchLinkIcon,
  digital_pr_outreach: OutreachIcon,
  dynamic_creative_optimizer: CreativeIcon,
  first_party_audience_modeler: AudienceIcon,
  knowledge_graph_schema: SchemaIcon,
  on_page_seo_sync: PageSyncIcon,
  predictive_budget_engine: ForecastIcon,
  referral_spam_guard: SpamShieldIcon,
  technical_seo_auditor: AuditIcon,
}

export function AgentIcon({ slug, size = 34 }: { slug: string; size?: number }) {
  const theme = AGENT_THEMES[slug] ?? FALLBACK_THEME
  const Component = AGENT_ICONS[slug] ?? GenericAgentIcon
  const glyphSize = Math.max(16, Math.round(size * 0.58))
  const style = {
    width: size,
    height: size,
    color: theme.ink,
    background: `linear-gradient(145deg, ${theme.from} 0%, ${theme.to} 100%)`,
    boxShadow: `0 6px 14px -6px ${theme.glow}, inset 0 1px 0 rgba(255,255,255,0.22)`,
  } as CSSProperties

  return (
    <span className="connector-icon agent-icon agent-icon-rich" style={style}>
      {Component({ size: glyphSize })}
    </span>
  )
}
