import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import App from './App'
import { board, dataset, meta, results } from './test-fixtures'

const START = meta.thresholds.total[0]
const todays = board.fixtures.filter((f) => f.date === '2026-10-12')
const first = todays[0]

async function mount(hash = '#/', load = () => Promise.resolve(dataset)) {
  window.location.hash = hash
  render(<App today="2026-10-12" now={Date.parse('2026-10-12T10:00:00Z')} load={load} />)
  await screen.findByRole('heading', { name: /Upcoming football matches/ }, { timeout: 3000 }).catch(() => undefined)
}
const rowFor = (home: string) => screen.getAllByRole('link').find((a) => a.getAttribute('aria-label')?.startsWith(`${home} v `))!
const hash = () => window.location.hash

describe('match list', () => {
  it('renders today with league groups, counts and rows', async () => {
    await mount()
    expect(screen.getByRole('heading', { name: /Premier League/ })).toBeInTheDocument()
    const rows = document.querySelectorAll('a.mlink')
    expect(rows).toHaveLength(todays.length)
    expect(document.querySelector('p[role=status]')).toHaveTextContent(`${todays.length} matches in`)
  })

  it('shows the model probability at N, exactly as published', async () => {
    await mount()
    const expected = Math.round(first.curve[24 - START] * 100)
    const label = rowFor(first.home).getAttribute('aria-label')!
    expect(label).toContain(`${expected}% chance of 24+ shots`)
    expect(label).toContain(`kick-off ${first.kickoff_local}`)
  })

  it('moving the threshold re-reads the curve and writes N to the URL', async () => {
    await mount()
    await userEvent.click(screen.getByRole('button', { name: 'Increase Shots' }))
    expect(hash()).toContain('n=25')
    const expected = Math.round(first.curve[25 - START] * 100)
    await waitFor(() => expect(rowFor(first.home).getAttribute('aria-label')).toContain(`${expected}% chance of 25+ shots`))
  })

  it('70%+ chip keeps only matches that clear the bar and announces the count', async () => {
    await mount()
    await userEvent.click(screen.getByRole('button', { name: /^70%\+/ }))
    const want = todays.filter((f) => f.curve[24 - START] >= 0.7).length
    expect(document.querySelectorAll('a.mlink')).toHaveLength(want)
    expect(hash()).toContain('chip=p70')
  })

  it('empty state offers to clear filters', async () => {
    await mount('#/?p=95&n=45')
    expect(await screen.findByText('Nothing clears these filters')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(document.querySelectorAll('a.mlink').length).toBeGreaterThan(0))
    expect(hash()).not.toContain('p=95')
  })

  it('a day with no matches says so and points to the next day', async () => {
    await mount('#/?date=2026-10-14')
    expect(await screen.findByText(/No matches on Wed 14 Oct/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Go to Sat 17 Oct/ })).toBeInTheDocument()
  })

  it('bad query params never crash: values are clamped', async () => {
    await mount('#/?n=9999&p=-3&sort=zzz&chip=%00&tab=nope&m=ghost')
    expect(screen.getByLabelText(/Shots/, { selector: 'input' })).toHaveValue('45')
    expect(screen.getByRole('heading', { name: 'Pick a match' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/not in the current board|may have finished/i)
  })

  it('stars persist in localStorage and fill the Favourites tab', async () => {
    await mount()
    await userEvent.click(within(rowFor(first.home).closest('li')!).getByRole('button', { name: /Follow/ }))
    expect(JSON.parse(localStorage.getItem('shots.fav')!).matches).toEqual([first.id])
    await userEvent.click(screen.getByRole('tab', { name: 'Favourites' }))
    expect(document.querySelectorAll('a.mlink')).toHaveLength(1)
  })

  it('shows finished matches with actual shots and a hit/miss mark for the current N', async () => {
    const r = results.results[0]
    await mount(`#/?date=${r.date}&chip=done`)
    const row = document.querySelector(`a.mlink[data-id="${r.id}"]`)!
    const hit = r.actual.total >= 24
    expect(row.textContent).toContain(`${r.actual.total} shots`)
    expect(row.textContent).toContain(hit ? 'hit' : 'miss')
    expect(row.getAttribute('aria-label')).toContain(hit ? ', hit' : ', miss')
  })
})

describe('detail panel', () => {
  it('opens beside the list from the URL with the headline, floors and a text alternative', async () => {
    await mount(`#/?m=${first.id}`)
    const panel = screen.getByRole('complementary', { name: 'Match detail' })
    expect(within(panel).getByRole('heading', { level: 2, name: new RegExp(first.home) })).toBeInTheDocument()
    const p = Math.round(first.curve[24 - START] * 100)
    expect(within(panel).getByTestId('headline')).toHaveTextContent(`${p}%chance of 24+ shots in total`)
    expect(within(panel).getByText(`at least ${first.lb['80']}`)).toBeInTheDocument()
    expect(within(panel).getByRole('img', { name: /Chance of at least N total shots/ })).toBeInTheDocument()
    await userEvent.click(within(panel).getByText('Show the chart as a table'))
    expect(within(panel).getByRole('table')).toBeInTheDocument()
  })

  it('clicking a row selects it and the list stays', async () => {
    await mount()
    await userEvent.click(rowFor(first.home))
    await waitFor(() => expect(hash()).toContain(`m=${first.id}`))
    expect(await screen.findByRole('heading', { level: 2, name: new RegExp(first.away) })).toBeInTheDocument()
    expect(document.querySelectorAll('a.mlink').length).toBe(todays.length)
    expect(rowFor(first.home)).toHaveAttribute('aria-current', 'true')
  })

  it('Teams tab labels team numbers experimental, About tab states odds and model', async () => {
    const withTeam = board.fixtures.find((f) => f.team && !f.odds)!
    await mount(`#/?date=${withTeam.date}&m=${withTeam.id}&pt=teams`)
    expect(screen.getAllByText('experimental').length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('tab', { name: 'About this match' }))
    expect(screen.getByText(/No odds yet/)).toBeInTheDocument()
    expect(screen.getByText(withTeam.model)).toBeInTheDocument()
  })

  it('flags a cold start', async () => {
    const cold = board.fixtures.find((f) => f.cold_start)!
    await mount(`#/?date=${cold.date}&m=${cold.id}`)
    expect(screen.getAllByText('cold start').length).toBeGreaterThan(0)
  })

  it('a match without team calibration says so', async () => {
    const none = board.fixtures.find((f) => f.team === null)!
    await mount(`#/?date=${none.date}&m=${none.id}&pt=teams`)
    expect(screen.getByText(/without team calibration/)).toBeInTheDocument()
  })

  it('finished match shows the outcome against the chosen N', async () => {
    const r = results.results[0]
    await mount(`#/?date=${r.date}&m=${r.id}&n=20`)
    expect(screen.getByRole('heading', { name: 'Final shots' })).toBeInTheDocument()
    expect(screen.getByText(r.actual.total >= 20 ? /Hit/ : /Miss/)).toBeInTheDocument()
  })

  it('unknown match id on the full page is a friendly not-found, not a crash', async () => {
    await mount('#/match/does-not-exist')
    expect(await screen.findByRole('heading', { name: 'Match not found' })).toBeInTheDocument()
  })
})

describe('pages and states', () => {
  it('track record page: backfill numbers, then an honest empty live state', async () => {
    await mount('#/track')
    expect(await screen.findByRole('heading', { level: 1, name: 'Track record' })).toBeInTheDocument()
    expect(screen.getByText('Hit rate at 70%+ calls')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Reliability/ })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Live picks' }))
    expect(screen.getByText('0 settled so far')).toBeInTheDocument()
  })

  it('how it works page lists the glossary', async () => {
    await mount('#/how')
    expect(await screen.findByRole('heading', { level: 1, name: 'How it works' })).toBeInTheDocument()
    expect(screen.getByText('Cold start', { selector: 'dt' })).toBeInTheDocument()
  })

  it('missing data files give a friendly error with a retry', async () => {
    let calls = 0
    const load = () => (++calls === 1 ? Promise.reject(new Error('data/board.json is not published yet (HTTP 404).')) : Promise.resolve(dataset))
    window.location.hash = '#/'
    render(<App today="2026-10-12" load={load} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('not published yet')
    await act(async () => { await userEvent.click(screen.getByRole('button', { name: 'Try again' })) })
    expect(await screen.findByRole('heading', { name: /Upcoming football matches/ })).toBeInTheDocument()
  })

  it('warns when the data is stale or the last job failed', async () => {
    window.location.hash = '#/'
    const failed = { ...dataset, meta: { ...meta, last_job: { status: 'failed', finished_at: null } } }
    render(<App today="2026-10-12" now={Date.parse(meta.built_at) + 3600_000} load={() => Promise.resolve(failed)} />)
    expect(await screen.findByText(/last update did not finish/i)).toBeInTheDocument()
  })

  it('guide can be dismissed and the choice is remembered', async () => {
    await mount()
    await userEvent.click(screen.getByRole('button', { name: 'Hide guide' }))
    expect(screen.queryByRole('heading', { name: /New here/ })).toBeNull()
    expect(JSON.parse(localStorage.getItem('shots.guide')!).dismissed).toBe(true)
  })
})
