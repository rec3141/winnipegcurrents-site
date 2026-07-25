# River City Currents — project guide (read me first)

A Winnipeg events calendar. Static site, no build step, no framework. Events are
organized into six Manitoba-river-themed "vibe streams" plus an "On the Horizon"
book-ahead section.

- **Live site:** https://winnipegcurrents.ca  (`.com` 301-redirects to `.ca`)
- **Repo (source of truth):** https://github.com/rec3141/winnipegcurrents-site
- **Host:** DreamHost (shared). Served from the `~/winnipegcurrents.ca/` web dir.

## The one-paragraph mental model

Two things are split on purpose: **presentation** (`index.html`, committed, rarely
changes) and **data** (`data/*.json`, machine-generated weekly). A weekly agent
gathers events and pushes JSON to this repo; a DreamHost cron pulls the repo to the
web server. The page fetches its data **same-origin** from the site, so visitor page
loads never hit GitHub. Edit `index.html` for anything visual/behavioral; the data
takes care of itself.

## Files

```
index.html            The ENTIRE site: HTML + CSS + vanilla JS aggregator.
                      Fetches data/latest.json and renders it. Edit this for design.
data/latest.json      The current events snapshot the page renders.
data/index.json       Manifest: { latest, updated, runs[] }.
data/YYYY/MM/DD.json  Dated archive — one snapshot per weekly refresh.
```

There is no package.json, bundler, or server. It's a single static file plus JSON.

## Data schema (each snapshot)

```json
{
  "generated": "2026-07-24",
  "window": { "start": "2026-07-24", "end": "2026-08-07", "label": "July 24 – August 7, 2026" },
  "headwaters": [ { "theme":"f1|f2", "tag":"", "title":"", "meta":"", "blurb":"", "url":"", "cta":"" } ],
  "events": [
    { "title":"", "date":"Jul 26 | Aug 8-10 | Through Sep 2", "dateISO":"2026-07-26",
      "time":"", "venue":"", "url":"", "blurb":"", "tags":["free","tix","fam"],
      "sellout":false, "stream":"confluence|rapids|roar|deep|sky|ripples|horizon" }
  ]
}
```

Rendering rules the aggregator applies (see `renderEvents()` in `index.html`):
- An event goes to **On the Horizon** if `stream === "horizon"` OR `dateISO > window.end`;
  otherwise it shows in its vibe stream. Horizon is month-grouped; `sellout:true`
  adds a "Book ahead" pill.
- **Date display:** a bare single day like `"Jul 26"` is re-rendered as `"Sat, Jul 26"`
  (weekday derived from `dateISO`). Ranges / ongoing strings (`"Aug 8-10"`,
  `"Through Sep 2"`, `"Daily this summer"`) are shown verbatim. See `displayDate()`.

## The six streams (keys / names / colors)

| key | name | vibe | color |
|-----|------|------|-------|
| confluence | The Confluence | The Forks, markets, street food, community | #e0a82e |
| rapids | The Rapids | live music, nightlife, music festivals | #e05a3b |
| roar | The Roar | pro sports, big crowds | #c23b3b |
| deep | Deep Waters | theatre, galleries, film, comedy, ceremony | #7a5ba6 |
| sky | Open Sky | outdoors, nature, recreation, day-trips | #3e92cc |
| ripples | Little Ripples | family & kids | #219f8e |
| horizon | On the Horizon | anything beyond ~2 weeks worth booking early | #c98a1e (amber) |

The stream definitions live in the `STREAMS` array near the top of the `<script>`.

## Key UI pieces in index.html

- **Floating nav bar** (`setupNav`) — slides in on scroll, scroll-spy highlights the
  current stream. Rebuilt on every render; the scroll-spy queries the DOM live.
- **Date filter** (`setupPicker`) — the 📅 chip in the floating bar and the "Now
  showing…" hero pill both open a date/range picker (From/To + Today / This weekend /
  Next 7 days). Filtering re-renders via `renderEvents(filteredEvents)`; "Clear filter"
  restores `SNAP.events`. Global state: `SNAP` (the snapshot), `FILTER` (or null).

## How data gets updated (the pipeline — NEITHER half is in this repo)

1. **Weekly generator = a Cowork scheduled task** (Anthropic/Claude side, not a file
   here). Mondays ~07:00 America/Winnipeg. It fans out ~12 web-search subagents across
   niches, assembles events, **merges** them into the existing `data/latest.json`
   (dedupe + drop past-dated events), and `git push`es the updated `data/` over HTTPS.
   It must NOT touch `index.html`. The full spec is in `docs/weekly-refresh-task.md`.
2. **Deploy = a DreamHost cron job** (in the DreamHost panel, not a file here). Hourly.
   Clones/pulls this repo and rsyncs it into the web root, so the live site mirrors
   `main`. Command roughly:
   ```
   D="$HOME/src/winnipegcurrents-site"; [ -d "$D/.git" ] || git clone -q https://github.com/rec3141/winnipegcurrents-site.git "$D"; git -C "$D" fetch -q origin && git -C "$D" reset --hard -q origin/main && rsync -a --delete --exclude='.git' --exclude='.well-known' "$D/" "$HOME/winnipegcurrents.ca/"
   ```

A Claude Code instance can own everything in **this repo** (the aggregator, the data
schema, tests, deploys via `git push`). It canNOT directly manage the Cowork scheduled
task or the DreamHost cron — those live outside the repo. If you want the whole
pipeline repo-owned, port the weekly generation into a script or GitHub Action here
(the spec in `docs/weekly-refresh-task.md` is enough to reimplement it).

## Deploying a change

1. Edit `index.html` (or data), commit, `git push origin main`.
2. The DreamHost cron mirrors `main` to the live site within the hour. No manual upload.
3. Test locally first — it's a static file that fetches `./data/latest.json`:
   ```
   cd <repo> && python3 -m http.server 8080
   # open http://localhost:8080/index.html — check the console is clean and cards render
   ```

## Gotchas / conventions

- **Push over HTTPS, not SSH.** The environments that generate/deploy this block
  outbound SSH (port 22). Use an HTTPS remote + a token. (The weekly task holds a
  fine-grained, single-repo, contents-only GitHub PAT in its own config — **no secret
  is committed to this repo, keep it that way.**)
- **Multiple actors push to `index.html`** (a human editing on GitHub, the assistant,
  you). Always `git pull --rebase` before pushing; expect the occasional rebase.
- **Don't hand-edit the DreamHost web copy** — the cron overwrites it every hour. Repo
  is the source of truth.
- **Data files are generated.** They're overwritten by the weekly merge, so prefer
  changing the generator or the aggregator over hand-editing `data/*.json`.
- **Merge/dedupe** (in the weekly task): dedupe key = normalized title (lowercased, cut
  at the first " - "/" — ", letters+digits only) + `dateISO`; a second same-date
  word-subset pass collapses title variants ("Folklorama 2026" vs "Folklorama 2026
  (55th Festival)"). Past-dated events are dropped so the set never grows stale.
