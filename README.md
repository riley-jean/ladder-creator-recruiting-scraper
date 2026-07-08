# creator-recruiting-scraper

Creator discovery + recruiting funnel built on the [ScrapeCreators](https://scrapecreators.com) API.
Given a niche, it discovers candidate creators across TikTok + Instagram, filters them down to a
qualified prospect list, and dedups against creators we already know.

First instance is the **Speak** Spanish-learning funnel (`recruit.py`). The discovery → gate →
enrich → classify pattern is the reusable core; the seed terms and qualification rules are the
per-niche config.

## Why ScrapeCreators

Chosen as our creator-data layer over per-seat SaaS (e.g. Upfluence ~$9.5k/yr): a pay-as-you-go
API (~1 credit/call, credits never expire) that we drive from code, across 35+ platforms. See the
head-to-head vs EnsembleData in [`eval/api-comparison/`](eval/api-comparison/).

## Setup

```bash
export SCRAPECREATORS_KEY=...        # get from scrapecreators.com
# note: if your shell exports it as SCRAPE_CREATORS_API_KEY, alias it:
#   export SCRAPECREATORS_KEY="$SCRAPE_CREATORS_API_KEY"
python recruit.py run --target 200 --out-json prospects.json --out-csv prospects.csv
```

## How `recruit.py` works

```
discover  → TikTok /v1/tiktok/search/keyword + /search/hashtag, IG /v1/instagram/search/profiles
            paginate, aggregate unique handles  (search payload is free of extra credits)
dedup     → drop handles already in speak-creator-recruiting.json + master_seen.json
free gate → keep follower count in [2K, 50K]  (from the search payload, no extra credits)
enrich    → TikTok /v3/tiktok/profile/videos | IG /v1/instagram/profile  (1 credit each)
            → bio + captions + dated posts → posts-in-last-4-weeks, competitor scan, Spanish check
classify  → qualified = follower range + ≥3 posts/4wk + Spanish-learning signal + not a competitor affiliate
            brand-program-template handles are kept and flagged as poach leads
accumulate until --target qualified, then export CSV/JSON and grow master_seen.json
```

**Qualification rules** (Speak instance): follower ∈ [2K, 50K]; ≥3 posts in last 4 weeks;
Spanish-learning niche signal in handle/bio/captions; excludes competitor affiliates
(Duolingo/Babbel/Preply/etc.).

## Config (edit in `recruit.py`)

- `QUERIES` — keyword discovery seeds (many angles for non-overlapping reach)
- `TIKTOK_HASHTAGS` — hashtag discovery seeds
- `FOLLOWER_MIN/MAX`, `RECENCY_WEEKS`, `MIN_POSTS_IN_WINDOW` — the gates
- `COMPETITORS`, `SPANISH_PAT`, `BRAND_PROGRAM_PAT` — the classifier signals

## Files

| Path | What |
|---|---|
| `recruit.py` | The recruiting funnel |
| `speak-creator-recruiting.json` | Known/contracted creators — dedup input |
| `master_seen.json` | Running ledger of handles already surfaced — dedup input, grows each run |
| `eval/api-comparison/` | ScrapeCreators vs EnsembleData bake-off (harness + viral.app baseline) |

Generated prospect outputs (`prospects.*`, `speak_prospects.xlsx`, etc.) are **gitignored** —
they contain creator contact info (PII) and are regenerated per run.

## Roadmap — generalize to genre-outlier discovery

This funnel is the seed of "Job 1" in `cousins-os/future-ideas-log.md`: a plain-language category
("female workout instruction") → LLM-expanded hashtags/keywords/anchor accounts → ScrapeCreators
discovery → **outlier scoring** (content punching above its own account baseline, not just high
absolute views) → tiered tracking of the top ~100 accounts into viral.app. `recruit.py`'s discovery
+ enrich scaffolding is directly reusable; what changes is the seed layer and swapping the
qualification rules for an outlier-ranking + stratified-sampling step.
