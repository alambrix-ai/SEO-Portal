/**
 * Official product websites for connectors whose marks are not in simple-icons.
 *
 * simple-icons (CC0) covers most vendors. A few brands — LinkedIn, Slack,
 * Microsoft, Adobe, Salesforce, Magento, OpenAI, Bing — were removed from that
 * set on trademark grounds, so we do not invent lettermarks or redraw their
 * logos. Instead the tile loads the favicon that site already publishes
 * (via Google's favicon service), which is the brand's own asset.
 *
 * Domains are the product's public site, not guesses at a colour hex.
 */
export const OFFICIAL_LOGO_DOMAINS: Record<string, { domain: string; title: string }> = {
  linkedin_ads: { domain: 'linkedin.com', title: 'LinkedIn' },
  slack: { domain: 'slack.com', title: 'Slack' },
  microsoft_ads: { domain: 'ads.microsoft.com', title: 'Microsoft Advertising' },
  adobe_analytics: { domain: 'adobe.com', title: 'Adobe' },
  salesforce: { domain: 'salesforce.com', title: 'Salesforce' },
  magento: { domain: 'magento.com', title: 'Magento' },
  openai: { domain: 'openai.com', title: 'OpenAI' },
  bing_webmaster: { domain: 'bing.com', title: 'Bing' },
}

/** Favicon published by the brand's own site (128px when available). */
export function officialLogoUrl(domain: string): string {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=128`
}

/** Secondary source if Google has no entry for that host. */
export function officialLogoFallbackUrl(domain: string): string {
  return `https://icon.horse/icon/${encodeURIComponent(domain)}`
}
