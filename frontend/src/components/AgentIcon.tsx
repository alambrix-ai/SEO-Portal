/**
 * Agent marks - one distinctive tile per agent.
 *
 * Neo-brutalist tiles: ink border, hard offset shadow, vivid brand gradients,
 * and bold filled glyphs so the Agents grid reads as characters, not grey
 * squares.
 */
import type { CSSProperties, ReactNode, SVGProps } from 'react'

type GlyphProps = SVGProps<SVGSVGElement> & { size?: number }



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

function MapPinIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path fill="currentColor" opacity="0.3" d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 1 1 13 0c0 5.4-6.5 11-6.5 11z" />
      <path stroke="currentColor" strokeWidth="2" strokeLinejoin="round" d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 1 1 13 0c0 5.4-6.5 11-6.5 11z" />
      <circle cx="12" cy="10" r="2.4" fill="currentColor" />
    </Glyph>
  )
}

function ReviewReplyIcon({ size }: GlyphProps) {
  return (
    <Glyph size={size}>
      <path fill="currentColor" opacity="0.3" d="M4 5h16v11H9l-5 4z" />
      <path stroke="currentColor" strokeWidth="2" strokeLinejoin="round" d="M4 5h16v11H9l-5 4z" />
      <path fill="currentColor" d="M12 7.6l.9 1.9 2.1.3-1.5 1.4.4 2.1-1.9-1-1.9 1 .4-2.1-1.5-1.4 2.1-.3z" />
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
  local_search_tracker: MapPinIcon,
  review_responder: ReviewReplyIcon,
}

/** Product-area tint for each agent: one restrained hue per area. */
const AREA_TINT: Record<string, { bg: string; fg: string; border: string }> = {
  seo: { bg: '#eff6ff', fg: '#1d4ed8', border: '#dbeafe' },
  offpage: { bg: '#f5f3ff', fg: '#6d28d9', border: '#ede9fe' },
  ads: { bg: '#f0fdfa', fg: '#0f766e', border: '#ccfbf1' },
  guard: { bg: '#f8fafc', fg: '#334155', border: '#e2e8f0' },
  local: { bg: '#fff7ed', fg: '#c2410c', border: '#ffedd5' },
}

const AGENT_AREA: Record<string, keyof typeof AREA_TINT> = {
  aeo_qa_injector: 'seo',
  knowledge_graph_schema: 'seo',
  on_page_seo_sync: 'seo',
  technical_seo_auditor: 'seo',
  backlink_node_discovery: 'offpage',
  competitor_link_monitor: 'offpage',
  digital_pr_outreach: 'offpage',
  dynamic_creative_optimizer: 'ads',
  first_party_audience_modeler: 'ads',
  predictive_budget_engine: 'ads',
  click_fraud_controller: 'guard',
  referral_spam_guard: 'guard',
  local_search_tracker: 'local',
  review_responder: 'local',
}

export function AgentIcon({ slug, size = 44 }: { slug: string; size?: number }) {
  const tint = AREA_TINT[AGENT_AREA[slug] ?? 'guard'] ?? AREA_TINT.guard!
  const Component = AGENT_ICONS[slug] ?? GenericAgentIcon
  const glyphSize = Math.max(16, Math.round(size * 0.55))
  const style = {
    width: size,
    height: size,
    color: tint.fg,
    background: tint.bg,
    borderColor: tint.border,
    ['--agent-detail' as string]: tint.fg,
  } as CSSProperties

  return (
    <span className="connector-icon agent-icon agent-icon-flat" style={style}>
      {Component({ size: glyphSize })}
    </span>
  )
}
