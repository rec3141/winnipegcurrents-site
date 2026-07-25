#!/usr/bin/env python3
"""Daily events refresh for River City Currents.

Port of the Cowork scheduled task described in docs/weekly-refresh-task.md.

Fans out ~12 web-search agents (one per niche) against the Claude API, shapes
the finds into the site's event schema, MERGES them into the existing
data/latest.json (dedupe + drop past events), and writes:

    data/latest.json
    data/YYYY/MM/DD.json
    data/index.json

It never touches index.html. Committing/pushing is the caller's job (see
.github/workflows/daily-refresh.yml).

Usage:
    python scripts/refresh.py                 # full run, writes data/
    python scripts/refresh.py --dry-run       # run + report, write nothing
    python scripts/refresh.py --niches 1,4,7  # subset (debugging)
    python scripts/refresh.py --offline       # merge/write existing data only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

MODEL = "claude-sonnet-5"
EFFORT = "medium"
MAX_TOKENS = 24000
WINDOW_DAYS = 14
MAX_PARALLEL = 6

STREAMS = """\
confluence  The Forks, markets, street food, community gatherings
rapids      live music, nightlife, music festivals, DJs, bar shows
roar        pro sports, big crowds, races
deep        theatre, galleries, film, comedy, literary, ceremony
sky         outdoors, nature, recreation, day-trips, parks
ripples     family & kids programming
horizon     anything worth booking early (auto-assigned if dateISO > window.end)"""

# Step 1 of docs/weekly-refresh-task.md — one searcher per niche.
NICHES = [
    ("indie-live-music", """Indie/DIY live music in Winnipeg. Check venue calendars by name:
Handsome Daughter, Good Will Social Club, Times Change(d) High & Lonesome Club,
Park Theatre, West End Cultural Centre, the Cavern, Bulldog Event Centre,
the Pyramid Cabaret, Sidestage. Include local bills, touring acts, album releases."""),
    ("art-theatre", """Visual art, galleries & theatre in Winnipeg. Check by name:
WAG-Qaumajuq, Royal Manitoba Theatre Centre, Prairie Theatre Exchange,
Dave Barber Cinematheque, aceartinc., Cre8ery, Plug In ICA, Rainbow Stage.
Include current exhibitions with end dates, runs, opening nights, screenings."""),
    ("indigenous-cultural", """Indigenous and ethnic/cultural festivals in and near Winnipeg.
Search LOCALE-SPECIFIC names, not generic terms. Traditional powwows: Manito Ahbee,
Sagkeeng, Long Plain, Dakota Tipi, Sioux Valley, Peguis, Waywayseecappo, Sandy Bay.
Also Red River Metis / Manitoba Metis Federation events, round dances,
National Indigenous Peoples Day, Naawi-Oodena. Named cultural festivals: Folklorama,
Islendingadagurinn (Gimli), Canada's National Ukrainian Festival (Dauphin),
Fiesta Filipino, Caribe Fest, GreekFest, Vaisakhi, MennoFolk."""),
    ("food-markets", """Food, drink & markets in Winnipeg. St. Norbert Farmers' Market,
Downtown Farmers' Market, food truck events, brewery/taproom events at
Little Brown Jug, Nonsuch, Torque, Trans Canada Brewing, Oxus, Kilter.
Include tastings, pop-ups, night markets, food festivals."""),
    ("outdoors-daytrips", """Outdoors, nature & day-trips from Winnipeg. Assiniboine Park,
FortWhyte Alive, Bird's Hill Provincial Park, Oak Hammock Marsh, Grand Beach,
Winnipeg Beach, provincial parks, guided hikes, cycling and paddling events."""),
    ("family-kids", """Family & kids events in Winnipeg. Manitoba Children's Museum,
Assiniboine Park Zoo and The Leaf, Manitoba Museum & Science Gallery,
Winnipeg Public Library programs, all-ages shows, drop-in programs."""),
    ("nerd-subculture", """Nerd & subculture events in Winnipeg. Board-game nights
(Across the Board Game Cafe), trivia nights, comedy and improv (Rumor's Comedy Club),
drag and cabaret, anime/gaming meetups, maker and tech meetups, book launches,
poetry slams and readings."""),
    ("city-festivals", """In-city festivals & fairs in Winnipeg — neighbourhood festivals,
cultural street festivals, events at The Forks and Old Market Square,
block parties, fairs and parades."""),
    ("sports", """Sports & active events in Winnipeg. Winnipeg Goldeyes, Winnipeg Sea Bears
(CEBL), Winnipeg Blue Bombers, Valour FC, Manitoba Moose, roller derby,
running races, cycling races, ticketed fitness events. Include home game dates."""),
    ("marquee-sellouts", """Far-ahead marquee concerts and shows in Winnipeg, from now through
about six months out. Check Canada Life Centre, Burton Cummings Theatre,
Centennial Concert Hall, Club Regent Event Centre, Park Theatre.
Flag anything selling out or likely to. These mostly belong in the horizon stream."""),
    ("out-of-town-fests", """Out-of-town destination festivals Winnipeggers road-trip to
(roughly a 4-hour radius, including northwestern Ontario). Search each BY NAME and
verify this year's dates: Winnipeg Folk Festival (Bird's Hill), Harvest Moon
(Clearwater), Harvest Sun (Kelwood), Rainbow Trout Music Festival, Elemental
(St. Laurent), Take Root, Real Love Summer Fest, Fire & Water Music Festival
(Lac du Bonnet), Dauphin's Countryfest, Trout Forest Music Festival (Ear Falls ON),
Half Moon Get Down."""),
    ("general-staples", """General Winnipeg event listings. Check Tourism Winnipeg,
theforks.com, winnipeg.events, wpgforfree.ca, To Do Canada Winnipeg,
Ticketmaster / Bandsintown / Songkick Winnipeg listings. Prioritize anything
notable that a niche-specific search would miss, especially free events."""),
]

EVENT_SCHEMA = """\
{
  "title":   "Event name, no venue suffix",
  "date":    "Human date. A single day is bare: \\"Jul 26\\" (NO weekday - the site adds it). \\
Ranges and ongoing runs are literal: \\"Aug 8-10\\", \\"Through Sep 2\\", \\"Daily this summer\\"",
  "dateISO": "YYYY-MM-DD of the START date (required, always a real date)",
  "time":    "e.g. \\"8:00 PM\\", \\"Doors 7 PM\\", \\"See listings\\"",
  "venue":   "Venue name, address in parens if useful",
  "url":     "Direct link to the event or its listing page (required)",
  "blurb":   "ONE plain sentence saying what it is. No hype.",
  "tags":    ["subset of: free, tix, fam"],
  "sellout": false,
  "stream":  "one of: confluence, rapids, roar, deep, sky, ripples, horizon"
}"""


# ---------------------------------------------------------------- Claude calls


def _client():
    # Imported lazily so --offline works without the SDK installed.
    import anthropic

    return anthropic.Anthropic()


def _ask(client, prompt: str, label: str) -> str:
    """One web-search-enabled turn; returns the final assistant text.

    Resumes automatically on `pause_turn` (server-tool iteration cap).
    """
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 20}]

    for attempt in range(6):
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            output_config={"effort": EFFORT},
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "refusal":
            raise RuntimeError(f"[{label}] request refused: {response.stop_details}")

        if response.stop_reason == "pause_turn":
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
            continue

        return "".join(b.text for b in response.content if b.type == "text")

    raise RuntimeError(f"[{label}] still paused after {attempt + 1} attempts")


def _parse_json(text: str, label: str):
    """Pull the JSON payload out of a model reply (tolerates ``` fences/preamble)."""
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"[{label}] no JSON found in reply: {text[:400]!r}")


def search_niche(client, key: str, brief: str, window: dict) -> list[dict]:
    prompt = f"""You are gathering real, verifiable events for a Winnipeg events calendar.

TODAY IS {window['start']}. The main window runs {window['start']} to {window['end']}.

NICHE TO COVER:
{brief}

Use web_search aggressively. Verify dates against a primary source (the venue's or
festival's own site) wherever you can — do NOT carry over dates from a previous year.
Skip anything you cannot find a date and a URL for. Skip events that already happened.

Include events inside the window, and also notable events after {window['end']}
that are worth booking ahead — give those "stream": "horizon".

Assign each event a stream:
{STREAMS}

Return ONLY a JSON array of event objects, no prose, no markdown fence. Schema:
{EVENT_SCHEMA}

Aim for 10-30 solid events. Quality and accuracy beat volume — an event with a wrong
date is worse than a missing one."""
    events = _parse_json(_ask(client, prompt, key), key)
    if not isinstance(events, list):
        raise ValueError(f"[{key}] expected a JSON array")
    return events


def search_headwaters(client, window: dict) -> list[dict]:
    prompt = f"""TODAY IS {window['start']}.

Find the ONE or TWO biggest marquee festivals or events happening in or near Winnipeg
right now or in the next several weeks — the things a visitor would plan a trip around.
Use web_search and verify this year's dates on the official site.

Return ONLY a JSON array of 1-2 objects, no prose, no markdown fence:
{{
  "theme": "f1" for the first, "f2" for the second,
  "tag":   "short label, e.g. \\"Cultural festival\\", \\"Music festival\\"",
  "title": "Festival name",
  "meta":  "e.g. \\"Aug 2-15, 2026 · citywide pavilions\\"",
  "blurb": "One or two sentences on what it is.",
  "url":   "Official site",
  "cta":   "Short call to action ending in →, e.g. \\"See pavilions →\\""
}}"""
    items = _parse_json(_ask(client, prompt, "headwaters"), "headwaters")
    return items if isinstance(items, list) else []


# ------------------------------------------------------------- shape and merge

VALID_STREAMS = {"confluence", "rapids", "roar", "deep", "sky", "ripples", "horizon"}
VALID_TAGS = {"free", "tix", "fam"}
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_event(raw: dict, window: dict) -> dict | None:
    """Coerce a model-produced event into the site schema, or drop it."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    iso = str(raw.get("dateISO") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not title or not url or not ISO_RE.match(iso):
        return None
    try:
        date.fromisoformat(iso)
    except ValueError:
        return None

    stream = str(raw.get("stream") or "").strip().lower()
    if stream not in VALID_STREAMS:
        stream = "confluence"
    # Anything past the window is a book-ahead regardless of what the model said.
    if iso > window["end"]:
        stream = "horizon"

    tags = [t for t in raw.get("tags") or [] if isinstance(t, str) and t in VALID_TAGS]

    return {
        "title": title,
        "date": str(raw.get("date") or "").strip() or iso,
        "dateISO": iso,
        "time": str(raw.get("time") or "").strip(),
        "venue": str(raw.get("venue") or "").strip(),
        "url": url,
        "blurb": str(raw.get("blurb") or "").strip(),
        "tags": tags,
        "sellout": bool(raw.get("sellout")),
        "stream": stream,
    }


def norm_title(title: str) -> str:
    """Normalized title: lowercase, cut at the first ' - '/' — ', letters+digits only."""
    t = title.lower()
    for sep in (" - ", " — ", " – ", ": "):
        idx = t.find(sep)
        if idx > 0:
            t = t[:idx]
            break
    return re.sub(r"[^a-z0-9]", "", t)


def _richer(a: dict, b: dict) -> dict:
    """Prefer the entry with the longer blurb; keep a sellout flag if either has it."""
    winner, loser = (a, b) if len(a.get("blurb", "")) >= len(b.get("blurb", "")) else (b, a)
    merged = dict(winner)
    merged["sellout"] = bool(a.get("sellout") or b.get("sellout"))
    merged["tags"] = sorted(set(a.get("tags", [])) | set(b.get("tags", [])))
    for field in ("time", "venue", "url", "blurb"):
        if not merged.get(field):
            merged[field] = loser.get(field, "")
    return merged


def merge_events(existing: list[dict], found: list[dict], today: str) -> list[dict]:
    """Dedupe + drop past events. Existing entries survive even if not re-found."""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for ev in list(existing) + list(found):
        if ev["dateISO"] < today:
            continue  # past events age out
        key = f"{norm_title(ev['title'])}|{ev['dateISO']}"
        if key in by_key:
            by_key[key] = _richer(by_key[key], ev)
        else:
            by_key[key] = ev
            order.append(key)

    # Second pass: same date, one title's words a subset of the other's
    # ("Folklorama 2026" vs "Folklorama 2026 (55th Festival)").
    collapsed: list[dict] = []
    for key in order:
        ev = by_key[key]
        words = set(re.findall(r"[a-z0-9]+", ev["title"].lower()))
        hit = None
        for other in collapsed:
            if other["dateISO"] != ev["dateISO"]:
                continue
            other_words = set(re.findall(r"[a-z0-9]+", other["title"].lower()))
            if words and other_words and (words <= other_words or other_words <= words):
                hit = other
                break
        if hit is None:
            collapsed.append(ev)
        else:
            collapsed[collapsed.index(hit)] = _richer(hit, ev)

    collapsed.sort(key=lambda e: (e["dateISO"], e["title"].lower()))
    return collapsed


# ---------------------------------------------------------------------- output


def label_for(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{start:%B %-d}, {start.year} – {end:%B %-d}, {end.year}"
    if start.month == end.month:
        return f"{start:%B %-d}–{end:%-d}, {end.year}"
    return f"{start:%B %-d} – {end:%B %-d}, {end.year}"


def write_snapshot(snapshot: dict, today: date) -> Path:
    DATA.mkdir(exist_ok=True)
    dated = DATA / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}.json"
    dated.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    dated.write_text(body, encoding="utf-8")
    (DATA / "latest.json").write_text(body, encoding="utf-8")

    index_path = DATA / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"runs": []}
    rel = str(dated.relative_to(REPO))
    runs = [r for r in index.get("runs", []) if r.get("date") != snapshot["generated"]]
    runs.append({"date": snapshot["generated"], "path": rel, "count": len(snapshot["events"])})
    runs.sort(key=lambda r: r["date"])
    index_path.write_text(
        json.dumps({"latest": rel, "updated": snapshot["generated"], "runs": runs},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return dated


# ------------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="run but write nothing")
    ap.add_argument("--offline", action="store_true", help="skip search; just merge/prune existing data")
    ap.add_argument("--niches", help="comma-separated 1-based niche numbers to run")
    args = ap.parse_args()

    today = date.today()
    end = today + timedelta(days=WINDOW_DAYS)
    window = {
        "start": today.isoformat(),
        "end": end.isoformat(),
        "label": label_for(today, end),
    }

    latest_path = DATA / "latest.json"
    previous = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else {}
    existing = [e for e in (clean_event(e, window) for e in previous.get("events", [])) if e]
    headwaters = previous.get("headwaters", [])

    found: list[dict] = []
    if args.offline:
        print("offline mode: merging existing data only")
    else:
        niches = NICHES
        if args.niches:
            picks = {int(n) for n in args.niches.split(",")}
            niches = [n for i, n in enumerate(NICHES, 1) if i in picks]

        client = _client()
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            jobs = {pool.submit(search_niche, client, k, b, window): k for k, b in niches}
            jobs[pool.submit(search_headwaters, client, window)] = "__headwaters__"

            for job in as_completed(jobs):
                key = jobs[job]
                try:
                    result = job.result()
                except Exception as exc:  # one bad searcher must not sink the run
                    print(f"  ! {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                if key == "__headwaters__":
                    if result:
                        headwaters = result[:2]
                    print(f"  ✓ headwaters: {len(result)}")
                    continue
                cleaned = [e for e in (clean_event(e, window) for e in result) if e]
                found.extend(cleaned)
                print(f"  ✓ {key}: {len(cleaned)} events ({len(result) - len(cleaned)} dropped)")

        if not found:
            print("no events found — refusing to write", file=sys.stderr)
            return 1

    merged = merge_events(existing, found, window["start"])
    snapshot = {
        "generated": window["start"],
        "window": window,
        "headwaters": headwaters,
        "events": merged,
    }

    print(
        f"\nwindow {window['label']}\n"
        f"  existing kept : {len(existing)}\n"
        f"  newly found   : {len(found)}\n"
        f"  after merge   : {len(merged)}\n"
        f"  horizon       : {sum(1 for e in merged if e['stream'] == 'horizon')}"
    )

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    dated = write_snapshot(snapshot, today)
    print(f"\nwrote data/latest.json, {dated.relative_to(REPO)}, data/index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
