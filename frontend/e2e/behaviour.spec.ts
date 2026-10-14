import { expect, test } from '@playwright/test'
import { TODAY_FIXTURE, boot } from './helpers'

test.use({ viewport: { width: 1280, height: 900 } })

test('opening a match moves focus to the panel, Escape returns it to the row', async ({ page }) => {
  await boot(page)
  const row = page.locator(`a.mlink[data-id="${TODAY_FIXTURE.id}"]`)
  await row.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('#panel-h')).toBeFocused()
  await expect(page).toHaveURL(new RegExp(`m=${TODAY_FIXTURE.id}`))
  await page.keyboard.press('Escape')
  await expect(row).toBeFocused()
  await expect(page).not.toHaveURL(/m=/)
})

test('arrow keys move between rows and the URL carries the view', async ({ page }) => {
  await boot(page)
  const links = page.locator('a.mlink')
  await links.first().focus()
  await page.keyboard.press('ArrowDown')
  await expect(links.nth(1)).toBeFocused()
  await page.getByLabel('Increase Shots').click()
  await page.getByRole('button', { name: /^70%\+/ }).click()
  await expect(page).toHaveURL(/n=25/)
  await expect(page).toHaveURL(/chip=p70/)
  await page.reload()
  await expect(page.getByRole('button', { name: /^70%\+/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('#ctl-n')).toHaveValue('25')
})

test('collapsed league and favourites are remembered', async ({ page }) => {
  await boot(page)
  await page.locator('.star').first().click()
  const head = page.locator('.ghead').first()
  await head.click()
  await expect(head).toHaveAttribute('aria-expanded', 'false')
  await page.reload()
  await expect(page.locator('.ghead').first()).toHaveAttribute('aria-expanded', 'false')
  await page.getByRole('tab', { name: 'Favourites' }).click()
  await expect(page.locator('a.mlink')).toHaveCount(1)
})

test('missing data shows a friendly error', async ({ page }) => {
  await boot(page, '#/', { missing: ['board.json'] })
  await expect(page.getByRole('alert')).toContainText('not published yet')
})

test('no panel layout shift on desktop', async ({ page }) => {
  await boot(page)
  // Compare in document coordinates: clicking a row may scroll it into view, which is not a layout shift.
  const box = () => page.locator('.g-list').evaluate((e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y + window.scrollY, width: r.width, height: r.height } })
  const before = await box()
  await page.locator('a.mlink').first().click()
  await expect(page.locator('#panel-h')).toBeVisible()
  expect(await box()).toEqual(before)
})
