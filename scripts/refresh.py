#!/usr/bin/env python3
"""Daily events refresh for River City Currents.

Port of the Cowork scheduled task described in docs/weekly-refresh-task.md.

Fans out 8 web-search agents (one per niche) against the Claude API, shapes
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

Cost is bounded three ways: MAX_SEARCHES per niche, MAX_RESUMES on the
pause_turn loop, and a REFRESH_BUDGET_USD ceiling that stops launching new
searchers. Every run prints its running token/dollar total.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

MODEL = "claude-sonnet-5"
EFFORT = "medium"
MAX_TOKENS = 16000
WINDOW_DAYS = 14
MAX_PARALLEL = 4

# Cost controls. The first live run drained a balance in 14 minutes without a
# single searcher finishing, so these are deliberately tight.
MAX_SEARCHES = 5      # web searches per niche; each result lands in context
MAX_RESUMES = 2       # pause_turn resumptions; each re-sends the whole turn
BUDGET_USD = float(os.environ.get("REFRESH_BUDGET_USD", "2.00"))

# Claude Sonnet 5 standard rates, $ per million tokens (intro pricing is lower,
# so this over-estimates rather than under-estimates). Web search is per call.
PRICE_IN, PRICE_OUT = 3.00, 15.00
PRICE_CACHE_READ, PRICE_CACHE_WRITE = 0.30, 3.75
PRICE_PER_SEARCH = 10.00 / 1000

STREAMS = """\
confluence  The Forks, markets, street food, community gatherings
rapids      live music, nightlife, music festivals, DJs, bar shows
roar        pro sports, big crowds, races
deep        theatre, galleries, film, comedy, literary, ceremony
sky         outdoors, nature, recreation, day-trips, parks
ripples     family & kids programming
horizon     anything worth booking early (auto-assigned if dateISO > window.end)"""

# Step 1 of docs/weekly-refresh-task.md, consolidated from 12 searchers to 8
# after the first live run proved 13 concurrent calls too expensive. Related
# niches share a call; the daily merge accumulates coverage across runs.
NICHES = [
    ("live-music", """Live music in Winnipeg — indie/DIY through mid-size rooms.
Check venue calendars by name: Handsome Daughter, Good Will Social Club,
Times Change(d) High & Lonesome Club, Park Theatre, West End Cultural Centre,
the Cavern, Bulldog Event Centre, the Pyramid Cabaret, Sidestage.
Local bills, touring acts, album releases."""),
    ("art-theatre-film", """Visual art, theatre & film in Winnipeg. Check by name:
WAG-Qaumajuq, Royal Manitoba Theatre Centre, Prairie Theatre Exchange,
Dave Barber Cinematheque, aceartinc., Cre8ery, Plug In ICA, Rainbow Stage.
Current exhibitions with end dates, runs, opening nights, screenings."""),
    ("indigenous-cultural", """Indigenous and ethnic/cultural festivals in and near Winnipeg.
Search LOCALE-SPECIFIC names, not generic terms. Traditional powwows: Manito Ahbee,
Sagkeeng, Long Plain, Dakota Tipi, Sioux Valley, Peguis, Waywayseecappo, Sandy Bay.
Also Red River Metis / Manitoba Metis Federation events, round dances,
National Indigenous Peoples Day, Naawi-Oodena. Named cultural festivals: Folklorama,
Islendingadagurinn (Gimli), Canada's National Ukrainian Festival (Dauphin),
Fiesta Filipino, Caribe Fest, GreekFest, Vaisakhi, MennoFolk."""),
    ("food-drink-markets", """Food, drink & markets in Winnipeg. St. Norbert Farmers' Market,
Downtown Farmers' Market, food truck events, brewery/taproom events at
Little Brown Jug, Nonsuch, Torque, Trans Canada Brewing, Oxus, Kilter.
Tastings, pop-ups, night markets, food festivals."""),
    ("outdoors-family", """Outdoors, day-trips, and family/kids programming around Winnipeg.
Assiniboine Park, FortWhyte Alive, Bird's Hill Provincial Park, Oak Hammock Marsh,
Grand Beach, Winnipeg Beach, provincial parks, guided hikes, cycling and paddling.
Also Manitoba Children's Museum, Assiniboine Park Zoo and The Leaf,
Manitoba Museum & Science Gallery, library programs, all-ages shows."""),
    ("community-nerd-fests", """Community festivals and subculture events in Winnipeg.
Neighbourhood and cultural street festivals, events at The Forks and Old Market
Square, block parties, fairs, parades. Also board-game nights (Across the Board
Game Cafe), trivia, comedy and improv (Rumor's Comedy Club), drag and cabaret,
anime/gaming meetups, maker and tech meetups, book launches, poetry slams."""),
    ("sports-and-marquee", """Sports and big-venue ticketed shows in Winnipeg.
Winnipeg Goldeyes, Winnipeg Sea Bears (CEBL), Winnipeg Blue Bombers, Valour FC,
Manitoba Moose, roller derby, running and cycling races — include home game dates.
Also marquee concerts and touring shows at Canada Life Centre, Burton Cummings
Theatre, Centennial Concert Hall, Club Regent Event Centre; flag likely sellouts."""),
    ("roadtrips-and-listings", """Two things. (1) Out-of-town destination festivals
Winnipeggers road-trip to (roughly a 4-hour radius, including northwestern Ontario).
Search each BY NAME and verify this year's dates: Winnipeg Folk Festival (Bird's Hill),
Harvest Moon (Clearwater), Harvest Sun (Kelwood), Rainbow Trout Music Festival,
Elemental (St. Laurent), Take Root, Real Love Summer Fest, Fire & Water Music Festival
(Lac du Bonnet), Dauphin's Countryfest, Trout Forest Music Festival (Ear Falls ON),
Half Moon Get Down. (2) Sweep the general listings — Tourism Winnipeg, theforks.com,
winnipeg.events, wpgforfree.ca, To Do Canada — for notable free events a
niche-specific search would miss."""),
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


class Fatal(Exception):
    """Unrecoverable: every remaining call would fail the same way."""


class Spend:
    """Thread-safe running cost estimate, so a run is never a black box."""

    def __init__(self, budget: float):
        self.budget = budget
        self.lock = threading.Lock()
        self.abort = threading.Event()   # set on Fatal — stop everything now
        self.calls = self.searches = 0
        self.tok_in = self.tok_out = self.cache_read = self.cache_write = 0

    def record(self, usage) -> None:
        with self.lock:
            self.calls += 1
            self.tok_in += getattr(usage, "input_tokens", 0) or 0
            self.tok_out += getattr(usage, "output_tokens", 0) or 0
            self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
            server = getattr(usage, "server_tool_use", None)
            self.searches += getattr(server, "web_search_requests", 0) or 0

    @property
    def usd(self) -> float:
        return (
            self.tok_in / 1e6 * PRICE_IN
            + self.tok_out / 1e6 * PRICE_OUT
            + self.cache_read / 1e6 * PRICE_CACHE_READ
            + self.cache_write / 1e6 * PRICE_CACHE_WRITE
            + self.searches * PRICE_PER_SEARCH
        )

    @property
    def exhausted(self) -> bool:
        return self.usd >= self.budget

    def report(self) -> str:
        return (
            f"{self.calls} calls · {self.searches} searches · "
            f"in {self.tok_in:,} (+{self.cache_read:,} cached) · out {self.tok_out:,} · "
            f"~${self.usd:.2f} of ${self.budget:.2f}"
        )


def _client():
    # Imported lazily so --offline works without the SDK installed.
    import anthropic

    # An explicit timeout matters: the first live run had searchers hang for
    # 13 minutes after the account hit zero balance.
    return anthropic.Anthropic(timeout=300.0, max_retries=2)


def _is_fatal(exc: Exception) -> bool:
    """Credit exhaustion and bad auth doom every other in-flight call too."""
    text = str(exc).lower()
    return any(
        s in text
        for s in ("credit balance", "authentication_error", "invalid x-api-key",
                  "permission_error", "billing")
    )


def _ask(client, prompt: str, label: str, spend: Spend) -> str:
    """One web-search-enabled turn; returns the final assistant text.

    Resumes on `pause_turn`, but only MAX_RESUMES times: each resumption
    re-sends the whole accumulated turn (every search result fetched so far),
    so the cost of resuming grows with each round. A cache_control breakpoint
    on the last block means the resend is billed at cache-read rates.
    """
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": MAX_SEARCHES}]

    for attempt in range(MAX_RESUMES + 1):
        if spend.abort.is_set():
            raise Fatal(f"[{label}] aborted before call {attempt + 1}")
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
                tools=tools,
                messages=messages,
            ) as stream:
                response = stream.get_final_message()
        except Exception as exc:
            if _is_fatal(exc):
                spend.abort.set()
                raise Fatal(f"[{label}] {exc}") from exc
            raise

        spend.record(response.usage)

        if response.stop_reason == "refusal":
            raise RuntimeError(f"[{label}] refused: {response.stop_details}")

        if response.stop_reason == "pause_turn":
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
            continue

        return "".join(b.text for b in response.content if b.type == "text")

    # Out of resumptions: salvage whatever text the last turn produced rather
    # than throwing away everything we just paid for.
    text = "".join(b.text for b in response.content if b.type == "text")
    if text.strip():
        print(f"  ~ {label}: hit the resumption cap, using a partial answer", file=sys.stderr)
        return text
    raise RuntimeError(f"[{label}] still paused after {MAX_RESUMES + 1} calls, no text")


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


def search_niche(client, key: str, brief: str, window: dict, spend: Spend) -> list[dict]:
    if spend.exhausted:
        raise RuntimeError(f"[{key}] skipped — budget reached before launch")
    prompt = f"""You are gathering real, verifiable events for a Winnipeg events calendar.

TODAY IS {window['start']}. The main window runs {window['start']} to {window['end']}.

NICHE TO COVER:
{brief}

Use web_search sparingly and precisely. Verify dates against a primary source (the
venue's or festival's own site) — do NOT carry over dates from a previous year.
Skip anything you cannot find a date and a URL for. Skip events that already happened.

Include events inside the window, and also notable events after {window['end']}
that are worth booking ahead — give those "stream": "horizon".

Assign each event a stream:
{STREAMS}

Return ONLY a JSON array of event objects, no prose, no markdown fence. Schema:
{EVENT_SCHEMA}

You have a hard budget of about {MAX_SEARCHES} web searches, so choose them well: prefer one
listing page covering many events over one search per event. Aim for 10-25 solid
events. Accuracy beats volume — an event with a wrong date is worse than a missing
one."""
    events = _parse_json(_ask(client, prompt, key, spend), key)
    if not isinstance(events, list):
        raise ValueError(f"[{key}] expected a JSON array")
    return events


def search_headwaters(client, window: dict, spend: Spend) -> list[dict]:
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
    items = _parse_json(_ask(client, prompt, "headwaters", spend), "headwaters")
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

        spend = Spend(BUDGET_USD)
        print(f"budget ${BUDGET_USD:.2f} · {len(niches)} niches + headwaters · "
              f"{MAX_SEARCHES} searches each\n")

        client = _client()
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            jobs = {pool.submit(search_niche, client, k, b, window, spend): k
                    for k, b in niches}
            jobs[pool.submit(search_headwaters, client, window, spend)] = "__headwaters__"

            for job in as_completed(jobs):
                key = jobs[job]
                try:
                    result = job.result()
                except Fatal as exc:
                    # Credit exhausted or bad key: every sibling is doomed too.
                    print(f"  !! {key}: {exc}\n  !! aborting run", file=sys.stderr)
                    continue
                except Exception as exc:  # one bad searcher must not sink the run
                    print(f"  ! {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                if key == "__headwaters__":
                    if result:
                        headwaters = result[:2]
                    print(f"  ✓ headwaters: {len(result)}  [{spend.report()}]")
                    continue
                cleaned = [e for e in (clean_event(e, window) for e in result) if e]
                found.extend(cleaned)
                print(f"  ✓ {key}: {len(cleaned)} events "
                      f"({len(result) - len(cleaned)} dropped)  [{spend.report()}]")

        print(f"\nspend: {spend.report()}")
        if spend.abort.is_set():
            print("run aborted on a fatal API error — nothing written", file=sys.stderr)
            return 1
        if spend.exhausted:
            print("budget reached: some niches were skipped, writing what we got",
                  file=sys.stderr)

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
