/**
 * Generates src/components/brand-icons.ts from simple-icons.
 *
 * Run after changing the map below or bumping simple-icons:
 *
 *     node scripts/generate-brand-icons.mjs
 *
 * Product-specific marks that remain in simple-icons (CC0). Generic or
 * trademark-removed brands (Google Business Profile, LinkedIn, Slack,
 * Microsoft, Adobe, Salesforce, Magento, OpenAI, Bing, …) are NOT listed
 * here — ConnectorIcon loads those from official-logos.ts (product favicon).
 */
import { writeFileSync } from 'node:fs'
import * as si from 'simple-icons'

/** connector slug → simple-icons export name (product-specific marks only). */
const MARKS = {
  wordpress: 'siWordpress',
  shopify: 'siShopify',
  webflow: 'siWebflow',
  github: 'siGithub',
  bitbucket: 'siBitbucket',
  google_ads: 'siGoogleads',
  meta_ads: 'siMeta',
  tiktok_ads: 'siTiktok',
  google_analytics_4: 'siGoogleanalytics',
  search_console: 'siGooglesearchconsole',
  hubspot: 'siHubspot',
  perplexity: 'siPerplexity',
  google_gemini: 'siGooglegemini',
  anthropic_claude: 'siClaude',
  zapier_webhooks: 'siZapier',
  pagespeed_insights: 'siPagespeedinsights',
}

const entries = []
for (const [slug, key] of Object.entries(MARKS)) {
  const icon = si[key]
  if (!icon) {
    throw new Error(
      `simple-icons has no ${key} (for ${slug}) — the export was renamed or the brand was removed`,
    )
  }
  entries.push(
    `  ${slug}: {\n` +
      `    title: ${JSON.stringify(icon.title)},\n` +
      `    hex: '#${icon.hex}',\n` +
      `    path: '${icon.path}',\n` +
      `  },`,
  )
}

const version = JSON.parse(
  (await import('node:fs')).readFileSync('node_modules/simple-icons/package.json', 'utf8'),
).version

writeFileSync(
  'src/components/brand-icons.ts',
  `/**
 * GENERATED — do not edit. Run \`node scripts/generate-brand-icons.mjs\`.
 *
 * Official brand marks from simple-icons ${version} (CC0 1.0). Each path is a
 * single 24x24 filled shape. Mis-mapped brands (e.g. Google Business Profile)
 * use official-logos.ts instead.
 */
export type BrandMark = { title: string; hex: string; path: string }

export const BRAND_MARKS: Record<string, BrandMark> = {
${entries.join('\n')}
}
`,
  'utf8',
)
console.log(`brand-icons.ts: ${entries.length} marks from simple-icons ${version}`)
