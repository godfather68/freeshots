import fs from 'node:fs'
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { audit, noHorizontalScroll } from './helpers'

// Smoke test against the real published files (public/data), when present. No interception.
const real = fs.existsSync(path.resolve(import.meta.dirname, '../public/data/board.json'))
test.skip(!real, 'public/data/board.json not generated yet')
test.setTimeout(240_000) // several axe runs per test; slow on busy machines

for (const scheme of ['light', 'dark'] as const) {
  test(`real data: list, panel, track (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme })
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.clock.setFixedTime(new Date('2026-10-12T16:00:00Z'))
    await page.goto('/#/')
    await expect(page.locator('a.mlink').first()).toBeVisible()
    expect(await page.locator('a.mlink').count()).toBeGreaterThan(0)
    await audit(page, `real list ${scheme}`)
    await page.screenshot({ path: `test-results/real-list-${scheme}.png`, fullPage: true })
    await page.locator('a.mlink').first().click()
    await expect(page.locator('#panel-h')).toBeVisible()
    await audit(page, `real panel ${scheme}`)
    await page.getByRole('tab', { name: 'Teams' }).click()
    await audit(page, `real teams ${scheme}`)
    await page.goto('/#/track')
    await expect(page.getByRole('heading', { level: 1, name: 'Track record' })).toBeVisible()
    await audit(page, `real track ${scheme}`)
    await page.setViewportSize({ width: 380, height: 800 })
    await page.goto('/#/')
    await noHorizontalScroll(page)
    await audit(page, `real mobile ${scheme}`)
  })
}
