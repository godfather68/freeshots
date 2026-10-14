import { expect, test } from '@playwright/test'
import { audit, boot, noHorizontalScroll } from './helpers'

// Fixture cl001 kicks off at 2026-10-13T16:45:00Z. Times and days must follow the browser's zone.
const row = (page: import('@playwright/test').Page) => page.locator('a.mlink[data-id="cl001"]')

test.describe('visitor in Brazzaville (UTC+1)', () => {
  test.use({ timezoneId: 'Africa/Brazzaville' })
  test('shows 17:45 and names the zone', async ({ page }) => {
    await boot(page, '#/?date=2026-10-13')
    await expect(row(page).locator('.mtime')).toHaveText('17:45')
    await expect(page.locator('.zonebar p')).toHaveText('Times in Africa/Brazzaville (UTC+1)')
    await expect(page.locator('.fresh')).toContainText('UTC+1')
    await noHorizontalScroll(page)
    await audit(page, 'board in Brazzaville')
  })
})

test.describe('visitor in Baku (UTC+4)', () => {
  test.use({ timezoneId: 'Asia/Baku' })
  test('shows 20:45, the toggle flips to venue time and persists', async ({ page }) => {
    await boot(page, '#/?date=2026-10-13')
    await expect(row(page).locator('.mtime')).toHaveText('20:45')
    await expect(page.locator('.zonebar p')).toContainText('Asia/Baku (UTC+4)')
    await page.getByRole('button', { name: 'Venue time' }).click()
    await expect(row(page).locator('.mtime')).toHaveText('18:45') // the fixture's published venue clock
    await page.reload()
    await page.waitForSelector('main#main')
    await expect(page.getByRole('button', { name: 'Venue time' })).toHaveAttribute('aria-pressed', 'true')
    await expect(row(page).locator('.mtime')).toHaveText('18:45')
    await audit(page, 'board in venue-time mode')
  })

  test('detail panel shows both clocks', async ({ page }) => {
    await boot(page, '#/match/cl001?date=2026-10-13')
    await expect(page.locator('.ko').first()).toContainText('20:45 your time')
    await expect(page.locator('.ko').first()).toContainText('18:45 at the venue')
    await audit(page, 'match page in Baku')
  })
})

test.describe('visitor in New York', () => {
  test.use({ timezoneId: 'America/New_York' })
  test('shows 12:45 on the same day', async ({ page }) => {
    await boot(page, '#/?date=2026-10-13')
    await expect(row(page).locator('.mtime')).toHaveText('12:45')
    await expect(page.locator('.zonebar p')).toContainText('(UTC-4)')
  })
})
