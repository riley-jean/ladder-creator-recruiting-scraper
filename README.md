# ladder-creator-recruiting-scraper

Automates Nic's #1 pain point on the Ladder Creator Program: manually searching TikTok/IG
for people already using the Ladder app so they can be recruited into the creator program.
Discovers candidates, filters out anyone already known, and outputs a ranked, qualified
prospect list.

Forked from `Cousin-Labs/creator-recruiting-scraper` (the reusable Speak-instance version of
this same pipeline pattern) and adapted for Ladder's fitness niche.

## Requirements

- Python 3 (standard library only — **no pip install needed**, nothing else to set up)
- A ScrapeCreators API key ([scrapecreators.com](https://scrapecreators.com)) — Ladder has its
  own dedicated key; ask Riley or Paul for it if you don't have one. Pay-as-you-go, credits
  never expire.

## Setup

```bash
git clone <this repo>
cd ladder-creator-recruiting-scraper
echo "SCRAPECREATORS_KEY=<your key>" > .env
```

`.env` is gitignored — never commit it. `recruit.py` doesn't auto-load `.env`, so export it
into your shell before running:

```bash
set -a && source .env && set +a
```

## Running it

```bash
python3 recruit.py run --target 20
```

This runs discovery → dedup → enrich → classify → rank, and writes `ladder_prospects.json` +
`ladder_prospects.csv` in the current directory (both gitignored — they contain creator
contact info). `--target 20` means "keep going until 20 qualified prospects are found or
discovery runs dry," not "run exactly 20 API calls" — see **Cost per run** below.

Other flags:
- `--fmin` / `--fmax` — override the follower range (default 1,500–150,000)
- `--out-json` / `--out-csv` — override output filenames
- `--no-ledger` — don't dedup against `ladder_seen.json` (see **Dedup, and the ledger trap**
  below — almost never what you want for a normal weekly run)

## Reading the output

Each row in `ladder_prospects.csv` is one qualified prospect, ranked by composite score
(relevance + engagement). Key columns:

- **Niche Confirmed** — bio/caption text matched a fitness-content signal
- **Composite Score** — `0.4 × relevance + 0.6 × engagement rate`, used for ranking
- **Confidence** — `HIGH` or `LOW - VERIFY`. HIGH means an unambiguous Ladder mention
  (`ladder app`, `@joinladder`, a coach name, etc.). LOW means the account only matched via
  the `#ladderworkout` hashtag (which can also mean generic agility-ladder footwork drills,
  unrelated to the app) or a program/team-name word like "resilient" or "transform" (which
  are also ordinary English words). **LOW-VERIFY rows still ship — check them manually
  before outreach, don't treat them as equivalent to HIGH.**

## How it works

```
discover  → TikTok /v1/tiktok/search/keyword + /search/hashtag, IG /v1/instagram/search/profiles
            (paginated). Free — no credits spent beyond the search call itself.
dedup     → drop handles already in ladder-roster.json, ladder-recruiting-history.json,
            ladder_seen.json, or matching the *fromladder/*withladder handle pattern
free gate → keep follower count in [1.5K, 150K]  (from the free search payload)
enrich    → TikTok /v3/tiktok/profile/videos | IG /v1/instagram/profile  (1 credit each)
            → pulls bio + captions + post dates → posts-in-last-4-weeks, Ladder signal,
              fitness confirmation, competitor-affiliate check
classify  → qualified = follower range + ≥3 posts/4wk + direct Ladder signal +
            fitness-niche confirmed + not a competitor affiliate
rank      → composite score (relevance + engagement), keep top `--target`
accumulate → grow ladder_seen.json so future runs don't resurface the same handles
```

## Dedup sources — and the ledger trap

Three files feed dedup, all merged in `load_seen()`:

| File | What | Update cadence |
|---|---|---|
| `ladder-roster.json` | Active roster (~20 creators). Confirmed matching the live "New & Cont. Creators" tab in the tracker sheet as of 2026-07-15 — re-check periodically for drift. | Manual, whenever the roster changes |
| `ladder-recruiting-history.json` | One-time snapshot (658 records) pulled from Cousin Labs' recruiting-tracker repo, handles only (no PII). | Static — re-pull from source if it goes stale |
| `ladder_seen.json` | Running ledger of every handle ever *discovered* by this tool (not just qualified ones) — grows every run. | Auto, every run |

**The trap:** because `ladder_seen.json` accumulates every discovered handle (qualified or
not), running the tool again without `--no-ledger` will silently skip anyone it already looked
at, even if they didn't qualify last time. This is normally what you want — it's what keeps
weekly runs from re-surfacing the same names. But if you're testing a classifier/query change
and want to see previously-rejected candidates re-evaluated, use `--no-ledger`.

## Config (edit in `recruit.py`)

- `QUERIES` / `TIKTOK_HASHTAGS` — discovery seed terms. Includes Ladder's coach roster
  (`COACH_NAMES` / `COACH_HANDLES`) as both keyword-search and @mention-catch terms.
- `FOLLOWER_MIN` / `FOLLOWER_MAX`, `RECENCY_WEEKS`, `MIN_POSTS_IN_WINDOW` — the gates
- `LADDER_UNAMBIGUOUS_PAT`, `LADDER_AMBIGUOUS_HASHTAG_PAT`, `PROGRAM_NAME_PAT`, `FITNESS_PAT`,
  `COMPETITORS` — the classifier signals (see comments inline for why the ambiguous/program-name
  patterns are split out and flagged low-confidence rather than trusted outright)

## Cost per run

Discovery alone is a fixed ~55–95 raw API calls per run (scales with how many `QUERIES` /
`TIKTOK_HASHTAGS` are configured), plus 1 more call per candidate that survives the follower
gate. Realistic total: **~75-100+ calls per run** at current config. ScrapeCreators' default
is 1 credit = 1 request, but not every endpoint is confirmed flat-rate (their TikTok
audience-demographics endpoint costs 26 credits, for example) — check
`app.scrapecreators.com`'s dashboard for the account's actual per-call cost if you need a
precise credit budget.

## Known limitations

- No automated delivery yet — output is a local CSV, manually imported into the tracker
  sheet as a new tab. There is no scheduled/cron job running this automatically.
- The `Confidence` flag is a stopgap for two known classifier weaknesses (see **Reading the
  output** above), not a full fix — false positives in the `LOW-VERIFY` bucket are expected,
  not a bug to report.
- `Coach Spark` (a handle referenced in Nic's original spec) has never been confirmed as a
  real account — it's not seeded anywhere in this tool. Flag to Nic if it turns out to matter.
