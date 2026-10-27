// Types for the static data contract (docs/static-api.md, schema 1).
export interface League { id: string; name: string; country: string; tier: number; fixtures: number; featured?: boolean }
export interface Meta {
  schema: number
  built_at: string
  results_up_to: string | null
  last_job: { status: string; finished_at: string | null } | null
  models: string[]
  thresholds: { total: [number, number]; team: [number, number]; default_total: number; default_team: number }
  leagues: League[]
  days: string[]
  counts: { fixtures: number; with_odds: number; cold_start: number }
  /** Additive (optional): finished matches with a prediction still waiting for their result. */
  awaiting_results?: { official: number; experimental: number; oldest: string | null }
}
export interface TeamCurve { mean: number; median: number; lb80: number; curve: number[] }
export type DataQuality = 'full' | 'partial' | 'none'
export interface Bounds { '70': number; '80': number; '90': number }
export interface Fixture {
  id: string
  league: string
  round: string | null
  date: string
  kickoff_local: string | null
  kickoff_utc: string | null
  home: string
  away: string
  expected: number
  lb: Bounds
  league_avg: number | null
  above_league: number | null
  odds: boolean
  cold_start: boolean
  model: string
  curve: number[]
  team: { home: TeamCurve; away: TeamCurve } | null
  /** Additive (optional): built from domestic form, not validated on this competition. */
  experimental?: boolean
  /** Additive (optional): how much domestic data both clubs had. */
  data_quality?: DataQuality
  /** Additive (optional): the 'why this number' breakdown. */
  explain?: Explain | null
  /** Additive (optional): Surge Watch labels (ids of surge.json signals). Absent = not assessed. */
  surge?: SurgeLabels | null
}
export interface SurgeLabels { signals: string[]; against: string[] }
export type SurgeRoute = 'odds' | 'noodds'
/** The research's own numbers for one signal on one route (surge.json). */
export interface SurgeRouteStats {
  n: number
  over_rate: number
  stated_rate: number
  base_rate: number
  excess_ci: [number, number]
  seasons_held: number
  seasons: number
  applies: boolean
}
export interface SurgeSignal {
  id: string
  label: string
  direction: 'boost' | 'damper'
  explanation: string
  routes: Record<SurgeRoute, SurgeRouteStats>
}
/** Additive file (optional): the Surge Watch catalogue. */
export interface Surge {
  schema: number
  report_date: string
  margin: number
  matches: number
  period: string
  method: string
  routes: Record<SurgeRoute, { label: string; base_rate: number }>
  signals: SurgeSignal[]
}
export type Volatility = 'low' | 'average' | 'high'
export interface ExplainFactor { id: string; side?: 'home' | 'away' | 'match' | null; label: string; detail?: string | null; effect: number }
export interface ExplainSignals {
  home_matches?: number | null
  away_matches?: number | null
  odds_used?: boolean | null
  home_volatility?: Volatility | null
  away_volatility?: Volatility | null
  model_gap?: number | null
}
/** Optional, additive: why the expected total is what it is (baseline plus signed factors). */
export interface Explain {
  expected_mean?: number | null
  baseline?: { value: number; label: string } | null
  factors?: ExplainFactor[] | null
  residual?: number | null
  signals?: ExplainSignals | null
}
export interface Board { schema: number; fixtures: Fixture[] }
export interface Result {
  id: string
  league: string
  date: string
  home: string
  away: string
  actual: { total: number; home: number; away: number }
  predicted_at: string
  expected: number
  lb: Bounds
  odds: boolean
  curve: number[]
  /** Additive (optional): a competition without a results feed (Champions League). Never counted in the official record. */
  experimental?: boolean
  /** Additive (optional): where the result came from: 'football-data', 'manual', or a provider name (e.g. 'espn') for a provisional result. */
  result_source?: string
  /** Additive (optional): 'provisional' = entered by hand until the official data arrives (it still counts); 'confirmed' otherwise. */
  result_status?: 'provisional' | 'confirmed'
  /** Additive (optional, manual and provisional results): link to the page the result was copied from. */
  source_url?: string | null
}
export interface Results { schema: number; results: Result[] }
export interface ReliabilityBand { band: [number, number]; n: number; stated: number; realised: number }
export interface TrackNEntry {
  selected_min_prob: number
  n: number
  hits: number
  hit_rate: number
  mean_stated: number
  wilson95: [number, number]
  brier: number
  brier_baseline: number
  reliability: ReliabilityBand[]
  by_league: { league: string; n: number; hit_rate: number; stated: number }[]
}
/** Additive (optional): one day or week of settled matches. `date` is the day, or the week start. */
export interface Period {
  date: string
  matches: number
  mae: number | null
  bias: number | null
  floor80_held: number | null
  by_n: Record<string, { picks: number; hits: number }>
  /** Additive (optional): how many of `matches` rest on a provisional result. */
  provisional?: number
}
export interface TrackRecord {
  label: string
  by_n: Record<string, TrackNEntry>
  settled?: number
  pending?: number
  /** Additive (optional): settled picks that rest on a provisional result. */
  provisional?: number
  daily?: Period[]
  weekly?: Period[]
}
export interface Track { schema: number; records: { backfill: TrackRecord; live: TrackRecord; experimental?: TrackRecord } }

/** One row in the list: an upcoming fixture or a finished match, normalised. */
export interface Item {
  id: string
  league: string
  /** Day to group by: the visitor's date when a UTC instant exists (and 'My time' is on), else the venue date. */
  date: string
  time: string | null // HH:mm in the active view; null for finished matches (not published)
  /** `time` is the venue's local time (venue mode, or no UTC instant to convert from). */
  venueTime: boolean
  /** Kick-off instant in ms (UTC); null when unknown (always for finished matches). */
  utc: number | null
  venueDate: string
  venueClock: string | null
  home: string
  away: string
  expected: number
  curve: number[]
  lb: Bounds
  odds: boolean
  coldStart: boolean
  experimental: boolean
  dataQuality: DataQuality
  aboveLeague: number | null
  teamExpected: [number, number] | null
  finished: boolean
  actual: { total: number; home: number; away: number } | null
  fixture: Fixture | null
  result: Result | null
}

export interface Dataset { meta: Meta; board: Board; results: Results | null; track: Track | null; surge?: Surge | null }
