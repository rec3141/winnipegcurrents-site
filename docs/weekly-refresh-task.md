# Weekly refresh — generator spec

This is the spec the weekly generator follows. It currently runs as a **Cowork
scheduled task** (Mondays ~07:00 America/Winnipeg) that lives outside this repo. It is
reproduced here so it can be audited or reimplemented (e.g. as a script or GitHub
Action) for a fully repo-owned pipeline.

The generator's job: gather Winnipeg events via a wide web search, **merge** them into
the existing `data/latest.json`, and push the updated `data/` files. It must **not**
touch `index.html`.

## Step 1 — comprehensive fan-out search

Run ~12 parallel searchers, one per niche, each returning structured events:

1. **Indie/DIY live music** — Handsome Daughter, Good Will Social Club, Times Change(d),
   Park Theatre, West End Cultural Centre, the Cavern, Bulldog, the Pyramid, Sidestage.
2. **Visual art, galleries & theatre** — WAG/Qaumajuq, Royal MTC, Prairie Theatre
   Exchange, Dave Barber Cinematheque, aceartinc, Cre8ery, Plug In ICA.
3. **Indigenous & ethnic/cultural festivals** — search with LOCALE-SPECIFIC names, not
   generic terms: traditional powwows (Manito Ahbee, Sagkeeng, Long Plain, Dakota Tipi,
   Sioux Valley, Peguis, Waywayseecappo, Sandy Bay), Red River Métis / MMF events, round
   dances, National Indigenous Peoples Day, Naawi-Oodena; and named cultural festivals
   (Folklorama, Íslendingadagurinn/Gimli, Canada's National Ukrainian Festival/Dauphin,
   Fiesta Filipino, Caribe Fest, GreekFest, Vaisakhi, MennoFolk, etc.).
4. **Food, drink & markets** — St. Norbert & Downtown farmers markets, food trucks,
   breweries/taprooms (Little Brown Jug, Nonsuch, Torque, Trans Canada, Oxus, Kilter).
5. **Outdoors, nature & day-trips** — Assiniboine Park, FortWhyte Alive, Bird's Hill,
   Oak Hammock Marsh, Grand/Winnipeg Beach, provincial parks, cycling/paddling.
6. **Family & kids** — Children's Museum, Assiniboine Park Zoo & the Leaf, Manitoba
   Museum/Science Gallery, library programs, all-ages shows.
7. **Nerd & subculture** — board-game nights (Across the Board), trivia, comedy/improv
   (Rumor's), drag/cabaret, anime/gaming meetups, maker/tech, book launches, poetry.
8. **In-city festivals & fairs** — neighbourhood, cultural, street fests, The Forks /
   Old Market Square.
9. **Sports & active** — Goldeyes, Sea Bears (CEBL), Blue Bombers, Valour FC, roller
   derby, races, ticketed fitness.
10. **Far-ahead marquee / sellouts** — big touring concerts & shows at Canada Life
    Centre, Burton Cummings Theatre, Centennial Concert Hall, Club Regent, Park Theatre,
    now through ~6 months out.
11. **Out-of-town destination festivals** Winnipeggers road-trip to (~4-hour radius,
    incl. NW Ontario). Search each BY NAME and verify 2026 dates: Winnipeg Folk Festival
    (Bird's Hill), Harvest Moon (Clearwater), Harvest Sun (Kelwood), Rainbow Trout,
    Elemental (St. Laurent), Take Root, Real Love Summer Fest, Fire & Water (Lac du
    Bonnet), Dauphin's Countryfest, Trout Forest (Ear Falls ON), Half Moon Get Down.
12. **General staples** — Tourism Winnipeg, theforks.com, winnipeg.events, wpgforfree.ca,
    To Do Canada, Ticketmaster/Bandsintown/Songkick.

## Step 2 — shape events

Each event: `{ title, date (human), dateISO (YYYY-MM-DD start), time, venue, url, blurb
(1 sentence), tags[] (free|tix|fam), sellout (bool), stream }`. Stream = one of
confluence, rapids, roar, deep, sky, ripples, horizon. Put any event with `dateISO`
after `window.end` (today + 14 days) into `horizon`. Also pick up to two current marquee
festivals for `headwaters`.

## Step 3 — merge (do NOT overwrite)

Read the existing `data/latest.json` events. Combine with this run's finds, then:
- **Dedupe** by key = normalized title (lowercased, cut at first " - "/" — ", keep
  letters/digits) + `"|"` + `dateISO`. On collision keep the richer (longer-blurb)
  version. A second same-date word-subset pass collapses title variants.
- **Drop** any event whose `dateISO` is before today (past events age out).
- Keep everything else, even if this run didn't re-find it (coverage resilience).
- Sort by `dateISO`.

Write the merged object to **both** `data/latest.json` and `data/YYYY/MM/DD.json`
(today's date), and update `data/index.json` (`latest`, `updated`, append to `runs`).

## Step 4 — publish

Push over HTTPS (SSH/port 22 is blocked in the generating environment):

```
git clone https://x-access-token:<GITHUB_PAT>@github.com/rec3141/winnipegcurrents-site.git /tmp/site
# ...write data files in /tmp/site...
cd /tmp/site && git add data && git commit -m "Data refresh <today> (merged)" && git push origin main
```

`<GITHUB_PAT>` = a fine-grained token scoped to this repo, Contents: read/write. It is
held only in the generator's own config — **never commit it to this repo.** The
DreamHost cron then mirrors `main` to the live site within the hour.
