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
}
export interface TeamCurve { mean: number; median: number; lb80: number; curve: number[] }
export type DataQuality = 'full' | 'partial' | 'none'
export interface Bounds { '70': number; '80': number; '90': number }
export interface Fixture {
  id: string
  league: string
  round: string | null
  date: string
  kickoff_local: string
  kickoff_utc: string
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
export interface TrackRecord {
  label: string
  by_n: Record<string, TrackNEntry>
  settled?: number
  pending?: number
}
export interface Track { schema: number; records: { backfill: TrackRecord; live: TrackRecord } }

/** One row in the list: an upcoming fixture or a finished match, normalised. */
export interface Item {
  id: string
  league: string
  date: string
  time: string | null // league-local kick-off; null for finished matches (not published)
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

export interface Dataset { meta: Meta; board: Board; results: Results | null; track: Track | null }
