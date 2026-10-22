/**
 * Plain-language labels for values the API sends as stored identifiers.
 *
 * Only presentation lives here. What an agent does, needs and changes comes
 * from the API (derived from the agent's code), never from this file.
 */

/** Schedule values are stored as-is; these are the words shown for them. */
export const SCHEDULE_LABEL: Record<string, string> = {
  Hourly: 'Every hour',
  'Every 6 hours': 'Every 6 hours',
  Daily: 'Once a day',
  Weekly: 'Once a week',
}

export function scheduleLabel(value: string): string {
  return SCHEDULE_LABEL[value] ?? value
}

/** Agent categories (AgentCategory on the server) as goals. */
export const AGENT_GROUPS: { category: string; title: string; description: string }[] = [
  {
    category: 'Content & Social',
    title: 'Create content',
    description: 'New articles, social posts and emails, written from your brand profile.',
  },
  {
    category: 'Local business',
    title: 'Get found by local customers',
    description: 'Your place in Google Maps searches like “salon near me”, and replies to your Google reviews.',
  },
  {
    category: 'SEO & AEO',
    title: 'Improve your website',
    description: 'Search rankings, answers in AI assistants, and clean analytics.',
  },
  {
    category: 'Off-Page',
    title: 'Get other websites to link to you',
    description: 'Find link opportunities and prepare pitches for your team.',
  },
  {
    category: 'Ads',
    title: 'Run and protect your ads',
    description: 'Ad text, budgets, audiences and fake-click protection.',
  },
]

/** Connector categories as the question a user is answering. */
export const CONNECTOR_CATEGORY: Record<string, { title: string; description: string }> = {
  CMS: {
    title: 'Your website',
    description: 'Where your pages live. Agents read and edit pages through this.',
  },
  'Local business': {
    title: 'Local business (Google Maps)',
    description: 'Your Google Business Profile and Google Maps search. For shops, salons, clinics and other local businesses.',
  },
  'AI models': {
    title: 'AI writing',
    description: 'The AI service agents use to write. You need at least one for most agents.',
  },
  'Analytics & Search': {
    title: 'Analytics & search data',
    description: 'Visits, search performance and links to your site.',
  },
  Analytics: {
    title: 'Analytics & search data',
    description: 'Visits, search performance and links to your site.',
  },
  'Ad Platforms': {
    title: 'Ad accounts',
    description: 'The platforms you advertise on.',
  },
  'Social media': {
    title: 'Social media',
    description: 'Facebook, Instagram, LinkedIn, X and Pinterest accounts the Social Media Manager posts to.',
  },
  'Email marketing': {
    title: 'Email marketing',
    description: 'Where the Email Marketer creates draft campaigns: Mailchimp, Brevo or Klaviyo.',
  },
  CRM: {
    title: 'Customer data (CRM)',
    description: 'Conversions and customer engagement.',
  },
  Collaboration: {
    title: 'Notifications',
    description: 'Where agents send updates, approvals and pitch drafts.',
  },
}

/** The order categories are offered in: what most agents need first. */
export const CONNECTOR_CATEGORY_ORDER = [
  'CMS',
  'Local business',
  'AI models',
  'Analytics & Search',
  'Collaboration',
  'Ad Platforms',
  'Social media',
  'Email marketing',
  'CRM',
]

export function categoryTitle(category: string): string {
  return CONNECTOR_CATEGORY[category]?.title ?? category
}

/** One chip / section per display title, so "Analytics" and "Analytics & Search" do not both appear. */
export function uniqueCategoryChips(names: readonly string[]): { value: string; label: string }[] {
  const seen = new Set<string>()
  const chips: { value: string; label: string }[] = []
  for (const name of names) {
    if (name === 'All') {
      if (!seen.has('All')) {
        seen.add('All')
        chips.push({ value: 'All', label: 'All' })
      }
      continue
    }
    const label = categoryTitle(name)
    if (seen.has(label)) continue
    seen.add(label)
    chips.push({ value: label, label })
  }
  return chips
}

export function categoryMatches(connectorCategory: string, selected: string): boolean {
  if (selected === 'All') return true
  return categoryTitle(connectorCategory) === selected || connectorCategory === selected
}

export function categorySortKey(category: string): number {
  const index = CONNECTOR_CATEGORY_ORDER.indexOf(category)
  if (index !== -1) return index
  const title = categoryTitle(category)
  const byTitle = CONNECTOR_CATEGORY_ORDER.findIndex((key) => CONNECTOR_CATEGORY[key]?.title === title)
  return byTitle === -1 ? CONNECTOR_CATEGORY_ORDER.length : byTitle
}

export const NOTIFY_LABEL: Record<string, string> = {
  None: "Don't notify me",
  Email: 'Email',
  Slack: 'Slack',
}

/** The workspace approval setting, as people read it. Stored values are unchanged. */
export const REVIEW_SETTING_NAME = 'Review before publishing'

/** The API values the setting is stored as (`guardrail` on the server). */
export type ReviewSetting = 'human' | 'hybrid' | 'full'

/** Mirrors GUARDRAIL_NAMES on the server (test_approval_vocabulary.py checks it). */
export const GUARDRAIL_NAME: Record<ReviewSetting, string> = {
  human: 'Always ask me',
  hybrid: 'Ask me for big changes',
  full: 'Publish automatically',
}

/** One line on what each choice means, for screens without the server's detail. */
export const GUARDRAIL_DETAIL: Record<ReviewSetting, string> = {
  human: 'Nothing goes live until you approve it.',
  hybrid: 'Small changes go live on their own. You approve the big ones.',
  full: 'Agents publish changes without asking you.',
}

export function isReviewSetting(value: string): value is ReviewSetting {
  return value === 'human' || value === 'hybrid' || value === 'full'
}

export function reviewSettingName(value: string): string {
  return isReviewSetting(value) ? GUARDRAIL_NAME[value] : value
}

/** The question asked before turning on automatic publishing. */
export const PUBLISH_AUTOMATICALLY_CONFIRM = {
  title: 'Publish automatically?',
  message: 'Agents will publish changes to your website and ad accounts without asking you first.',
  detail: 'You can switch back to "Always ask me" or "Ask me for big changes" at any time.',
  confirmLabel: 'Yes, publish automatically',
  cancelLabel: 'Keep reviewing',
  tone: 'danger' as const,
}

/**
 * Approval types (the `type` the API sends) as words a marketer uses.
 * Agents set a title-case type, or fall back to their action kind.
 */
export const APPROVAL_TYPE_LABEL: Record<string, string> = {
  'Content Rewrite': 'Page text update',
  'AEO Injection': 'Questions and answers for AI search',
  'Schema Patch': 'Search engine page details',
  'PR Pitch': 'Pitch email to a website',
  'Ad Creative': 'New ad',
  'Budget Shift': 'Ad budget change',
  'Audience Activation': 'New ad audience',
  'Traffic Block': 'Block bad ad traffic',
  'Backlink Target': 'Website to ask for a link',
  'Email campaign': 'Email campaign draft',
  'Review Reply': 'Reply to a Google review',
  'SEO Article': 'New article',
  'Search Campaign': 'New search ad campaign',
  'Social Post': 'Social media post',
  publish_rewrite: 'Page text update',
  inject_qa: 'Questions and answers for AI search',
  rebalance_budget: 'Ad budget change',
  activate_audience: 'New ad audience',
  block_referrer: 'Block bad ad traffic',
  block_placement: 'Block bad ad traffic',
  send_pitch: 'Pitch email to a website',
  send_counter_pitch: 'Pitch email to a website',
  reply_review: 'Reply to a Google review',
  record_target: 'Website to ask for a link',
  create_article: 'New article',
  create_email_draft: 'Email campaign draft',
  create_search_campaign: 'New search ad campaign',
}

export function approvalTypeLabel(type: string): string {
  const known = APPROVAL_TYPE_LABEL[type]
  if (known) return known
  // Unknown machine-style kinds ("do_thing") read as words, not identifiers.
  return /^[a-z0-9_]+$/.test(type) ? sentence(type.replace(/_/g, ' ')) : type
}

/** Capitalise a sentence the server wrote in lower case ("no pages were due…"). */
export function sentence(text: string): string {
  const trimmed = (text || '').trim()
  return trimmed ? trimmed.charAt(0).toUpperCase() + trimmed.slice(1) : ''
}

/** What a finished run did, in one line, including runs that never finished. */
export function runOutcome(run: { state: string; error: string; summary: string }): string {
  if (run.error) return sentence(run.error)
  if (run.summary) return sentence(run.summary)
  if (run.state === 'interrupted') return 'Stopped reporting before it finished.'
  return ''
}
