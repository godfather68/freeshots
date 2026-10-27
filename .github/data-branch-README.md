# Shots: published predictions (audit trail)

This branch is written by the scheduled workflow `.github/workflows/update.yml` and by nothing else.
It holds the five JSON files the static site reads (`data/meta.json`, `data/board.json`,
`data/results.json`, `data/track.json`, `data/surge.json`; format: `docs/static-api.md` on the default branch).

A commit is added only when the published data changed. Commits are never rewritten or force-pushed,
so the history of `data/board.json`, with GitHub's server-side push times, shows what was predicted
for a match before it kicked off. To see the prediction for one fixture over time:

    git log -p -S '"id":"<fixture id>"' -- data/board.json

Only our own model outputs, fixture identity (teams, league, kick-off) and final shot counts of
finished matches are published here. No odds and no source data.
