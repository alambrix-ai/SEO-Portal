/**
 * The mark on an agent card.
 *
 * Drawn here rather than borrowed: an agent is this platform's own thing, so
 * these are thin-stroke line icons at 1.5 in exactly the geometry the design
 * system uses for the sidebar. Same tile as the connector cards, so the two
 * screens sit next to each other without one looking heavier.
 *
 * One per registered agent, keyed by slug. Each glyph says what the agent
 * *does* — a shield for the two that block things, a graph for the two that
 * work on link structure — because twelve cards distinguished only by a
 * generic robot icon would be twelve cards distinguished only by their text.
 */
import type { SVGProps } from 'react'

type Props = SVGProps<SVGSVGElement> & { size?: number }

function Glyph({ size = 18, children, ...rest }: Props & { children: React.ReactNode }) {
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

/** A question answered — what an AEO answer block is. */
function AnswerIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M3 5.5A1.5 1.5 0 0 1 4.5 4h11A1.5 1.5 0 0 1 17 5.5v6A1.5 1.5 0 0 1 15.5 13H7l-4 3.5V5.5Z" />
      <path d="M8.4 7.1a1.6 1.6 0 0 1 3.1.5c0 1.1-1.5 1.2-1.5 2.3" />
      <line x1="10" y1="11.4" x2="10" y2="11.5" />
    </Glyph>
  )
}

/** Nodes found and joined — link prospecting. */
function NodesIcon(props: Props) {
  return (
    <Glyph {...props}>
      <circle cx="4" cy="6" r="2" />
      <circle cx="16" cy="4.5" r="2" />
      <circle cx="15" cy="15" r="2" />
      <circle cx="6" cy="14" r="2" />
      <path d="M5.9 6.6 14 4.9M5.7 12.3 13.7 6M8 14.2l5 .7" />
    </Glyph>
  )
}

/** A shield with a pointer in it — fraudulent clicks, stopped. */
function ClickShieldIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M10 2.6l5.6 2v4.9c0 3.2-2.3 6-5.6 7-3.3-1-5.6-3.8-5.6-7V4.6l5.6-2Z" />
      <path d="M8.4 6.9l4.2 4.3-2 .3-.6 2-1.6-6.6Z" />
    </Glyph>
  )
}

/** A link, watched. */
function WatchLinkIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M8.4 5.6 10 4a2.8 2.8 0 0 1 4 4l-1.6 1.6" />
      <path d="M11.6 11.4 10 13" />
      <path d="M2 15.5c1.8-2.6 5.4-2.6 7.2 0-1.8 2.6-5.4 2.6-7.2 0Z" />
      <circle cx="5.6" cy="15.5" r="1" />
    </Glyph>
  )
}

/** A megaphone — outreach. */
function OutreachIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M3 8.4 14 4.4v11.2L3 11.6V8.4Z" />
      <path d="M5.6 10.6v3.6a1.4 1.4 0 0 0 2.8 0v-2.6" />
      <path d="M16.2 8.2a2.4 2.4 0 0 1 0 3.6" />
    </Glyph>
  )
}

/** Layered creatives, with the one that won on top. */
function CreativeIcon(props: Props) {
  return (
    <Glyph {...props}>
      <rect x="2.5" y="6" width="10" height="8" rx="1" />
      <path d="M5.5 8.5h2M5.5 11.5h4" />
      <path d="M15 4.5v4M13 6.5h4" />
      <path d="M15.5 12v4.5" />
      <path d="M14 14.2h3" />
    </Glyph>
  )
}

/** People, grouped — a modelled audience. */
function AudienceIcon(props: Props) {
  return (
    <Glyph {...props}>
      <circle cx="7.5" cy="7" r="2.4" />
      <path d="M3.2 15.5c0-2.4 1.9-4.2 4.3-4.2s4.3 1.8 4.3 4.2" />
      <circle cx="14.4" cy="6.2" r="1.8" />
      <path d="M13.2 11.5c2 .1 3.6 1.7 3.6 4" />
    </Glyph>
  )
}

/** Braces around a node — structured data. */
function SchemaIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M7 3.5C5 3.5 5.4 8.6 3.2 10c2.2 1.4 1.8 6.5 3.8 6.5" />
      <path d="M13 3.5c2 0 1.6 5.1 3.8 6.5-2.2 1.4-1.8 6.5-3.8 6.5" />
      <circle cx="10" cy="10" r="1.8" />
    </Glyph>
  )
}

/** A page, being written back. */
function PageSyncIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M4.5 8.5V3.5h7l3.5 3.5v5" />
      <path d="M11.5 3.5V7H15" />
      <path d="M4 14.5a3.4 3.4 0 0 0 5.8 1.9" />
      <path d="M16 12.5a3.4 3.4 0 0 0-5.8-1.9" />
      <path d="M4 11.8v2.7h2.7M16 15.2v-2.7h-2.7" />
    </Glyph>
  )
}

/** A forecast line over the budget it is allocating. */
function ForecastIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M3 16.5V4" />
      <path d="M3 16.5h14" />
      <path d="M5.6 13.4l3-3.4 2.6 2 4.2-5" />
      <path d="M12.4 7h3v3" />
    </Glyph>
  )
}

/** A shield turning traffic away — referral spam. */
function SpamShieldIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M10 2.6l5.6 2v4.9c0 3.2-2.3 6-5.6 7-3.3-1-5.6-3.8-5.6-7V4.6l5.6-2Z" />
      <line x1="7.6" y1="11.6" x2="12.4" y2="7.2" />
    </Glyph>
  )
}

/** A gauge under a wrench — the technical audit. */
function AuditIcon(props: Props) {
  return (
    <Glyph {...props}>
      <path d="M3.4 14.5a7.6 7.6 0 1 1 13.2 0" />
      <path d="M10 14.4 13.2 9" />
      <line x1="10" y1="4.2" x2="10" y2="5.6" />
      <line x1="4.9" y1="7.2" x2="6.1" y2="7.9" />
      <line x1="15.1" y1="7.2" x2="13.9" y2="7.9" />
    </Glyph>
  )
}

const AGENT_ICONS: Record<string, (props: Props) => React.ReactNode> = {
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

/** Fallback for an agent registered after this file was last touched. */
function GenericAgentIcon(props: Props) {
  return (
    <Glyph {...props}>
      <rect x="5" y="5" width="10" height="10" rx="0.5" />
      <line x1="10" y1="1.5" x2="10" y2="5" />
      <line x1="10" y1="15" x2="10" y2="18.5" />
    </Glyph>
  )
}

/** The tile, matching `ConnectorIcon` so the two screens agree. */
export function AgentIcon({ slug, size = 34 }: { slug: string; size?: number }) {
  const Component = AGENT_ICONS[slug] ?? GenericAgentIcon
  return (
    <span className="connector-icon agent-icon" style={{ width: size, height: size }}>
      {Component({ size: Math.round(size * 0.56) })}
    </span>
  )
}
