import { expect, test } from '@playwright/test'
import { trackFull } from '../src/test-fixtures'
import { FINISHED, TODAY_FIXTURE, audit, boot, noHorizontalScroll } from './helpers'

for (const scheme of ['light', 'dark'] as const) {
  test.describe(`${scheme} theme, desktop`, () => {
    test.use({ colorScheme: scheme, viewport: { width: 1280, height: 900 } })

    test('list', async ({ page }) => {
      await boot(page)
      await expect(page.getByRole('heading', { name: 'Pick a match' })).toBeVisible()
      await audit(page, 'list')
      await page.screenshot({ path: `test-results/list-${scheme}.png`, fullPage: true })
    })

    test('panel open, each tab', async ({ page }) => {
      await boot(page, `#/?m=${TODAY_FIXTURE.id}`)
      await expect(page.locator('#panel-h')).toContainText(TODAY_FIXTURE.home)
      await audit(page, 'panel prediction')
      await page.screenshot({ path: `test-results/panel-${scheme}.png`, fullPage: true })
      await page.getByRole('tab', { name: 'Teams' }).click()
      await audit(page, 'panel teams')
      await page.screenshot({ path: `test-results/panel-teams-${scheme}.png`, fullPage: true })
      await page.getByRole('tab', { name: 'About this match' }).click()
      await audit(page, 'panel about')
    })

    test('finished match panel', async ({ page }) => {
      await boot(page, `#/?date=${FINISHED.date}&m=${FINISHED.id}`)
      await expect(page.getByRole('heading', { name: 'Final shots' })).toBeVisible()
      await audit(page, 'finished panel')
    })

    test('track record, how it works, full match page', async ({ page }) => {
      await boot(page, '#/track')
      await expect(page.getByRole('heading', { name: 'Track record', level: 1 })).toBeVisible()
      await audit(page, 'track backfill')
      await page.screenshot({ path: `test-results/track-${scheme}.png`, fullPage: true })
      await page.getByRole('tab', { name: 'Live picks' }).click()
      await expect(page.getByText('0 settled so far')).toBeVisible()
      await audit(page, 'track live')
      await boot(page, '#/how')
      await audit(page, 'how it works')
      await boot(page, `#/match/${TODAY_FIXTURE.id}`)
      await expect(page.getByRole('heading', { level: 1 })).toContainText(TODAY_FIXTURE.home)
      await expect(page.locator('#panel-h')).toContainText(TODAY_FIXTURE.away)
      await audit(page, 'full match page')
    })

    test('results page and evolution charts', async ({ page }) => {
      await boot(page, '#/results')
      await expect(page.getByRole('heading', { name: 'Results', level: 1 })).toBeVisible()
      await expect(page.getByRole('heading', { name: /^European cups/ })).toBeVisible()
      await audit(page, 'results')
      await page.screenshot({ path: `test-results/results-${scheme}.png`, fullPage: true })
      await boot(page, '#/track?rec=live', { data: { 'track.json': trackFull } })
      await expect(page.getByRole('img', { name: /Weekly hit rate/ })).toBeVisible()
      await audit(page, 'track live charts')
      await page.screenshot({ path: `test-results/track-live-${scheme}.png`, fullPage: true })
      await boot(page, '#/track?rec=experimental', { data: { 'track.json': trackFull } })
      await audit(page, 'track champions league')
    })

    test('surge watch page and panel row', async ({ page }) => {
      await boot(page, '#/surge')
      await expect(page.getByRole('heading', { name: 'Surge Watch', level: 1 })).toBeVisible()
      await expect(page.getByRole('heading', { name: 'Upcoming surge candidates' })).toBeVisible()
      await audit(page, 'surge watch')
      await page.screenshot({ path: `test-results/surge-${scheme}.png`, fullPage: true })
      await boot(page, '#/match/fx001')
      await expect(page.getByRole('region', { name: /Surge signals/ })).toBeVisible()
      await audit(page, 'surge panel row')
    })

    test('stale data warning', async ({ page }) => {
      await boot(page, '#/', { now: '2026-10-20T10:00:00Z' })
      await expect(page.locator('.statusbar.stale')).toBeVisible()
      await audit(page, 'stale banner')
    })
  })
}

test.describe('mobile 380px', () => {
  test.use({ viewport: { width: 380, height: 800 } })
  for (const scheme of ['light', 'dark'] as const) {
    test(`list and sheet, ${scheme}`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme })
      await boot(page)
      await noHorizontalScroll(page)
      await audit(page, `mobile list ${scheme}`)
      await page.screenshot({ path: `test-results/mobile-list-${scheme}.png` })
      await boot(page, `#/?m=${TODAY_FIXTURE.id}`)
      await noHorizontalScroll(page)
      await expect(page.locator('#panel-h')).toBeVisible()
      await audit(page, `mobile sheet ${scheme}`)
      await page.screenshot({ path: `test-results/mobile-sheet-${scheme}.png` })
      await boot(page, '#/track')
      await noHorizontalScroll(page)
      await audit(page, `mobile track ${scheme}`)
      await boot(page, '#/results')
      await noHorizontalScroll(page)
      await audit(page, `mobile results ${scheme}`)
      await page.screenshot({ path: `test-results/mobile-results-${scheme}.png`, fullPage: true })
      await boot(page, '#/surge')
      await noHorizontalScroll(page)
      await audit(page, `mobile surge ${scheme}`)
      await page.screenshot({ path: `test-results/mobile-surge-${scheme}.png`, fullPage: true })
      await boot(page, '#/track?rec=live', { data: { 'track.json': trackFull } })
      await noHorizontalScroll(page)
      await audit(page, `mobile charts ${scheme}`)
      await page.screenshot({ path: `test-results/mobile-charts-${scheme}.png`, fullPage: true })
    })
  }
})

test.describe('tablet 820px', () => {
  test.use({ viewport: { width: 820, height: 900 } })
  test('list + panel', async ({ page }) => {
    await boot(page, `#/?m=${TODAY_FIXTURE.id}`)
    await noHorizontalScroll(page)
    await audit(page, 'tablet')
    await page.screenshot({ path: 'test-results/tablet.png', fullPage: true })
  })
})
