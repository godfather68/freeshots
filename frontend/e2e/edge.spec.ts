import { expect, test } from '@playwright/test'
import { audit, boot, noHorizontalScroll } from './helpers'

// fx001 has team curves; fx003 carries a full `explain` block; fx004 a partial one.
const sizes = [
  { name: 'desktop', width: 1280, height: 900 },
  { name: 'mobile', width: 380, height: 800 },
]

for (const scheme of ['light', 'dark'] as const) {
  for (const s of sizes) {
    test.describe(`${s.name}, ${scheme}`, () => {
      test.use({ colorScheme: scheme, viewport: { width: s.width, height: s.height } })

      test('odds check: result, error, out of range, My checks', async ({ page }) => {
        await boot(page, '#/match/fx001?date=2026-10-12&pt=edge&ln=24.5&od=1.85')
        await expect(page.locator('.edgeres')).toContainText('Model chance of Over 24.5')
        await expect(page.locator('.disclaimer').first()).toContainText('Estimates only, not advice')
        await noHorizontalScroll(page)
        await audit(page, 'odds check result')

        await page.getByRole('button', { name: 'Under' }).click()
        await expect(page.locator('.edge')).toContainText('less tested')
        await audit(page, 'odds check under')

        const odds = page.getByLabel(/Bookmaker odds for/)
        await odds.fill('0.5')
        await expect(page.getByRole('alert')).toContainText('above 1.00')
        await audit(page, 'odds check error')

        await boot(page, '#/match/fx001?date=2026-10-12&pt=edge&ln=60.5&od=2')
        await expect(page.locator('.edgeres')).toContainText('do not extrapolate')
        await audit(page, 'odds check out of range')

        await boot(page, '#/match/fx001?date=2026-10-12&pt=edge&ln=24.5&od=2')
        await page.getByRole('button', { name: 'Save to My checks' }).click()
        await page.getByRole('link', { name: 'Open My checks' }).click()
        await expect(page.getByRole('heading', { name: 'My checks', level: 1 })).toBeVisible()
        await expect(page.getByText('Waiting for the result')).toBeVisible()
        await noHorizontalScroll(page)
        await audit(page, 'my checks')
        await page.screenshot({ path: `test-results/checks-${s.name}-${scheme}.png`, fullPage: true })
      })

      test('why this number: full, partial, none', async ({ page }) => {
        await boot(page, '#/match/fx003?date=2026-10-12')
        await expect(page.locator('.why')).toContainText('= about 33 shots expected')
        await expect(page.locator('.why .wf-row.down')).toHaveCount(1)
        await noHorizontalScroll(page)
        await audit(page, 'why full')
        await page.screenshot({ path: `test-results/why-${s.name}-${scheme}.png`, fullPage: true })
        await boot(page, '#/match/fx004?date=2026-10-12')
        await expect(page.locator('.why')).toContainText('Match tempo')
        await audit(page, 'why partial')
        await boot(page, '#/match/fx002?date=2026-10-12')
        await expect(page.locator('.why')).toHaveCount(0)
      })
    })
  }
}
