import { defineConfig } from '@playwright/test'

// Audits run against the production build served by `vite preview`. data/*.json is intercepted in the tests
// and answered from src/test-fixtures, so results do not depend on the real published data.
export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  reporter: [['list']],
  timeout: 60_000,
  use: {
    baseURL: 'http://127.0.0.1:4173/',
    timezoneId: 'Africa/Brazzaville',
    ...(process.env.CHROMIUM_PATH ? { launchOptions: { executablePath: process.env.CHROMIUM_PATH } } : {}),
  },
  webServer: { command: 'npm run preview', url: 'http://127.0.0.1:4173/', reuseExistingServer: true, timeout: 60_000 },
})
