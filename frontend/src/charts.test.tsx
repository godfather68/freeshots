import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import App from './App'
import { DAILY, WEEKLY, dataset, meta, trackFull } from './test-fixtures'
import type { Dataset, TrackRecord } from './lib/types'
import { cumulative, milestonesFor } from './lib/history'
import { buckets, weekStart } from './components/ResultsChart'
import { byDay } from './lib/recap'
import { applyTheme, themeStore } from './lib/storage'

async function mount(hash: string, load: () => Promise<Dataset> = () => Promise.resolve(dataset)) {
  window.location.hash = hash
  render(<App zone="Africa/Brazzaville" today="2026-10-12" now={Date.parse('2026-10-12T10:00:00Z')} load={load} />)
  await screen.findByRole('heading', { level: 1 }, { timeout: 3000 }).catch(() => undefined)
}
const withTrack = (track: Dataset['track']) => () => Promise.resolve({ ...dataset, track })

describe('growth of the record (lib/history)', () => {
  it('adds up calls and hits, with a Wilson range, using weekly points when the daily ones do not cover the record', () => {
    const rec = { label: 'x', by_n: {}, daily: DAILY, weekly: WEEKLY } as TrackRecord
    const c = cumulative(rec, 24)
    expect(c.grain).toBe('week') // daily: 15 calls, weekly: 55
    expect(c.points.map((p) => p.cumPicks)).toEqual([24, 51, 55, 55])
    expect(c.points.map((p) => p.cumHits)).toEqual([19, 40, 42, 42])
    const last = c.points[3]
    expect(last.rate).toBeCloseTo(42 / 55)
    expect(last.lo).toBeLessThan(last.rate!)
    expect(last.hi).toBeGreaterThan(last.rate!)
  })

  it('uses daily points when they hold every call, oldest first', () => {
    const rec = { label: 'x', by_n: {}, daily: DAILY, weekly: [] } as TrackRecord
    const c = cumulative(rec, 26) // nearest published threshold is 24
    expect(c.grain).toBe('day')
    expect(c.usedN).toBe(24)
    expect(c.points.map((p) => p.date)).toEqual(['2026-10-10', '2026-10-11'])
  })

  it('keeps the European-cup milestones apart from the official record, sorted by date', () => {
    const live = milestonesFor(meta.model_history, 'live')
    expect(live.map((m) => m.model)).toEqual(['negbin_pit_comp', 'fin_c'])
    expect(milestonesFor(meta.model_history, 'experimental').map((m) => m.route)).toEqual(['all', 'cross_border'])
    expect(milestonesFor(undefined, 'live')).toEqual([])
  })
})

describe('results chart buckets', () => {
  it('groups days into Monday weeks and averages the stated chance of the calls', () => {
    expect(weekStart('2026-10-11')).toBe('2026-10-05') // a Sunday
    expect(weekStart('2026-10-12')).toBe('2026-10-12') // a Monday
    const groups = byDay(dataset.results!.results.filter((r) => !r.experimental), 24, meta.thresholds.total[0])
    const days = buckets(groups, 'day')
    const weeks = buckets(groups, 'week')
    expect(days.reduce((a, b) => a + b.matches, 0)).toBe(weeks.reduce((a, b) => a + b.matches, 0))
    expect(days.map((d) => d.date)).toEqual([...days.map((d) => d.date)].sort())
    for (const b of days) if (b.calls > 0) expect(b.said!).toBeGreaterThanOrEqual(0.7)
  })
})

describe('new charts on the pages', () => {
  it('track: growth charts with milestones, legend and reading line', async () => {
    await mount('#/track?rec=live', withTrack(trackFull))
    expect(await screen.findByRole('img', { name: /Cumulative hit rate of 70%\+ calls at 24\+/ })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Settled 70%\+ calls at 24\+ shots, cumulative/ })).toBeInTheDocument()
    const ms = screen.getByRole('list', { name: 'Model milestones' })
    expect(within(ms).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      expect.stringContaining('Live record starts'), expect.stringContaining('fin_c adopted'),
    ])
    expect(screen.getAllByText('How to read this').length).toBeGreaterThan(2)
  })

  it('track: the reliability chart labels the diagonal', async () => {
    await mount('#/track')
    const rel = await screen.findByRole('img', { name: /Reliability/ })
    expect(rel).toHaveTextContent('perfectly calibrated')
    expect(screen.getByRole('img', { name: /Hit rate of 70%\+ calls by league at 24\+ shots, highest first/ })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Cumulative/ })).toBeNull() // backfill: no timeline
  })

  it('plots answer the arrow keys with a tooltip', async () => {
    await mount('#/track?rec=live', withTrack(trackFull))
    const img = await screen.findByRole('img', { name: /Weekly hit rate/ })
    const plot = img.closest('.plot') as HTMLElement
    plot.focus()
    fireEvent.keyDown(plot, { key: 'ArrowRight' })
    expect(plot.querySelector('.vtip')).toHaveTextContent(/Week of .*19 of 24 hit/)
    fireEvent.keyDown(plot, { key: 'Escape' })
    expect(plot.querySelector('.vtip')).toBeNull()
  })

  it('results: daily and weekly hit-rate bars', async () => {
    await mount('#/results')
    const chart = await screen.findAllByRole('img', { name: /Hit rate of 70%\+ calls at 24\+ shots, day by day/ })
    expect(chart.length).toBeGreaterThan(0)
    await userEvent.click(screen.getAllByRole('button', { name: 'Weekly' })[0])
    expect(screen.getAllByRole('img', { name: /week by week/ }).length).toBeGreaterThan(0)
  })

  it('theme switch stamps html[data-theme] and remembers it', async () => {
    await mount('#/how')
    const btn = screen.getByRole('button', { name: 'Dark theme' })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(btn)
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('shots.theme')).toBe('"dark"')
    expect(btn).toHaveAttribute('aria-pressed', 'true')
    themeStore.set('system')
    applyTheme('system')
    expect(document.documentElement.dataset.theme).toBeUndefined()
  })
})
