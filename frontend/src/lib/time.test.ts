import { describe, expect, it } from 'vitest'
import { buildItems, availableDays, sortItems, topPicks } from './items'
import { dateIn, isValidZone, kickoffView, offsetLabel, stampIn, wallClock } from './time'
import { dataset, meta } from '../test-fixtures'
import type { Dataset } from './types'

const BRA = 'Africa/Brazzaville'
const LON = 'Europe/London'
const BAK = 'Asia/Baku'
const NYC = 'America/New_York'
const START = meta.thresholds.total[0]

describe('wallClock', () => {
  it('converts one instant into each zone (the same Champions League kick-off)', () => {
    const t = Date.parse('2026-10-13T16:45:00Z')
    expect(wallClock(t, BRA)).toMatchObject({ date: '2026-10-13', time: '17:45' })
    expect(wallClock(t, LON)).toMatchObject({ date: '2026-10-13', time: '17:45' })
    expect(wallClock(t, BAK)).toMatchObject({ date: '2026-10-13', time: '20:45' })
    expect(wallClock(t, NYC)).toMatchObject({ date: '2026-10-13', time: '12:45' })
  })
  it('moves a late match to the next day or the previous day', () => {
    const late = Date.parse('2026-10-13T22:30:00Z')
    expect(wallClock(late, BAK)).toMatchObject({ date: '2026-10-14', time: '02:30' })
    expect(wallClock(late, NYC)).toMatchObject({ date: '2026-10-13', time: '18:30' })
    const early = Date.parse('2026-10-13T01:00:00Z')
    expect(wallClock(early, NYC)).toMatchObject({ date: '2026-10-12', time: '21:00' })
  })
  it('uses a 24-hour clock and never shows 24:xx at midnight', () => {
    const w = wallClock(Date.parse('2026-10-13T23:05:00Z'), LON)
    expect(w.time).toBe('00:05')
    expect(w.date).toBe('2026-10-14')
  })
  it('follows the Europe DST change on 25 Oct 2026 (clocks go back at 01:00 UTC)', () => {
    const before = Date.parse('2026-10-25T00:30:00Z')
    const after = Date.parse('2026-10-25T01:30:00Z')
    expect(wallClock(before, LON).time).toBe('01:30')
    expect(wallClock(after, LON).time).toBe('01:30')
    expect(offsetLabel(before, LON)).toBe('UTC+1')
    expect(offsetLabel(after, LON)).toBe('UTC')
    expect(wallClock(Date.parse('2026-10-24T19:00:00Z'), 'Europe/Paris').time).toBe('21:00')
    expect(wallClock(Date.parse('2026-10-25T19:00:00Z'), 'Europe/Paris').time).toBe('20:00')
    expect(wallClock(Date.parse('2026-10-25T19:00:00Z'), NYC).time).toBe('15:00') // US is on summer time until 1 Nov
  })
})

describe('offsetLabel / stampIn / zones', () => {
  it('formats offsets', () => {
    const t = Date.parse('2026-10-13T12:00:00Z')
    expect(offsetLabel(t, BRA)).toBe('UTC+1')
    expect(offsetLabel(t, NYC)).toBe('UTC-4')
    expect(offsetLabel(t, 'Asia/Kolkata')).toBe('UTC+5:30')
    expect(offsetLabel(t, 'UTC')).toBe('UTC')
  })
  it('stamps in the visitor zone', () => {
    expect(stampIn('2026-10-13T16:45:00Z', BAK)).toBe('13 Oct 20:45 UTC+4')
    expect(stampIn(null, BAK)).toBe('unknown')
    expect(stampIn('nope', BAK)).toBe('unknown')
  })
  it('validates zones and gives the visitor date', () => {
    expect(isValidZone(BAK)).toBe(true)
    expect(isValidZone('Mars/Olympus')).toBe(false)
    expect(dateIn(Date.parse('2026-10-13T22:30:00Z'), BAK)).toBe('2026-10-14')
  })
})

describe('kickoffView', () => {
  const src = { date: '2026-10-13', kickoff_local: '23:30', kickoff_utc: '2026-10-13T22:30:00Z' }
  it('my time derives date and clock from the UTC instant', () => {
    expect(kickoffView(src, BAK, 'mine')).toMatchObject({ date: '2026-10-14', time: '02:30', venueTime: false, venueDate: '2026-10-13', venueClock: '23:30' })
  })
  it('venue time keeps the published date and clock', () => {
    expect(kickoffView(src, BAK, 'venue')).toMatchObject({ date: '2026-10-13', time: '23:30', venueTime: true })
  })
  it('falls back to the venue date and time, marked as venue time, without a UTC instant', () => {
    const k = kickoffView({ date: '2026-10-13', kickoff_local: '21:00', kickoff_utc: null }, BAK, 'mine')
    expect(k).toMatchObject({ date: '2026-10-13', time: '21:00', venueTime: true, utc: null })
    expect(kickoffView({ date: '2026-10-13', kickoff_local: null, kickoff_utc: null }, BAK, 'mine')).toMatchObject({ time: null, venueTime: false })
    expect(kickoffView({ ...src, kickoff_utc: 'garbage' }, BAK, 'mine')).toMatchObject({ date: '2026-10-13', time: '23:30', venueTime: true })
  })
})

describe('items in a visitor zone', () => {
  const f0 = dataset.board.fixtures[0]
  const ds: Dataset = {
    ...dataset,
    results: null,
    board: {
      ...dataset.board,
      fixtures: [
        { ...f0, id: 'late', date: '2026-10-13', kickoff_local: '23:30', kickoff_utc: '2026-10-13T22:30:00Z' },
        { ...f0, id: 'early', date: '2026-10-14', kickoff_local: '14:00', kickoff_utc: '2026-10-14T12:00:00Z' },
        { ...f0, id: 'nokick', date: '2026-10-14', kickoff_local: '09:00', kickoff_utc: null },
      ],
    },
  }
  it('regroups by the visitor date and flags venue-time fallbacks', () => {
    const by = Object.fromEntries(buildItems(ds, { zone: BAK, mode: 'mine' }).map((i) => [i.id, i]))
    expect(by.late).toMatchObject({ date: '2026-10-14', time: '02:30', venueTime: false })
    expect(by.early).toMatchObject({ date: '2026-10-14', time: '16:00' })
    expect(by.nokick).toMatchObject({ date: '2026-10-14', time: '09:00', venueTime: true })
    const venue = Object.fromEntries(buildItems(ds, { zone: BAK, mode: 'venue' }).map((i) => [i.id, i]))
    expect(venue.late).toMatchObject({ date: '2026-10-13', time: '23:30' })
  })
  it('sorts by the UTC instant, not the clock string', () => {
    const mixed = buildItems(ds, { zone: BAK, mode: 'mine' }).filter((i) => i.date === '2026-10-14')
    // "late" shows 02:30 but is the earliest instant; "early" shows 16:00.
    expect(sortItems(mixed, 'time', 24, START).map((i) => i.id)).toEqual(['late', 'nokick', 'early'])
    const items = buildItems(ds, { zone: NYC, mode: 'mine' })
    expect(items.find((i) => i.id === 'late')).toMatchObject({ date: '2026-10-13', time: '18:30' })
  })
  it('availableDays ignores venue dates from meta unless times are venue times', () => {
    const items = buildItems(ds, { zone: BAK, mode: 'mine' })
    expect(availableDays({ days: ['2026-10-13'] }, items, false)).toEqual(['2026-10-14'])
    expect(availableDays({ days: ['2026-10-13'] }, items, true)).toEqual(['2026-10-13', '2026-10-14'])
  })
  it('top picks are by visitor day', () => {
    const items = buildItems(ds, { zone: BAK, mode: 'mine' })
    expect(topPicks(items, '2026-10-13', 24, START)).toHaveLength(0)
    expect(topPicks(items, '2026-10-14', 24, START).length).toBeGreaterThan(0)
  })
})
