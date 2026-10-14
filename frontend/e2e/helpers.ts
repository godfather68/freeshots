import { expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import path from 'node:path'
import { board, fixtureFor, results } from '../src/test-fixtures'

export const TODAY_FIXTURE = board.fixtures.find((f) => f.date === '2026-10-12')!
export const FINISHED = results.results.find((r) => r.date === '2026-10-11') ?? results.results[0]

/** Serve data/*.json from the test fixtures and freeze the clock at the fixtures' "today". */
export async function boot(page: Page, hash = '#/', opts: { missing?: string[]; now?: string; data?: Record<string, unknown> } = {}) {
  await page.clock.setFixedTime(new Date(opts.now ?? '2026-10-12T10:00:00Z'))
  await page.route('**/data/*.json', (route) => {
    const file = new URL(route.request().url()).pathname.split('/').pop()!
    if (opts.missing?.includes(file)) return route.fulfill({ status: 404, body: 'not found' })
    return route.fulfill({ json: fixtureFor(file, opts.data) })
  })
  await page.goto(`/${hash}`)
  await page.reload()
  await page.waitForSelector('main#main')
}

const AXE = path.resolve(import.meta.dirname, '../../tests/a11y/vendor/axe.min.js')
type Violation = { id: string; impact: string; nodes: { target: string[]; failureSummary?: string }[] }

export async function audit(page: Page, label: string) {
  await page.addScriptTag({ path: AXE })
  const res = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (c: Document, o: object) => Promise<{ violations: Violation[] }> } }).axe
    return axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })
  })
  const bad = res.violations.map(
    (v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(' ') + ' :: ' + (n.failureSummary ?? '').split('\n')[1]).join(' | ')}`,
  )
  expect(bad, `axe violations on ${label}`).toEqual([])
}

export async function noHorizontalScroll(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(over).toBeLessThanOrEqual(0)
}
