import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import App from './App'
import { board, dataset, meta, surge } from './test-fixtures'
import { loadDataset } from './lib/data'
import { catalogOf, labelsOf, surgeCandidates, usualRate } from './lib/surge'
import { buildItems } from './lib/items'
import type { Dataset } from './lib/types'

const fx = (id: string) => board.fixtures.find((f) => f.id === id)!
const START = meta.thresholds.total[0]

async function mount(hash: string, ds: Dataset = dataset) {
  window.location.hash = hash
  render(<App zone="Africa/Brazzaville" today="2026-10-12" now={Date.parse('2026-10-12T10:00:00Z')} load={() => Promise.resolve(ds)} />)
  await screen.findByRole('heading', { level: 1 }, { timeout: 3000 })
}
const rows = () => [...document.querySelectorAll('li.srow')] as HTMLElement[]
const rowTeams = () => rows().map((r) => within(r).getByRole('link').getAttribute('aria-label'))

describe('Surge Watch page', () => {
  it('is in the main navigation and has its own title', async () => {
    await mount('#/surge')
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Surge' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { level: 1, name: 'Surge Watch' })).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('Surge Watch · Shots board'))
  })

  it('defines a surge in one line and says probabilities are unchanged', async () => {
    await mount('#/surge')
    const usual = Math.round(usualRate(surge) * 100)
    expect(screen.getByText(/is a match that ends 5\+ shots above our expected total/)).toHaveTextContent(`about ${usual}% of matches; these signals raise the odds`)
    const note = screen.getByRole('note', { name: 'Caveat' })
    expect(note).toHaveTextContent('A lean, not a promise.')
    expect(note).toHaveTextContent('49,643 past matches')
    expect(note).toHaveTextContent('probabilities on this site are unchanged')
  })

  it('shows boost cards before dampers, with realised vs usual and vs our curve', async () => {
    await mount('#/surge')
    const titles = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent ?? '')
    const iBoost = titles.findIndex((t) => t.includes('Nothing to play for'))
    const iDamp = titles.findIndex((t) => t.includes('December or January'))
    expect(iBoost).toBeGreaterThanOrEqual(0)
    expect(iDamp).toBeGreaterThan(titles.findIndex((t) => t.includes('Strong home side')))
    const card = screen.getByRole('article', { name: /Nothing to play for/ })
    expect(card).toHaveTextContent('27% vs 21% usual')
    expect(within(card).getByRole('img', { name: 'Happened in 27.4% of these matches; our curve said 21.5%' })).toBeInTheDocument()
    expect(card).toHaveTextContent('Held in 7 of 7 seasons · 1,341 matches')
    // the route where it does not hold is shown, marked "not used"
    expect(card).toHaveTextContent(/Not used\s*Without odds: 22.7% against our curve’s 22.0%/)
  })

  it('lists upcoming fixtures with a boost, most signals first, linked to the match panel', async () => {
    await mount('#/surge')
    const want = surgeCandidates(buildItems(dataset), catalogOf(surge), 24, START)
    expect(want.map((c) => c.item.id)).toEqual(['fx001', 'fx005'])
    expect(rowTeams()).toEqual(want.map((c) => `Open ${c.item.home} v ${c.item.away}`))
    const first = rows()[0]
    expect(within(first).getByRole('link')).toHaveAttribute('href', expect.stringContaining('m=fx001'))
    expect(within(first).getByRole('list', { name: /Surge signals for/ })).toHaveTextContent('▲ Head-to-head runs high')
    expect(first).toHaveTextContent(`We expect${fx('fx001').expected} shots`)
    // an id this client does not know is ignored; the damper is shown as a damper
    const second = rows()[1]
    expect(within(second).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      '▲ Head-to-head runs high (raises the chance of a surge)', '▼ December or January (lowers the chance of a surge)'])
    expect(second.querySelector('.scount b')).toHaveTextContent('1')
  })

  it('filters by minimum signals and league, and clears', async () => {
    await mount('#/surge')
    await userEvent.click(screen.getByRole('button', { name: /^2\+ signals/ }))
    expect(rowTeams()).toHaveLength(1)
    expect(window.location.hash).toContain('ms=2')
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(rows()).toHaveLength(2)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'League' }), fx('fx005').league)
    await waitFor(() => expect(rows()).toHaveLength(fx('fx001').league === fx('fx005').league ? 2 : 1))
    expect(screen.getByRole('status')).toHaveTextContent(/shown of 2/)
    await userEvent.click(screen.getByRole('button', { name: /^3\+ signals/ }))
    expect(await screen.findByText('No candidate fits these filters')).toBeInTheDocument()
  })

  it('says so when no upcoming fixture has a signal', async () => {
    const quiet: Dataset = { ...dataset, board: { ...board, fixtures: board.fixtures.map((f) => ({ ...f, surge: undefined })) } }
    await mount('#/surge', quiet)
    expect(screen.getByText('No upcoming match has a surge signal right now')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: /Nothing to play for/ })).toBeInTheDocument()
  })

  it('tolerates a missing surge.json', async () => {
    await mount('#/surge', { ...dataset, surge: null })
    expect(screen.getByRole('heading', { name: 'Surge Watch is not published yet' })).toBeInTheDocument()
    expect(rows()).toHaveLength(0)
  })
})

describe('Surge signals in the match panel', () => {
  it('shows the chips with a glossary entry', async () => {
    await mount(`#/match/fx001`)
    const sec = screen.getByRole('region', { name: /Surge signals/ })
    expect(within(sec).getByRole('list')).toHaveTextContent('▲ Head-to-head runs high')
    expect(within(sec).getByText(/What does “Surge signals” mean\?/)).toBeInTheDocument()
    expect(within(sec).getByRole('link', { name: 'What the signals mean' })).toHaveAttribute('href', '#/surge')
  })

  it('says none for an assessed fixture without signals, and hides the row when not assessed', async () => {
    await mount(`#/match/fx002`)
    expect(screen.getByRole('region', { name: /Surge signals/ })).toHaveTextContent('None for this match')
  })

  it('has no row when the fixture was not assessed or surge.json is missing', async () => {
    await mount(`#/match/fx003`)
    expect(screen.queryByRole('region', { name: /Surge signals/ })).toBeNull()
  })

  it('has no row without the catalogue', async () => {
    await mount(`#/match/fx001`, { ...dataset, surge: null })
    expect(screen.queryByRole('region', { name: /Surge signals/ })).toBeNull()
  })

  it('lists the term in the glossary', async () => {
    await mount('#/how')
    expect(screen.getByText('Surge signals', { selector: 'dt' })).toBeInTheDocument()
  })
})

describe('surge helpers and loading', () => {
  it('resolves labels against the catalogue and drops unknown ids', () => {
    const cat = catalogOf(surge)
    expect(labelsOf({ signals: ['h2h_above', 'nope', 'winter'], against: ['winter', 'h2h_above'] }, cat)).toEqual({
      boosts: [surge.signals[1]], dampers: [surge.signals[3]] })
    expect(labelsOf(undefined, cat)).toBeNull()
    expect(labelsOf({ signals: ['h2h_above'], against: [] }, null)).toBeNull()
    expect(catalogOf(null)).toBeNull()
  })

  const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })
  const fetcher = (files: Record<string, unknown>) =>
    (async (u: string) => {
      const name = u.split('/').pop()!
      return name in files ? ok(files[name]) : new Response('no', { status: 404 })
    }) as unknown as typeof fetch
  const base = { 'meta.json': meta, 'board.json': board }

  it('loads surge.json when published, null when missing or not a catalogue', async () => {
    expect((await loadDataset(fetcher({ ...base, 'surge.json': surge }))).surge?.signals).toHaveLength(4)
    expect((await loadDataset(fetcher(base))).surge).toBeNull()
    expect((await loadDataset(fetcher({ ...base, 'surge.json': { schema: 1 } }))).surge).toBeNull()
  })
})
