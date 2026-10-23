/**
 * Responsive smoke: Overview (and a few shell routes) at phone → ultrawide.
 *
 * Run (with mock API + Vite already up, or let this file start them):
 *   npx playwright test -c e2e/playwright.config.mjs
 */
import { test, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ARTIFACT_ROOT = process.env.RESPONSIVE_ARTIFACT_DIR
  || '/opt/cursor/artifacts/responsive'
const PHASE = process.env.RESPONSIVE_PHASE || 'after' // before | after

const WIDTHS = [
  { name: '375', width: 375, height: 812 },
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1280', width: 1280, height: 800 },
  { name: '1920', width: 1920, height: 1080 },
]

const ROUTES = [
  { path: '/dashboard', slug: 'overview' },
  { path: '/visibility', slug: 'visibility' },
  { path: '/topics', slug: 'topics' },
  { path: '/agents', slug: 'agents' },
  { path: '/login', slug: 'login', public: true },
]

async function seedSession(page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('automarket.access_token', 'audit-access')
    sessionStorage.setItem('automarket.refresh_token', 'audit-refresh')
  })
}

async function measureOverflow(page) {
  return page.evaluate(() => {
    const doc = document.documentElement
    const body = document.body
    const shell = document.querySelector('.app-shell')
    const main = document.querySelector('.app-main')
    const pageEl = document.querySelector('main.page')
    const scrollWidth = Math.max(doc.scrollWidth, body.scrollWidth)
    const clientWidth = doc.clientWidth
    const mainWidth = main ? main.getBoundingClientRect().width : 0
    const pageWidth = pageEl ? pageEl.getBoundingClientRect().width : 0
    const shellWidth = shell ? shell.getBoundingClientRect().width : 0
    return {
      clientWidth,
      scrollWidth,
      overflowPx: scrollWidth - clientWidth,
      shellWidth,
      mainWidth,
      pageWidth,
      mainRatio: clientWidth ? mainWidth / clientWidth : 0,
    }
  })
}

test.describe(`responsive ${PHASE}`, () => {
  for (const vp of WIDTHS) {
    for (const route of ROUTES) {
      test(`${route.slug} @ ${vp.name}`, async ({ page }) => {
        await page.setViewportSize({ width: vp.width, height: vp.height })
        if (!route.public) await seedSession(page)
        await page.goto(route.path, { waitUntil: 'networkidle' })
        // Allow fit-density ResizeObserver + data fetch to settle.
        await page.waitForTimeout(600)

        const dir = join(ARTIFACT_ROOT, PHASE)
        mkdirSync(dir, { recursive: true })
        const file = join(dir, `${route.slug}_${vp.name}.png`)
        await page.screenshot({ path: file, fullPage: false })

        if (!route.public && vp.width <= 414) {
          // Wait for authenticated shell (mock session) — blank body means the page crashed.
          await expect(page.locator('.app-main')).toBeVisible({ timeout: 15_000 })
          const m = await measureOverflow(page)
          expect(m.overflowPx, `horizontal overflow on ${route.slug}@${vp.width}`).toBeLessThanOrEqual(1)
          expect(m.mainRatio, `main should fill viewport on ${route.slug}@${vp.width}`).toBeGreaterThan(0.9)
          const sidebarBox = await page.locator('.sidebar').boundingBox()
          if (sidebarBox) {
            // Closed drawer sits off-canvas (transformed left of the viewport).
            expect(sidebarBox.x + 2).toBeLessThanOrEqual(0)
          }
        }
      })
    }
  }

  test('overview fills phone width (Dishant regression)', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await seedSession(page)
    await page.goto('/dashboard', { waitUntil: 'networkidle' })
    await page.waitForTimeout(800)
    const m = await measureOverflow(page)
    expect(m.mainRatio).toBeGreaterThan(0.92)
    expect(m.overflowPx).toBeLessThanOrEqual(1)

    // Stat tiles / board should not paint a ~700px intrinsic grid.
    const board = page.locator('.dash-board, .dash-stat-grid').first()
    if (await board.count()) {
      const box = await board.boundingBox()
      expect(box.width).toBeLessThanOrEqual(390)
    }
  })
})
