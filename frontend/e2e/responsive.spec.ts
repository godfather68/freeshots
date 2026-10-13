import { expect, test } from '@playwright/test'
import { audit, boot, noHorizontalScroll } from './helpers'
import { board } from '../src/test-fixtures'

// Five widths x two themes: no horizontal scroll, zero axe violations, screenshots for review in test-results/.
const WIDTHS = [
  { name: 'mobile', width: 360, height: 780 },
  { name: 'tablet', width: 768, height: 1000 },
  { name: 'laptop', width: 1024, height: 800 },
  { name: 'desktop', width: 1366, height: 800 },
  { name: 'wide', width: 1920, height: 1080 },
]
const CL = board.fixtures.find((f) => f.league === 'uefa-champions-league' && f.data_quality === 'partial')!
const LEAGUE_DAY = '2026-10-12'

for (const scheme of ['light', 'dark'] as const) {
  for (const w of WIDTHS) {
    test.describe(`${w.name} ${w.width}px, ${scheme}`, () => {
      test.use({ colorScheme: scheme, viewport: { width: w.width, height: w.height } })

      test('board, Champions League day with panel', async ({ page }) => {
        await boot(page, `#/?date=2026-10-13&m=${CL.id}`)
        await expect(page.locator('#panel-h')).toContainText(CL.home)
        await noHorizontalScroll(page)
        await audit(page, `cl ${w.name} ${scheme}`)
        await page.screenshot({ path: `test-results/shot-${w.name}-${w.width}-${scheme}-cl.png`, fullPage: w.width < 700 ? false : true })
      })

      test('board, league day', async ({ page }) => {
        await boot(page, `#/?date=${LEAGUE_DAY}`)
        await expect(page.locator('a.mlink').first()).toBeVisible()
        await noHorizontalScroll(page)
        await audit(page, `board ${w.name} ${scheme}`)
        await page.screenshot({ path: `test-results/shot-${w.name}-${w.width}-${scheme}-board.png`, fullPage: true })
      })
    })
  }
}
