// Realistic, deterministic fixtures that follow docs/static-api.md. Used by unit/component tests and by the
// Playwright audits (served in place of data/*.json). Never shipped: not imported by the app.
import type { Board, Dataset, Explain, Fixture, Meta, Period, Result, Results, Track, TrackNEntry, TrackRecord } from '../lib/types'

const TOTAL: [number, number] = [10, 45]
const TEAM: [number, number] = [3, 25]

function mulberry(seed: number) {
  return () => {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function erf(x: number): number {
  const t = 1 / (1 + 0.3275911 * Math.abs(x))
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x)
  return x >= 0 ? y : -y
}
const cdf = (z: number) => 0.5 * (1 + erf(z / Math.SQRT2))

export function makeCurve(mean: number, sd: number, [lo, hi]: [number, number]): number[] {
  const out: number[] = []
  for (let k = lo; k <= hi; k++) out.push(Math.round(Math.min(1, Math.max(0, 1 - cdf((k - 0.5 - mean) / sd))) * 1e4) / 1e4)
  return out
}
function bound(curve: number[], start: number, q: number): number {
  let best = start
  curve.forEach((p, i) => { if (p >= q) best = start + i })
  return best
}

export const LEAGUES: Array<{ id: string; name: string; country: string; tier: number; avg: number; teams: string[]; featured?: boolean }> = [
  { id: 'england-premier-league', name: 'Premier League', country: 'England', tier: 1, avg: 27.8, teams: ['Man United', 'Tottenham', 'Arsenal', 'Chelsea', 'Liverpool', 'Newcastle', 'Aston Villa', 'Brighton'] },
  { id: 'spain-la-liga', name: 'La Liga', country: 'Spain', tier: 1, avg: 24.6, teams: ['Girona', 'Mallorca', 'Sevilla', 'Valencia', 'Real Betis', 'Villarreal', 'Getafe', 'Osasuna'] },
  { id: 'germany-bundesliga', name: 'Bundesliga', country: 'Germany', tier: 1, avg: 26.9, teams: ['Union Berlin', 'Freiburg', 'Mainz', 'Werder Bremen', 'Augsburg', 'Wolfsburg', 'Heidenheim', 'Köln'] },
  { id: 'italy-serie-a', name: 'Serie A', country: 'Italy', tier: 1, avg: 25.1, teams: ['Torino', 'Genoa', 'Lecce', 'Cagliari', 'Udinese', 'Parma', 'Verona', 'Como'] },
  { id: 'netherlands-eredivisie', name: 'Eredivisie', country: 'Netherlands', tier: 1, avg: 27.2, teams: ['Twente', 'Utrecht', 'Heerenveen', 'Sparta Rotterdam', 'Go Ahead Eagles', 'Zwolle', 'Groningen', 'NEC'] },
  { id: 'england-championship', name: 'Championship', country: 'England', tier: 2, avg: 25.9, teams: ['Leeds', 'Norwich', 'Bristol City', 'Hull', 'Swansea', 'Millwall', 'Preston', 'Stoke'] },
  { id: 'uefa-champions-league', name: 'Champions League', country: 'Europe', tier: 0, avg: 26.4, featured: true, teams: ['Bayern', 'Inter', 'Benfica', 'Celtic', 'PSV', 'Galatasaray', 'Red Star', 'Young Boys'] },
]

const DAYS = ['2026-10-12', '2026-10-13', '2026-10-17', '2026-10-18']
const BUILT_AT = '2026-10-12T09:00:00Z'

/** Champions League fixtures: experimental, with every data-quality level. Built apart so the league fixtures stay stable. */
function buildEuropean(): Fixture[] {
  const rnd = mulberry(23)
  const lg = LEAGUES.find((l) => l.featured)!
  const quality: Array<'full' | 'partial' | 'none'> = ['full', 'partial', 'none', 'full']
  const out: Fixture[] = []
  for (let g = 0; g < 4; g++) {
    const mean = Math.round((lg.avg + (rnd() - 0.4) * 6) * 10) / 10
    const curve = makeCurve(mean, 6.1, TOTAL)
    const hm = Math.round(mean * 0.54 * 10) / 10
    const am = Math.round((mean - hm) * 10) / 10
    const hc = makeCurve(hm, 4, TEAM)
    const ac = makeCurve(am, 3.8, TEAM)
    const exp = Math.round(mean)
    out.push({
      id: `cl${String(g + 1).padStart(3, '0')}`,
      league: lg.id,
      round: 'League Phase - 3',
      date: '2026-10-13',
      kickoff_local: g < 2 ? '18:45' : '21:00',
      kickoff_utc: g < 2 ? '2026-10-13T16:45:00Z' : '2026-10-13T19:00:00Z',
      home: lg.teams[g * 2],
      away: lg.teams[g * 2 + 1],
      expected: exp,
      lb: { '70': bound(curve, TOTAL[0], 0.7), '80': bound(curve, TOTAL[0], 0.8), '90': bound(curve, TOTAL[0], 0.9) },
      league_avg: null,
      above_league: null,
      odds: false,
      cold_start: false,
      model: 'negbin_pit_comp-noodds_2026-10-05',
      curve,
      team: {
        home: { mean: hm, median: Math.round(hm), lb80: bound(hc, TEAM[0], 0.8), curve: hc },
        away: { mean: am, median: Math.round(am), lb80: bound(ac, TEAM[0], 0.8), curve: ac },
      },
      experimental: true,
      data_quality: quality[g],
    })
  }
  return out
}

function buildFixtures(): Fixture[] {
  const rnd = mulberry(7)
  const out: Fixture[] = []
  let idn = 0
  DAYS.forEach((date, di) => {
    LEAGUES.forEach((lg, li) => {
      if (lg.featured) return // European fixtures are built apart
      // not every league plays every day
      if ((li + di) % 3 === 2 && di > 1) return
      if (di < 2 && li > 2 + di) return
      const games = di < 2 ? 1 + ((li + di) % 2) : 2
      for (let g = 0; g < games; g++) {
        const h = lg.teams[(g * 2 + di) % lg.teams.length]
        const a = lg.teams[(g * 2 + di + 1) % lg.teams.length]
        const mean = Math.round((lg.avg + (rnd() - 0.45) * 7) * 10) / 10
        const curve = makeCurve(mean, 5.4, TOTAL)
        const hm = Math.round((mean * (0.52 + rnd() * 0.06)) * 10) / 10
        const am = Math.round((mean - hm) * 10) / 10
        const withOdds = di === 0 && li < 2
        const idx = (curve.findIndex((p) => p < 0.5) === -1 ? curve.length : curve.findIndex((p) => p < 0.5)) + TOTAL[0] - 1
        idn += 1
        const hc = makeCurve(hm, 3.8, TEAM)
        const ac = makeCurve(am, 3.6, TEAM)
        out.push({
          id: `fx${String(idn).padStart(3, '0')}`,
          league: lg.id,
          round: `Regular Season - ${6 + di}`,
          date,
          kickoff_local: ['12:30', '15:00', '17:30', '20:00'][(g + li + di) % 4],
          kickoff_utc: `${date}T${['11:30', '14:00', '16:30', '19:00'][(g + li + di) % 4]}:00Z`,
          home: h,
          away: a,
          expected: idx,
          lb: { '70': bound(curve, TOTAL[0], 0.7), '80': bound(curve, TOTAL[0], 0.8), '90': bound(curve, TOTAL[0], 0.9) },
          league_avg: lg.avg,
          above_league: Math.round((idx - lg.avg) * 10) / 10,
          odds: withOdds,
          cold_start: idn === 4,
          model: withOdds ? 'negbin_pit_comp_odds_2026-10-05' : 'negbin_pit_comp-noodds_2026-10-05',
          curve,
          ...(idn === 3 ? { explain: EXPLAIN_FULL } : idn === 4 ? { explain: EXPLAIN_PARTIAL } : {}),
          team: idn % 7 === 0 ? null : {
            home: { mean: hm, median: Math.round(hm), lb80: bound(hc, TEAM[0], 0.8), curve: hc },
            away: { mean: am, median: Math.round(am), lb80: bound(ac, TEAM[0], 0.8), curve: ac },
          },
        })
      }
    })
  })
  out.push(...buildEuropean())
  return out.sort((x, y) => (x.kickoff_utc ?? '').localeCompare(y.kickoff_utc ?? '') || x.league.localeCompare(y.league) || x.home.localeCompare(y.home))
}

/** Full explain block, with a negative factor and every signal. */
export const EXPLAIN_FULL: Explain = {
  expected_mean: 32.6,
  baseline: { value: 27.8, label: 'Premier League average this season' },
  factors: [
    { id: 'home_attack', side: 'home', label: 'Home attack', detail: '18.2 shots per match, last 10 (league 13.9)', effect: 2.9 },
    { id: 'away_defence', side: 'away', label: 'Away defence', detail: 'Concede 9.1 shots per match (league 12.4)', effect: -1.2 },
    { id: 'away_attack', side: 'away', label: 'Away attack', detail: '12.0 shots per match, last 10 (league 11.2)', effect: 1.0 },
  ],
  residual: 2.1,
  signals: { home_matches: 38, away_matches: 4, odds_used: false, home_volatility: 'average', away_volatility: 'high', model_gap: 1.4 },
}
/** Partial block: no baseline, no signals. */
export const EXPLAIN_PARTIAL: Explain = {
  factors: [{ id: 'match_tempo', side: 'match', label: 'Match tempo', effect: -0.8 }],
}

function buildResults(): Result[] {
  const rnd = mulberry(11)
  const out: Result[] = []
  const dates = ['2026-10-10', '2026-10-11', '2026-10-11', '2026-10-04', '2026-10-04']
  let idn = 0
  for (const date of dates) {
    for (const lg of LEAGUES.slice(0, 4)) {
      if (rnd() > 0.7) continue
      const mean = lg.avg + (rnd() - 0.5) * 6
      const curve = makeCurve(mean, 5.4, TOTAL)
      const total = Math.max(8, Math.round(mean + (rnd() - 0.5) * 14))
      const home = Math.round(total * (0.45 + rnd() * 0.15))
      idn += 1
      out.push({
        id: `rs${String(idn).padStart(3, '0')}`,
        league: lg.id,
        date,
        home: lg.teams[idn % 8],
        away: lg.teams[(idn + 3) % 8],
        actual: { total, home, away: total - home },
        predicted_at: `${date.slice(0, 8)}${String(Number(date.slice(8)) - 2).padStart(2, '0')}T09:43:00Z`,
        expected: Math.round(mean),
        lb: { '70': bound(curve, TOTAL[0], 0.7), '80': bound(curve, TOTAL[0], 0.8), '90': bound(curve, TOTAL[0], 0.9) },
        odds: idn % 3 === 0,
        curve,
      })
    }
  }
  // One league result entered by hand while football-data had not published it yet (provisional).
  const prov = out.find((r) => r.date === '2026-10-11')!
  Object.assign(prov, { result_source: 'espn', result_status: 'provisional', source_url: 'https://www.espn.com/soccer/team-stats/_/gameId/1' })
  out.push(...buildEuropeanResults())
  return out
}

/** Champions League results: experimental, entered by hand, with a source link. */
function buildEuropeanResults(): Result[] {
  const lg = LEAGUES.find((l) => l.featured)!
  const rows: Array<[string, number, number, number, number]> = [
    ['2026-10-08', 0, 1, 15, 12], ['2026-10-08', 2, 3, 11, 9], ['2026-10-07', 4, 5, 18, 10], ['2026-10-07', 6, 7, 9, 8],
  ]
  return rows.map(([date, h, a, hs, as], i) => {
    const mean = lg.avg + (i - 1.5)
    const curve = makeCurve(mean, 6.1, TOTAL)
    return {
      id: `clr00${i + 1}`, league: lg.id, date, home: lg.teams[h], away: lg.teams[a],
      actual: { total: hs + as, home: hs, away: as },
      predicted_at: `${date.slice(0, 8)}01T09:00:00Z`,
      expected: Math.round(mean),
      lb: { '70': bound(curve, TOTAL[0], 0.7), '80': bound(curve, TOTAL[0], 0.8), '90': bound(curve, TOTAL[0], 0.9) },
      odds: false, curve, experimental: true, result_source: 'manual' as const,
      source_url: 'https://example.org/ucl/match-' + (i + 1),
    }
  })
}

function trackEntry(n: number): TrackNEntry {
  const hit = Math.max(0.35, Math.min(0.97, 0.78 - (n - 24) * 0.07))
  const total = 1085
  const bands: [number, number][] = [[0.5, 0.6], [0.6, 0.7], [0.7, 0.8], [0.8, 0.9], [0.9, 1]]
  return {
    selected_min_prob: 0.7,
    n: total,
    hits: Math.round(total * hit),
    hit_rate: Math.round(hit * 1e4) / 1e4,
    mean_stated: Math.round((hit - 0.025) * 1e4) / 1e4,
    wilson95: [Math.round((hit - 0.024) * 1e3) / 1e3, Math.round((hit + 0.024) * 1e3) / 1e3],
    brier: 0.2205,
    brier_baseline: 0.2419,
    reliability: bands.map(([lo, hi], i) => ({ band: [lo, hi], n: 120 + i * 90, stated: Math.round(((lo + hi) / 2) * 1e3) / 1e3, realised: Math.round(((lo + hi) / 2 + 0.02) * 1e3) / 1e3 })),
    by_league: LEAGUES.slice(0, 5).map((l, i) => ({ league: l.id, n: 200 + i * 15, hit_rate: 0.74 + i * 0.01, stated: 0.75 })),
  }
}

export const meta: Meta = {
  schema: 1,
  built_at: BUILT_AT,
  results_up_to: '2026-10-11',
  last_job: { status: 'ok', finished_at: '2026-10-12T08:59:40Z' },
  models: ['negbin_pit_comp-noodds_2026-10-05', 'negbin_pit_comp_odds_2026-10-05'],
  thresholds: { total: TOTAL, team: TEAM, default_total: 24, default_team: 12 },
  leagues: [],
  days: DAYS,
  counts: { fixtures: 0, with_odds: 0, cold_start: 0 },
}

const fixtures = buildFixtures()
meta.leagues = LEAGUES.map((l) => ({ id: l.id, name: l.name, country: l.country, tier: l.tier, fixtures: fixtures.filter((f) => f.league === l.id).length, ...(l.featured ? { featured: true } : {}) }))
meta.counts = { fixtures: fixtures.length, with_odds: fixtures.filter((f) => f.odds).length, cold_start: fixtures.filter((f) => f.cold_start).length }

export const board: Board = { schema: 1, fixtures }
export const results: Results = { schema: 1, results: buildResults() }
export const track: Track = {
  schema: 1,
  records: {
    backfill: {
      label: '2025/26 season (out of sample)',
      by_n: Object.fromEntries([18, 20, 22, 24, 26, 28, 30].map((n) => [String(n), trackEntry(n)])),
    },
    live: { label: 'Live picks (saved before kick-off)', settled: 0, pending: 127, by_n: {} },
  },
}
const period = (date: string, matches: number, mae: number, bias: number, floor: number, picks: number, hits: number): Period => ({
  date, matches, mae, bias, floor80_held: floor, by_n: { '24': { picks, hits } },
})
/** Weekly points of every kind: a big week, a small one, and a week without any 70%+ call. */
export const WEEKLY: Period[] = [
  period('2026-09-21', 38, 4.6, 0.4, 0.84, 24, 19),
  period('2026-09-28', 41, 4.2, -0.3, 0.8, 27, 21),
  period('2026-10-05', 6, 5.1, 1.2, 0.83, 4, 2),
  period('2026-10-12', 3, 3.8, 0.2, 1, 0, 0),
]
export const DAILY: Period[] = [period('2026-10-11', 14, 4.1, 1.2, 0.86, 9, 7), period('2026-10-10', 9, 4.4, -0.2, 0.78, 6, 5)]
const liveFull: TrackRecord = {
  label: 'Live picks (saved before kick-off)', settled: 88, pending: 40, daily: DAILY, weekly: WEEKLY,
  by_n: { '24': { ...trackEntry(24), n: 55, hits: 42, hit_rate: 0.7636, mean_stated: 0.74, wilson95: [0.636, 0.859], by_league: [] } },
}
const experimentalRec: TrackRecord = {
  label: 'European cups (experimental, manual results)', settled: 4, pending: 4, by_n: {},
  daily: [period('2026-10-08', 2, 5.5, 1.5, 1, 1, 1)], weekly: [period('2026-10-05', 4, 5.9, 0.8, 0.75, 2, 1)],
}
/** The default track: live not yet settled (empty state), Champions League with one small week. */
export const trackFull: Track = { schema: 1, records: { ...track.records, live: liveFull, experimental: experimentalRec } }
track.records.experimental = { ...experimentalRec }
/** The pre-extension contract: no experimental record, no daily or weekly. */
export const trackLegacy: Track = { schema: 1, records: { backfill: track.records.backfill, live: track.records.live } }
export const dataset: Dataset = { meta, board, results, track }

/** Map a request path to a fixture body, for Playwright route interception. */
export function fixtureFor(file: string, overrides: Record<string, unknown> = {}): unknown {
  if (file in overrides) return overrides[file]
  return { 'meta.json': meta, 'board.json': board, 'results.json': results, 'track.json': track }[file] ?? null
}
