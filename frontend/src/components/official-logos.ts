/**
 * Official product websites for branded connectors.
 *
 * Preference order in ConnectorIcon:
 * 1. Product-domain favicon (this map) — the brand's own published icon.
 * 2. simple-icons SVG when we keep a product-specific path in brand-icons.ts.
 * 3. Drawn glyphs only for non-brand connectors (custom API, crawler, DSP, SMTP).
 *
 * Domains are the product's public site — not invented colours or lettermarks.
 * Google Business Profile uses business.google.com, not the generic Google mark.
 */
export const OFFICIAL_LOGO_DOMAINS: Record<string, { domain: string; title: string }> = {
  // CMS / site
  wordpress: { domain: 'wordpress.org', title: 'WordPress' },
  shopify: { domain: 'shopify.com', title: 'Shopify' },
  webflow: { domain: 'webflow.com', title: 'Webflow' },
  magento: { domain: 'magento.com', title: 'Magento' },
  // Repos
  github: { domain: 'github.com', title: 'GitHub' },
  bitbucket: { domain: 'bitbucket.org', title: 'Bitbucket' },
  // Google
  google_ads: { domain: 'ads.google.com', title: 'Google Ads' },
  google_analytics_4: { domain: 'analytics.google.com', title: 'Google Analytics' },
  search_console: { domain: 'search.google.com', title: 'Google Search Console' },
  google_business_profile: { domain: 'business.google.com', title: 'Google Business Profile' },
  google_gemini: { domain: 'gemini.google.com', title: 'Google Gemini' },
  pagespeed_insights: { domain: 'pagespeed.web.dev', title: 'PageSpeed Insights' },
  // Ads / social
  meta_ads: { domain: 'business.facebook.com', title: 'Meta Ads' },
  tiktok_ads: { domain: 'ads.tiktok.com', title: 'TikTok Ads' },
  linkedin_ads: { domain: 'linkedin.com', title: 'LinkedIn Ads' },
  microsoft_ads: { domain: 'ads.microsoft.com', title: 'Microsoft Advertising' },
  // Analytics / CRM / mail / collab
  adobe_analytics: { domain: 'adobe.com', title: 'Adobe Analytics' },
  hubspot: { domain: 'hubspot.com', title: 'HubSpot' },
  salesforce: { domain: 'salesforce.com', title: 'Salesforce' },
  slack: { domain: 'slack.com', title: 'Slack' },
  bing_webmaster: { domain: 'bing.com', title: 'Bing Webmaster' },
  zapier_webhooks: { domain: 'zapier.com', title: 'Zapier' },
  // AI models
  openai: { domain: 'openai.com', title: 'OpenAI' },
  anthropic_claude: { domain: 'anthropic.com', title: 'Anthropic Claude' },
  perplexity: { domain: 'perplexity.ai', title: 'Perplexity' },
}

/** Favicon published by the brand's own site (128px when available). */
export function officialLogoUrl(domain: string): string {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=128`
}

/** Secondary source if Google has no entry for that host. */
export function officialLogoFallbackUrl(domain: string): string {
  return `https://icon.horse/icon/${encodeURIComponent(domain)}`
}
