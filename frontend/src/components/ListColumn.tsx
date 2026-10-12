import { useEffect, useMemo, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import { useApp } from '../lib/app'
import { countLabel, filterItems, groupByLeague, sortItems, shiftDay } from '../lib/items'
import type { Filters } from '../lib/items'
import { dayLong, dayNum, dayShort } from '../lib/format'
import { collapsedStore, favouritesStore, toggleIn } from '../lib/storage'
import { CHIPS, SORTS, TABS } from '../lib/url'
import type { Chip, ListTab, Sort } from '../lib/url'
import { MatchRow } from './MatchRow'
import { Flag, Help, Stepper, Tabs, useStore } from './ui'

const CHIP_LABEL: Record<Chip, string> = { all: 'All', p70: '70%+', odds: 'With odds', done: 'Finished' }
const SORT_LABEL: Record<Sort, string> = { time: 'Kick-off time', prob: 'Highest chance', above: 'Most above league average' }
const TAB_LABEL: Record<ListTab, string> = { all: 'All', fav: 'Favourites', leagues: 'Leagues' }

export function ListColumn() {
  const { ds, items, days, date, today, state, set, href, totalStart } = useApp()
  const favs = useStore(favouritesStore)
  const collapsed = useStore(collapsedStore)
  const listRef = useRef<HTMLDivElement>(null)
  const stripRef = useRef<HTMLDivElement>(null)
  const { n, tab, chip, p, lg, sort } = state
  const [tlo, thi] = ds.meta.thresholds.total

  const favTeams = useMemo(() => new Set(favs.teams), [favs.teams])
  const favMatches = useMemo(() => new Set(favs.matches), [favs.matches])
  const base: Filters = useMemo(
    () => ({
      date: tab === 'fav' ? null : date, chip, minProb: p, league: lg, n, totalStart,
      favTeams, favMatches, onlyFavs: tab === 'fav',
    }),
    [tab, date, chip, p, lg, n, totalStart, favTeams, favMatches],
  )
  const shown = useMemo(() => sortItems(filterItems(items, base), sort, n, totalStart), [items, base, sort, n, totalStart])
  const groups = useMemo(() => groupByLeague(shown, ds.meta.leagues), [shown, ds.meta.leagues])
  const counts = useMemo(
    () => Object.fromEntries(CHIPS.map((c) => [c, filterItems(items, { ...base, chip: c }).length])) as Record<Chip, number>,
    [items, base],
  )
  const dayHasAny = useMemo(() => items.some((i) => i.date === date), [items, date])
  const hasResults = (ds.results?.results.length ?? 0) > 0
  const activeLeagues = useMemo(() => {
    const used = new Set(items.map((i) => i.league))
    return ds.meta.leagues.filter((l) => l.fixtures > 0 || used.has(l.id))
  }, [ds.meta.leagues, items])

  // Keep the selected day centred in the strip without scrolling the page.
  useEffect(() => {
    const strip = stripRef.current
    const cur = strip?.querySelector<HTMLElement>('[aria-current="date"]')
    if (strip && cur) strip.scrollLeft = cur.offsetLeft - strip.clientWidth / 2 + cur.offsetWidth / 2
  }, [date, days])

  const prev = shiftDay(days, date, -1)
  const next = shiftDay(days, date, 1)
  const onStar = (id: string) => favouritesStore.set({ ...favs, matches: toggleIn(favs.matches, id) })
  const toggleGroup = (id: string) => collapsedStore.set(toggleIn(collapsed, id))

  const onListKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const t = e.target as HTMLElement
    if (!t.classList?.contains('mlink')) return
    const links = [...(listRef.current?.querySelectorAll<HTMLAnchorElement>('.groupbody:not([hidden]) a.mlink') ?? [])]
    const i = links.indexOf(t as HTMLAnchorElement)
    const to = links[i + (e.key === 'ArrowDown' ? 1 : -1)]
    if (to) {
      e.preventDefault()
      to.focus()
    }
  }

  const anyFilter = chip !== 'all' || p > 0 || lg !== ''
  const clear = () => set({ chip: 'all', p: 0, lg: '' }, { replace: true })
  const nextWithMatches = days.find((d) => d > date && items.some((i) => i.date === d && !i.finished))

  return (
    <section className="listcol" aria-labelledby="list-h">
      <h2 id="list-h" className="sr">Matches</h2>
      <Tabs
        label="Match list"
        tabs={TABS.map((t) => ({ id: t, label: TAB_LABEL[t] }))}
        value={tab}
        onChange={(t) => set({ tab: t }, { replace: true })}
        className="listtabs"
      >
        {tab !== 'fav' && (
          <div className="datenav">
            <div className="daybar">
              <button type="button" className="sq" aria-label="Previous day" disabled={!prev} onClick={() => prev && set({ date: prev }, { replace: true })}>&lsaquo;</button>
              <div className="daylabel">
                <strong>{dayLong(date)}</strong>
                {date === today && <span className="todaytag">Today</span>}
              </div>
              <button type="button" className="sq" aria-label="Next day" disabled={!next} onClick={() => next && set({ date: next }, { replace: true })}>&rsaquo;</button>
              <button type="button" className="quiet" disabled={state.date === null} onClick={() => set({ date: null }, { replace: true })}>Today</button>
            </div>
            <nav aria-label="Pick a day" className="strip" ref={stripRef}>
              {days.map((d) => (
                <a key={d} href={href({ date: d })} aria-current={d === date ? 'date' : undefined} className={d === today ? 'istoday' : ''}
                  aria-label={dayLong(d) + (d === today ? ', today' : '')}
                  onClick={(e) => { e.preventDefault(); set({ date: d }, { replace: true }) }}>
                  <span>{dayShort(d)}</span><b>{dayNum(d)}</b>
                </a>
              ))}
            </nav>
          </div>
        )}

        {tab === 'leagues' ? (
          <ul className="leaguelist">
            {activeLeagues.map((l) => (
              <li key={l.id}>
                <a href={href({ lg: l.id, tab: 'all' })}>
                  <Flag country={l.country} />
                  <span className="lname">{l.name}<small>{l.country}</small></span>
                  <span className="count">{l.fixtures}<span className="sr"> upcoming matches</span></span>
                </a>
              </li>
            ))}
            {activeLeagues.length === 0 && <li className="hint">No leagues published yet.</li>}
          </ul>
        ) : (
          <>
            <div className="chips" role="group" aria-label="Show">
              {CHIPS.filter((c) => c !== 'done' || hasResults).map((c) => (
                <button key={c} type="button" className="chip" aria-pressed={chip === c} onClick={() => set({ chip: c }, { replace: true })}>
                  {CHIP_LABEL[c]} <span className="count">{counts[c]}</span>
                </button>
              ))}
            </div>

            <div className="controls" role="group" aria-label="Threshold and sorting">
              <Stepper id="ctl-n" label="Shots" value={n} min={tlo} max={thi} onChange={(v) => set({ n: v }, { replace: true })} help={<Help term="threshold" />} />
              <div className="stepper">
                <label htmlFor="ctl-p" className="lbl">Minimum chance <Help term="probability" /></label>
                <div className="steprow">
                  <input id="ctl-p" type="range" min={0} max={95} step={5} value={p} onChange={(e) => set({ p: Number(e.target.value) }, { replace: true })} aria-valuetext={p ? `${p}% or more` : 'any'} />
                  <output htmlFor="ctl-p">{p ? `${p}%+` : 'Any'}</output>
                </div>
              </div>
              <label className="sel">Sort
                <select value={sort} onChange={(e) => set({ sort: e.target.value as Sort }, { replace: true })}>
                  {SORTS.map((s) => <option key={s} value={s}>{SORT_LABEL[s]}</option>)}
                </select>
              </label>
              <label className="sel">League
                <select value={lg} onChange={(e) => set({ lg: e.target.value }, { replace: true })}>
                  <option value="">All leagues</option>
                  {activeLeagues.map((l) => <option key={l.id} value={l.id}>{l.name} ({l.country})</option>)}
                </select>
              </label>
            </div>

            <p className="sr" role="status" aria-live="polite" aria-atomic="true">{countLabel(shown.length, groups.length)}</p>

            {/* Arrow keys are a shortcut on top of plain links; the links stay tabbable. */}
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
            <div ref={listRef} onKeyDown={onListKey} className="groups">
              {groups.map((g) => {
                const open = !collapsed.includes(g.league.id)
                const gid = `grp-${g.league.id}`
                return (
                  <section key={g.league.id} className="group" aria-labelledby={`${gid}-h`}>
                    <h3 id={`${gid}-h`}>
                      <button type="button" className="ghead" aria-expanded={open} aria-controls={`${gid}-b`} onClick={() => toggleGroup(g.league.id)}>
                        <svg className="chev" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M3 4.5l3 3 3-3" /></svg>
                        <Flag country={g.league.country} />
                        <span className="lname">{g.league.name}<small>{g.league.country}</small></span>
                        <span className="count">{g.items.length}<span className="sr"> matches</span></span>
                      </button>
                    </h3>
                    <ul id={`${gid}-b`} className="groupbody" hidden={!open}>
                      {g.items.map((it) => (
                        <MatchRow
                          key={it.id}
                          item={it}
                          n={n}
                          totalStart={totalStart}
                          href={href({ m: it.id })}
                          selected={state.m === it.id}
                          starred={favMatches.has(it.id)}
                          homeFav={favTeams.has(it.home)}
                          awayFav={favTeams.has(it.away)}
                          showDate={tab === 'fav'}
                          onStar={onStar}
                        />
                      ))}
                    </ul>
                  </section>
                )
              })}
            </div>

            {shown.length === 0 && (
              <div className="empty">
                {tab === 'fav' ? (
                  <>
                    <h3>No favourites yet</h3>
                    <p>Tap the star on a match, or on a team in the match panel, and it will be kept here (on this device only).</p>
                  </>
                ) : anyFilter && dayHasAny ? (
                  <>
                    <h3>Nothing clears these filters</h3>
                    <p>{chip === 'done' ? 'No finished matches' : 'No matches'} on {dayLong(date)} fit your chance, odds or league choice at {n}+ shots.</p>
                    <button type="button" onClick={clear}>Clear filters</button>
                  </>
                ) : (
                  <>
                    <h3>No matches on {dayLong(date)}</h3>
                    <p>{nextWithMatches ? 'Try the next day with fixtures.' : 'There are no upcoming fixtures in the latest board.'}</p>
                    {nextWithMatches && <p><a href={href({ date: nextWithMatches })}>Go to {dayLong(nextWithMatches)}</a></p>}
                  </>
                )}
              </div>
            )}
          </>
        )}
      </Tabs>
    </section>
  )
}
