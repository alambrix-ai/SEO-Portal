import { defineConfig } from '@playwright/test'

const PORT = Number(process.env.RESPONSIVE_PORT || 5173)
const BASE = process.env.RESPONSIVE_BASE_URL || `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: '.',
  testMatch: 'responsive.spec.mjs',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: BASE,
    viewport: { width: 1280, height: 800 },
    trace: 'off',
    screenshot: 'off',
  },
})
