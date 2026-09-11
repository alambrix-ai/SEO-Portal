/**
 * Agent marks - one distinctive tile per agent.
 *
 * Neo-brutalist tiles: ink border, hard offset shadow, vivid brand gradients,
 * and bold filled glyphs so the Agents grid reads as characters, not grey
 * squares.
 */
import type { CSSProperties, ReactNode, SVGProps } from 'react'

type GlyphProps = SVGProps<SVGSVGElement> & { size?: number }

type Theme = {
  from: string
  mid: string
  to: string
  ink: string
  /** Inner glyph accents that contrast the tile ink. */
  detail: string
}

const AGENT_THEMES: Record<string, Theme> = {
  aeo_qa_injector: {
    from: '#6d4570',
    mid: '#8f5f8a',
    to: '#5b3e58',
    ink: '#fff8f0',
    detail: '#3d2a38',
  },
  backlink_node_discovery: {
    from: '#b07a45',
    mid: '#966b3f',
    to: '#6e4a28',
    ink: '#fff8f0',
    detail: '#3d2a18',
  },
  click_fraud_controller: {
    from: '#5c3a22',
    mid: '#3d2a18',
    to: '#2a1c10',
    ink: '#f6efe6',
    detail: '#1a120c',
  },
  competitor_link_monitor: {
    from: '#6a8f74',
    mid: '#7d9a83',
    to: '#4a3247',
    ink: '#faf8f4',
    detail: '#243028',
  },
  digital_pr_outreach: {
    from: '#e0c27a',
    mid: '#d4b56a',
    to: '#a87848',
    ink: '#1a1a1a',
    detail: '#fff8f0',
  },
  dynamic_creative_optimizer: {
    from: '#9a6a8c',
    mid: '#865a78',
    to: '#c09060',
    ink: '#fff8f0',
    detail: '#3d2a38',
  },
  first_party_audience_modeler: {
    from: '#8fad96',
    mid: '#7d9a83',
    to: '#4f5f55',
    ink: '#faf8f4',
    detail: '#243028',
  },
  knowledge_graph_schema: {
    from: '#8a6238',
    mid: '#5c4026',
    to: '#3d2a18',
    ink: '#f6efe6',
    detail: '#1a120c',
  },
  on_page_seo_sync: {
    from: '#8fa97f',
    mid: '#6f8f68',
    to: '#4f6a48',
    ink: '#fff8f0',
    detail: '#243028',
  },
  predictive_budget_engine: {
    from: '#e8c96e',
    mid: '#d4b56a',
    to: '#b8893f',
    ink: '#1a1a1a',
    detail: '#fff8f0',
  },
  referral_spam_guard: {
    from: '#6b455f',
    mid: '#4a3247',
    to: '#2f1f2c',
    ink: '#f6efe6',
    detail: '#1a120c',
  },
  technical_seo_auditor: {
    from: '#8a4f4a',
    mid: '#6e3d3a',
    to: '#4a2a28',
    ink: '#fff8f0',
    detail: '#2a1614',
  },
}

const FALLBACK_THEME: Theme = {
  from: '#f3ebe0',
  mid: '#e5d8c6',
  to: '#cbb89a',
  ink: '#5c4026',
  detail: '#fff8f0',
}

function Glyph({
  size = 22,
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
        d="M4 5.2A2.2 2.2 0 0 1 6.2 3h11.6A2.2 2.2 0 0 1 20 5.2v7.1A2.2 2.2 0 0 1 17.8 14.5H9.4L4 19V5.2Z"
        opacity="0.95"
      />
      <path
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeOpacity="0.35"
        d="M8.4 7.4h7.2M8.4 10.6h4.8"
      />
      <circle cx="17.6" cy="17.2" r="3.4" fill="currentColor" opacity="0.35" />
      <path
        stroke="var(--agent-detail)"
        strokeWidth="1.8"
        strokeLinecap="round"
        d="M16.4 17.2h2.4M17.6 16v2.4"
      />
    </Glyph>
  )
}

function NodesIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        d="M7.4 8.4 15.2 6.4M7.2 15.4 15.6 8.8M9.2 16.6l5.6.8"
      />
      <circle cx="5.6" cy="7.6" r="2.8" fill="currentColor" />
      <circle cx="17.8" cy="5.6" r="2.6" fill="currentColor" opacity="0.85" />
      <circle cx="17" cy="17" r="2.7" fill="currentColor" opacity="0.7" />
      <circle cx="7.2" cy="16.4" r="2.5" fill="currentColor" />
    </Glyph>
  )
}

function ClickShieldIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        d="M12 2.6 19.2 5.4v5.9c0 4.2-3 7.8-7.2 9.2-4.2-1.4-7.2-5-7.2-9.2V5.4L12 2.6Z"
      />
      <path
        stroke="var(--agent-detail)"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M8.6 12.1 11 14.5l4.6-5"
      />
    </Glyph>
  )
}

function WatchLinkIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        d="M8.8 7.4 10.8 5.4a3.6 3.6 0 0 1 5.1 5.1l-1.6 1.6"
      />
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        d="M15.2 13.8 13.2 15.8a3.6 3.6 0 0 1-5.1-5.1l1.2-1.2"
      />
      <path
        fill="currentColor"
        d="M3.4 18.4c2.1-3.2 6.6-3.2 8.7 0-2.1 3.2-6.6 3.2-8.7 0Z"
      />
      <circle cx="7.8" cy="18.4" r="1.35" fill="var(--agent-detail)" />
    </Glyph>
  )
}

function OutreachIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path fill="currentColor" d="M3.2 9.4 16.8 4.2v15.6L3.2 14.6V9.4Z" />
      <path
        stroke="var(--agent-detail)"
        strokeWidth="1.9"
        strokeLinecap="round"
        d="M7 12.4v3.8a1.7 1.7 0 0 0 3.3.2v-2.8"
      />
      <path
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        d="M19 9.2a3.2 3.2 0 0 1 0 5.6"
        opacity="0.75"
      />
    </Glyph>
  )
}

function CreativeIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <rect x="2.8" y="7" width="12.4" height="10.4" rx="2.2" fill="currentColor" />
      <path stroke="var(--agent-detail)" strokeWidth="1.8" strokeLinecap="round" d="M5.6 10.2h4M5.6 13.8h6.4" />
      <circle cx="17.4" cy="6.2" r="3.5" fill="currentColor" opacity="0.9" />
      <path stroke="var(--agent-detail)" strokeWidth="1.9" strokeLinecap="round" d="M17.4 4.6v3.2M15.8 6.2h3.2" />
    </Glyph>
  )
}

function AudienceIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <circle cx="8.6" cy="7.6" r="3.4" fill="currentColor" />
      <path
        fill="currentColor"
        d="M2.8 18.8c0-3.2 2.6-5.5 5.8-5.5s5.8 2.3 5.8 5.5"
      />
      <circle cx="16.8" cy="7" r="2.6" fill="currentColor" opacity="0.85" />
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        d="M15.2 13c2.4.3 4.4 2.2 4.4 4.8"
      />
    </Glyph>
  )
}

function SchemaIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        d="M8 3.8C5.2 3.8 5.8 10.4 2.8 12.2c3 1.8 2.4 8.4 5.2 8.4"
      />
      <path
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        d="M16 3.8c2.8 0 2.2 6.6 5.2 8.4-3 1.8-2.4 8.4-5.2 8.4"
      />
      <circle cx="12" cy="12" r="3.1" fill="currentColor" />
      <circle cx="12" cy="12" r="1.05" fill="var(--agent-detail)" />
    </Glyph>
  )
}

function PageSyncIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        d="M5.2 3.8h8.4L17.8 8v4.2H5.2V3.8Z"
      />
      <path stroke="var(--agent-detail)" strokeWidth="1.6" strokeLinejoin="round" d="M13.6 3.8V8H17.8" opacity="0.85" />
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        d="M5 16.4a4.2 4.2 0 0 0 7.1 2.3M19.2 13.6a4.2 4.2 0 0 0-7.1-2.3"
      />
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5 13.2v3.4h3.4M19.2 17v-3.4h-3.4"
      />
    </Glyph>
  )
}

function ForecastIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" d="M4 19.2V4.8M4 19.2h16" />
      <path fill="currentColor" opacity="0.35" d="M6.4 15.6 10.2 11l3.1 2.5 4.9-6.4V19H6.4Z" />
      <path
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M6.4 15.6 10.2 11l3.1 2.5 4.9-6.4"
      />
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15.2 7h3.4v3.4"
      />
    </Glyph>
  )
}

function SpamShieldIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        d="M12 2.6 19.2 5.4v5.9c0 4.2-3 7.8-7.2 9.2-4.2-1.4-7.2-5-7.2-9.2V5.4L12 2.6Z"
      />
      <path stroke="var(--agent-detail)" strokeWidth="2.3" strokeLinecap="round" d="M8.4 14.4 15.6 8" />
      <circle cx="12" cy="11.2" r="1.35" fill="var(--agent-detail)" />
    </Glyph>
  )
}

function AuditIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path
        fill="currentColor"
        opacity="0.35"
        d="M4 16.4a8.8 8.8 0 1 1 16 0"
      />
      <path
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        d="M4 16.4a8.8 8.8 0 1 1 16 0"
      />
      <path
        stroke="currentColor"
        strokeWidth="2.3"
        strokeLinecap="round"
        d="M12 16.4 16.6 9"
      />
      <circle cx="12" cy="16.4" r="1.5" fill="currentColor" />
      <path
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        d="M12 4.8v2M5.4 8.2l1.4.9M18.6 8.2l-1.4.9"
      />
    </Glyph>
  )
}

function GenericAgentIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <rect x="5" y="7" width="14" height="11.5" rx="2.6" fill="currentColor" />
      <circle cx="9.2" cy="12.2" r="1.35" fill="var(--agent-detail)" />
      <circle cx="14.8" cy="12.2" r="1.35" fill="var(--agent-detail)" />
      <path
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        d="M12 4.2v2.6M8.2 5 9.8 7M15.8 5 14.2 7"
      />
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

export function AgentIcon({ slug, size = 44 }: { slug: string; size?: number }) {
  const theme = AGENT_THEMES[slug] ?? FALLBACK_THEME
  const Component = AGENT_ICONS[slug] ?? GenericAgentIcon
  const glyphSize = Math.max(18, Math.round(size * 0.55))
  const style = {
    width: size,
    height: size,
    color: theme.ink,
    ['--agent-detail' as string]: theme.detail,
    background: `linear-gradient(145deg, ${theme.from} 0%, ${theme.mid} 48%, ${theme.to} 100%)`,
  } as CSSProperties

  return (
    <span className="connector-icon agent-icon agent-icon-rich" style={style}>
      {Component({ size: glyphSize })}
    </span>
  )
}
