#!/usr/bin/env python3
"""
Ladder creator sourcing funnel — ScrapeCreators API. Fitness-only, TikTok + Instagram.

Built for Nic's #1 pain point on the Ladder Creator Program: manually searching
TikTok/IG for people already using the Ladder app so they can be recruited into
the creator program. This replaces that manual search.

Qualified prospect =
  - follower count in [1.5K, 150K]  (soft pre-filter, not a priority signal)
  - >= 3 posts in the last 4 weeks
  - direct Ladder signal (app name, coach name, or team/program name + fitness context)
  - fitness-niche content confirmed (so a bare "#limitless" post about something
    unrelated to fitness doesn't slip through)
  - NOT already an active/alumni Ladder creator (roster + running seen-ledger +
    "*fromladder"/"*withladder" handle pattern pre-check)

Pipeline (run to target):
  discover -> keyword/hashtag (TikTok) + profile search (IG); paginate; aggregate unique handles
  dedup    -> drop handles in ladder-roster.json + ladder_seen.json + fromladder/withladder handle pattern
  free gate-> follower range (from search payload, no extra credits)
  enrich   -> TikTok /v3/tiktok/profile/videos | IG /v1/instagram/profile (1 credit each)
              -> per-post captions/hashtags/dates/engagement, bio, Ladder + fitness checks
  rank     -> composite score (relevance + engagement), export top ~target
  accumulate seen handles into ladder_seen.json so future runs don't resurface them

Usage:
  export SCRAPECREATORS_KEY=...
  python recruit.py run --target 20 --out-json ladder_prospects.json --out-csv ladder_prospects.csv
"""
import argparse, csv, json, os, re, sys, time, threading, urllib.parse, urllib.request
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKERS = 12

BASE = "https://api.scrapecreators.com"
FOLLOWER_MIN, FOLLOWER_MAX = 1500, 150000  # soft pre-filter only — Riley: not the most important factor
RECENCY_WEEKS = 4
MIN_POSTS_IN_WINDOW = 3
SLEEP = 0.25

# ---- Ladder discovery query set (per Nic's spec, 2026-07-08) ----
# Widened 2026-07-15 after the first full-batch live run returned only 1
# qualified prospect out of 32 enriched candidates: that prospect's post
# tagged "@joinladder" directly, a term the seed set never searched for.
# Added "joinladder" (the app's actual handle/mention form) plus a couple
# natural phrasing variants consistent with the existing "ladder <noun>" style.
QUERIES = ["ladder app", "ladder workout", "ladder coach", "ladder fitness",
           "joinladder", "ladder training", "ladder gym"]
TIKTOK_HASHTAGS = ["ladder", "ladderapp", "ladderworkout", "define", "teamthrive", "limitless", "transform",
                   "joinladder", "laddercoach"]

# Coach roster pulled from Viral App (2026-07-08 screenshot). Handles are
# identical across TikTok/IG for every coach so far. "Ladder Coach Spark" was
# never confirmed as a real handle (not in this list, not found anywhere in
# the LCP repo) — still open with Nic.
COACH_NAMES = ["shelby robins", "corey perkins", "nicole winter", "maia henry",
               "kelly matthews", "jennifer jacobs", "allegra paris", "brian pruett"]
COACH_HANDLES = ["shelbyrobinss", "perkfitt", "nicolemwinter_", "maiahenryfit",
                  "kellylmatthews", "jmethod", "allegraparis", "brian_pruett"]

if COACH_NAMES:
    QUERIES += COACH_NAMES
if COACH_HANDLES:
    QUERIES += COACH_HANDLES        # catches @handle mentions in captions/bios via keyword search
    TIKTOK_HASHTAGS += COACH_HANDLES

# ---- signals ----
# Truly unambiguous Ladder mentions — no realistic false-positive path.
LADDER_UNAMBIGUOUS_PAT = re.compile(
    r"(ladder\s?app|ladderapp|#ladderapp\b|joinladder|ladder\s?coach|laddercoachspark)", re.I)
# Ambiguous hashtag — added 2026-07-15 after a widened-discovery run qualified
# @kasciussm, whose "#ladderworkout" tag was about physical agility-ladder
# footwork drills (a generic training-equipment niche), not the Ladder app.
# Kept in the qualification gate per Riley's direction (still surfaces the
# candidate rather than dropping it silently) but flagged low-confidence
# below since the term alone doesn't reliably mean "Ladder app."
LADDER_AMBIGUOUS_HASHTAG_PAT = re.compile(r"#ladderworkout\b", re.I)
# Ladder team/program names — these are also generic English words (e.g. "limitless",
# "transform", "resilient"), so on their own they're weak signal. A 2026-07-15 run
# qualified @marcelletti24 (a baseball-training account) purely because its caption
# used "resilient" in an ordinary sentence — FITNESS_PAT doesn't actually filter this
# out since every discovery candidate is fitness content by construction. Kept in the
# qualification gate per Riley's direction, flagged low-confidence below.
PROGRAM_NAME_PAT = re.compile(
    r"\b(define|limitless|transform|thrive|resilient|forged|vantage|ascend|vitality)\b|project\s?alpha", re.I)
# Existing/alumni Ladder creators' handles follow this pattern almost universally
# (18/20 rows in the July roster export) — used both as an extra dedupe pre-check
# and as a relevance signal if one slips past the roster/seen-ledger check.
LADDER_HANDLE_PAT = re.compile(r"(fromladder|withladder)$", re.I)
# Combined gate used for qualification — unambiguous mention, ambiguous hashtag,
# or program name (still requires FITNESS_PAT to pass _qual). Unchanged behavior
# from before 2026-07-15 — only the confidence split below is new.
LADDER_PAT = re.compile(LADDER_UNAMBIGUOUS_PAT.pattern + "|" + LADDER_AMBIGUOUS_HASHTAG_PAT.pattern
                        + "|" + PROGRAM_NAME_PAT.pattern, re.I)
# Fitness-niche confirmation gate (the "actually fitness" check from Nic's spec).
FITNESS_PAT = re.compile(
    r"(fitness|work\s?out|\bgym\b|training|trainer|personal\s?coach|\bfit\b|exercise|strength|lifting|cardio|nutrition|health\s?journey)",
    re.I)

# Direct fitness-app/program competitors — seeded 2026-07-15 after the first live
# run surfaced a Beachbody-affiliated account (handle "bodibybeachbody") as a false
# positive: it only matched because a coach-name keyword search ("jennifer jacobs")
# happens to also return unrelated content. Substring match against handle+bio+
# captions catches brand mentions in the handle itself, not just captions.
COMPETITORS = ["beachbody", "bodi", "peloton", "orangetheory", "f45", "tonal",
               "future app", "fitbod", "caliber"]
AFFILIATE_PAT = re.compile(r"(use code|discount|link in bio|% off|sponsored|#ad\b|partner|affiliate|promo code)", re.I)


def call(key, path, params):
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": key})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            last = str(e); time.sleep(1)
    return {"error": last}


def norm(h): return (h or "").strip().lstrip("@").lower()


def is_probable_alumni(handle):
    """Cheap pre-check: handles like davefromladder/dillonwithladder are almost
    certainly existing/alumni creators even if not yet in ladder-roster.json."""
    return bool(LADDER_HANDLE_PAT.search(norm(handle)))


def load_seen(use_ledger=True):
    seen = set()
    paths = ["ladder-roster.json", "ladder-recruiting-history.json"] + (["ladder_seen.json"] if use_ledger else [])
    for path in paths:
        try: rec = json.load(open(path))
        except Exception: continue
        def harvest(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k.lower() in ("handle","handles","tiktok","tiktok_handle","instagram",
                                      "instagram_handle","roster_handles","username","seen"):
                        if isinstance(v, str): seen.add(norm(v))
                        elif isinstance(v, list):
                            for x in v:
                                if isinstance(x, str): seen.add(norm(x))
                    harvest(v)
            elif isinstance(o, list):
                for x in o: harvest(x)
        harvest(rec)
    return seen


# ---------------- discovery (concurrent) ----------------
def _task_tt_kw(key, q):
    d = call(key, "/v1/tiktok/search/keyword", {"query": q}); out = []
    for it in d.get("search_item_list", []):
        a = it.get("aweme_info", {}); au = a.get("author", {})
        if au.get("unique_id"):
            out.append(("tiktok", au["unique_id"], au.get("follower_count"), au.get("signature"),
                        f"tt:{q}", a.get("create_time"), None))
    return out

def _task_tt_tag(key, tag):
    d = call(key, "/v1/tiktok/search/hashtag", {"hashtag": tag}); out = []
    for it in d.get("search_item_list", []):
        a = it.get("aweme_info", {}); au = a.get("author", {})
        if au.get("unique_id"):
            out.append(("tiktok", au["unique_id"], au.get("follower_count"), au.get("signature"),
                        f"tt:#{tag}", a.get("create_time"), None))
    return out

def _task_ig(key, q):
    out, cursor, pages = [], None, 0
    while pages < 3:
        params = {"query": q}
        if cursor: params["cursor"] = cursor
        d = call(key, "/v1/instagram/search/profiles", params)
        profs = d.get("profiles", []) or []
        for p in profs:
            links = [l.get("url") for l in (p.get("bio_links") or [])] + ([p["external_url"]] if p.get("external_url") else [])
            out.append(("instagram", p.get("username"), p.get("follower_count"),
                        p.get("biography"), f"ig:{q}", None, links))
        cursor = d.get("cursor"); pages += 1
        if not cursor or not profs: break
    return out

def discover(key, seen):
    tasks = ([(_task_tt_kw, q) for q in QUERIES]
             + [(_task_tt_tag, t) for t in TIKTOK_HASHTAGS]
             + [(_task_ig, q) for q in QUERIES])
    pool, done = {}, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fn, key, arg): arg for fn, arg in tasks}
        for fut in as_completed(futs):
            done += 1
            for (platform, handle, followers, bio, src, ts, links) in (fut.result() or []):
                h = norm(handle)
                if not h or h in seen or is_probable_alumni(h): continue
                r = pool.setdefault((platform, h), {"handle": handle, "platform": platform,
                    "followers": followers, "bio": bio or "", "found_via": set(), "search_ts": [],
                    "ig_links": links or []})
                r["found_via"].add(src)
                if followers and not r["followers"]: r["followers"] = followers
                if ts: r["search_ts"].append(ts)
            if done % 10 == 0:
                print(f"discovery {done}/{len(tasks)} tasks, pool={len(pool)}", file=sys.stderr)
    for r in pool.values(): r["found_via"] = sorted(r["found_via"])
    return list(pool.values())


# ---------------- enrichment ----------------
def _tt_hashtags(desc): return re.findall(r"#(\w+)", desc or "")

def enrich_tiktok(key, r):
    """Returns a list of per-post dicts: caption, hashtags, date, url, engagement."""
    d = call(key, "/v3/tiktok/profile/videos", {"handle": r["handle"]})
    vids = d.get("aweme_list", []) or []
    if vids and not r.get("bio"):
        r["bio"] = (vids[0].get("author", {}) or {}).get("signature", "") or ""
    # TODO: verify whether the ScrapeCreators TikTok profile/videos response exposes
    # a bio link field (e.g. author.bio_url) — not confirmed yet, so link_in_bio is
    # left empty for TikTok prospects until checked against a real response.
    posts = []
    for v in vids:
        stats = v.get("statistics", {}) or {}
        desc = v.get("desc", "") or ""
        ts = v.get("create_time") or 0
        vid_id = v.get("aweme_id") or v.get("id")
        posts.append({
            "caption": desc,
            "hashtags": _tt_hashtags(desc),
            "post_ts": ts,
            "post_date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if ts else None,
            "post_url": f"https://www.tiktok.com/@{r['handle']}/video/{vid_id}" if vid_id else None,
            "views": stats.get("play_count", 0) or 0,
            "likes": stats.get("digg_count", 0) or 0,
            "comments": stats.get("comment_count", 0) or 0,
            "shares": stats.get("share_count", 0) or 0,
            "saves": stats.get("collect_count", 0) or 0,
        })
    return posts


def enrich_instagram(key, r):
    """Returns a list of per-post dicts. Note: IG's public payload doesn't expose
    share/save counts, so those are always 0 for Instagram prospects."""
    d = call(key, "/v1/instagram/profile", {"handle": r["handle"]})
    u = (d.get("data") or {}).get("user") or {}
    if u.get("biography") and not r.get("bio"): r["bio"] = u["biography"]
    for l in (u.get("bio_links") or []):
        if l.get("url"): r.setdefault("ig_links", []).append(l["url"])
    if u.get("external_url"): r.setdefault("ig_links", []).append(u["external_url"])
    edges = (u.get("edge_owner_to_timeline_media") or {}).get("edges") or []
    edges += (u.get("edge_felix_video_timeline") or {}).get("edges") or []
    posts = []
    for e in edges:
        n = e.get("node", {})
        ts = n.get("taken_at_timestamp") or 0
        caption = ""
        cap_edges = (n.get("edge_media_to_caption", {}) or {}).get("edges") or []
        if cap_edges:
            caption = cap_edges[0].get("node", {}).get("text", "") or ""
        shortcode = n.get("shortcode")
        likes = (n.get("edge_liked_by") or n.get("edge_media_preview_like") or {}).get("count", 0) or 0
        comments = (n.get("edge_media_to_comment") or {}).get("count", 0) or 0
        views = n.get("video_view_count", 0) or 0
        posts.append({
            "caption": caption,
            "hashtags": re.findall(r"#(\w+)", caption),
            "post_ts": ts,
            "post_date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if ts else None,
            "post_url": f"https://instagram.com/p/{shortcode}/" if shortcode else None,
            "views": views,
            "likes": likes,
            "comments": comments,
            "shares": 0,
            "saves": 0,
        })
    return posts


def pick_representative_post(posts, followers):
    """Pick the highest-engagement post among those that actually mention Ladder;
    fall back to the highest-engagement post overall if none match. This is what
    gets exported as the creator's caption/post date/post url/engagement fields."""
    def eng_rate(p):
        interactions = (p.get("likes",0) or 0) + (p.get("comments",0) or 0) + (p.get("shares",0) or 0) + (p.get("saves",0) or 0)
        denom = max(p.get("views") or 0, followers or 0, 1)
        return interactions / denom
    matches = [p for p in posts if LADDER_PAT.search(p.get("caption","") or "")]
    pool = matches if matches else posts
    if not pool: return None, 0.0
    best = max(pool, key=eng_rate)
    return best, eng_rate(best)


def relevance_score(handle, text_lower):
    """Strength of Ladder signal, used as the relevance half of the composite score."""
    if LADDER_HANDLE_PAT.search(handle): return 1.0          # handle itself says fromladder/withladder
    if LADDER_UNAMBIGUOUS_PAT.search(text_lower): return 0.9  # unambiguous app/coach mention
    if COACH_NAMES and any(c.lower() in text_lower for c in COACH_NAMES): return 0.85
    if LADDER_AMBIGUOUS_HASHTAG_PAT.search(text_lower): return 0.5  # #ladderworkout only — could be agility-drill content
    if PROGRAM_NAME_PAT.search(text_lower): return 0.5        # team/program name only — weaker
    return 0.0


def classify(r, posts):
    h = r["handle"]
    all_caps = " ".join(p.get("caption","") for p in posts)
    text = f"{h} {r.get('bio','')} {all_caps}"
    text_lower = text.lower()
    r["ladder_signal"] = bool(LADDER_PAT.search(text)) or bool(LADDER_HANDLE_PAT.search(h))
    r["fitness_confirmed"] = bool(FITNESS_PAT.search(text))
    r["relevance_score"] = relevance_score(h, text_lower)
    # Confidence split — added 2026-07-15. "High" = an unambiguous mention or the
    # handle pattern itself; "Low" = qualified only via the ambiguous #ladderworkout
    # hashtag and/or a generic program-name word (define/limitless/transform/thrive/
    # resilient/forged/vantage/ascend/vitality), which real runs showed can false-
    # positive on unrelated fitness content. Low-confidence prospects still ship —
    # they're just flagged for manual verification before outreach, not dropped.
    r["signal_confidence"] = ("high" if (LADDER_UNAMBIGUOUS_PAT.search(text) or LADDER_HANDLE_PAT.search(h))
                               else "low")
    hits = [c for c in COMPETITORS if c in text_lower]
    if hits:
        r["competitor_flag"] = "COMPETITOR_AFFILIATE:" + ",".join(sorted(set(hits)))
    elif AFFILIATE_PAT.search(text_lower):
        r["competitor_flag"] = "potential_affiliate_or_sponsored"
    else:
        r["competitor_flag"] = "clear"


RELEVANCE_WEIGHT = 0.4
ENGAGEMENT_WEIGHT = 0.6

def composite_score(r):
    return RELEVANCE_WEIGHT * r.get("relevance_score", 0.0) + ENGAGEMENT_WEIGHT * min(r.get("eng_rate", 0.0), 1.0)


def profile_url(r):
    return (f"https://www.tiktok.com/@{r['handle']}" if r["platform"] == "tiktok"
            else f"https://instagram.com/{r['handle']}")


# ---------------- main run ----------------
def run(key, target, out_json, out_csv, fmin=FOLLOWER_MIN, fmax=FOLLOWER_MAX, use_ledger=True):
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    cutoff = now - RECENCY_WEEKS * 7 * 86400
    seen = load_seen(use_ledger=use_ledger)
    print(f"known/seen handles: {len(seen)}", file=sys.stderr)

    pool = discover(key, seen)
    print(f"\nDISCOVERED {len(pool)} new unique handles", file=sys.stderr)

    # free gate: follower range only (soft pre-filter, saves credits — not a priority signal)
    cand = [r for r in pool if fmin <= (r.get("followers") or 0) <= fmax]
    print(f"FREE-GATE (followers {fmin}-{fmax}): {len(cand)} candidates to enrich", file=sys.stderr)

    def enrich_one(r):
        try:
            posts = enrich_tiktok(key, r) if r["platform"] == "tiktok" else enrich_instagram(key, r)
        except Exception as e:
            r["enrich_error"] = str(e); return r
        r["posts_last_4wk"] = sum(1 for p in posts if (p.get("post_ts") or 0) >= cutoff)
        classify(r, posts)
        rep, eng_rate = pick_representative_post(posts, r.get("followers") or 0)
        r["representative_post"] = rep
        r["eng_rate"] = round(eng_rate, 4)
        r["composite_score"] = round(composite_score(r), 4)
        r["_qual"] = (r["posts_last_4wk"] >= MIN_POSTS_IN_WINDOW and r["ladder_signal"] and r["fitness_confirmed"]
                      and not r["competitor_flag"].startswith("COMPETITOR_AFFILIATE")
                      and rep is not None)
        return r

    qualified, enriched_n, i, BATCH = [], 0, 0, WORKERS * 6
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        while i < len(cand) and len(qualified) < target * 5:
            chunk = cand[i:i + BATCH]; i += BATCH
            for r in ex.map(enrich_one, chunk):
                enriched_n += 1
                if r.get("_qual"): qualified.append(r)
            print(f"  enriched={enriched_n} qualified={len(qualified)}", file=sys.stderr)

    print(f"\nQUALIFIED: {len(qualified)} (enriched {enriched_n})", file=sys.stderr)

    # rank by composite score (relevance + engagement) and cap to target
    qualified.sort(key=lambda r: -r.get("composite_score", 0.0))
    qualified = qualified[:target]

    # export
    json.dump({"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
               "target": target, "qualified_count": len(qualified),
               "criteria": {"followers": [fmin, fmax], "min_posts_4wk": MIN_POSTS_IN_WINDOW,
                            "ladder_fitness_only": True, "platforms": ["tiktok", "instagram"]},
               "qualified": qualified}, open(out_json, "w"), indent=2, ensure_ascii=False)
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Handle","Profile URL","Platform","Followers","Likes","Comments","Shares","Saves",
                    "Video Views","Eng Rate","Caption","Hashtags Used","Post Date","Post URL","Bio",
                    "Niche Confirmed","Link In Bio","Composite Score","Found Via","Confidence"])
        for r in qualified:
            rep = r.get("representative_post") or {}
            link_in_bio = (r.get("ig_links") or [None])[0]
            confidence = ("HIGH" if r.get("signal_confidence") == "high"
                          else "LOW - VERIFY (matched only via #ladderworkout hashtag and/or a generic program-name word)")
            w.writerow([
                "@"+r["handle"], profile_url(r), r["platform"], r.get("followers"),
                rep.get("likes"), rep.get("comments"), rep.get("shares"), rep.get("saves"),
                rep.get("views"), r.get("eng_rate"),
                (rep.get("caption","") or "").replace("\n"," ")[:200],
                " ".join(rep.get("hashtags", []) or []),
                rep.get("post_date"), rep.get("post_url"),
                (r.get("bio","") or "").replace("\n"," ")[:200],
                r.get("fitness_confirmed"), link_in_bio, r.get("composite_score"),
                "; ".join(r.get("found_via", [])), confidence,
            ])

    # grow ladder_seen ledger so future runs don't resurface these handles
    try: ledger = set(json.load(open("ladder_seen.json")).get("seen", []))
    except Exception: ledger = set()
    ledger |= {norm(r["handle"]) for r in pool}
    json.dump({"seen": sorted(ledger)}, open("ladder_seen.json", "w"))
    print(f"ladder_seen now {len(ledger)} handles", file=sys.stderr)
    print(f"wrote {out_json}, {out_csv}", file=sys.stderr)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    p.add_argument("--key", default=os.environ.get("SCRAPECREATORS_KEY"))
    r = sub.add_parser("run")
    r.add_argument("--target", type=int, default=20)
    r.add_argument("--out-json", default="ladder_prospects.json")
    r.add_argument("--out-csv", default="ladder_prospects.csv")
    r.add_argument("--fmin", type=int, default=FOLLOWER_MIN)
    r.add_argument("--fmax", type=int, default=FOLLOWER_MAX)
    r.add_argument("--no-ledger", action="store_true", help="don't dedup against ladder_seen.json")
    a = p.parse_args()
    if not a.key: sys.exit("set SCRAPECREATORS_KEY or pass --key")
    run(a.key, a.target, a.out_json, a.out_csv, fmin=a.fmin, fmax=a.fmax, use_ledger=not a.no_ledger)
