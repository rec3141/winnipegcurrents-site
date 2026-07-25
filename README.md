# River City Currents

A daily-refreshed events calendar for Winnipeg, Manitoba. Events are sorted into six
Manitoba-river-themed "vibe streams" plus an **On the Horizon** section for things worth
booking early.

**Live:** https://winnipegcurrents.ca (`.com` redirects here)

## How it works (short version)

- `index.html` is the whole site — a self-contained static page (HTML/CSS/vanilla JS)
  that fetches `data/latest.json` and renders it. No build step, no framework.
- `data/latest.json` is the current events snapshot; `data/YYYY/MM/DD.json` are dated
  archives; `data/index.json` is a manifest.
- A GitHub Action (`.github/workflows/daily-refresh.yml`) runs `scripts/refresh.py` every
  morning: it fans out web-search agents, merges the finds into `data/`, and commits.
  A DreamHost cron pulls this repo to the web host hourly. The page fetches its data
  same-origin, so visitors never hit GitHub.

## Working on it

```bash
git clone https://github.com/rec3141/winnipegcurrents-site.git
cd winnipegcurrents-site
python3 -m http.server 8080     # then open http://localhost:8080/index.html
```

Edit `index.html`, commit, and `git push origin main` — the live site mirrors `main`
within the hour. Pull/rebase before pushing (several people/agents push here).

To exercise the generator without spending tokens or writing anything:

```bash
python3 scripts/refresh.py --offline --dry-run   # re-merge + prune existing data
```

**Full architecture, conventions, and the data schema are in [`CLAUDE.md`](./CLAUDE.md).**
The generation spec is in [`docs/weekly-refresh-task.md`](./docs/weekly-refresh-task.md).
