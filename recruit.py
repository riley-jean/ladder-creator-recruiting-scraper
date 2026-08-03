#!/usr/bin/env python3
"""
Creator recruiting funnel — ScrapeCreators API. TikTok + Instagram.

Universal core + per-client config. This file is the client-agnostic pipeline;
each client (Speak, Ladder, ...) is a config in the clients/ package, selected
with --client. No forks — one tool, one pipeline.

Pipeline (run to target):
  discover -> keyword/hashtag (TikTok) + profile search (IG); paginate; aggregate unique handles
  dedup    -> drop handles in the client's seen files + running ledger + client pre_drop rule
  free gate-> follower range (from search payload, no extra credits)
  enrich   -> TikTok /v3/tiktok/profile/videos | IG /v1/instagram/profile (1 credit each)
              -> per-post captions/hashtags/dates/engagement, bio
  classify -> client rules set qualification + ranking fields
  rank     -> client rank key, cap to target
  export   -> JSON + CSV (+ optional Google Sheet append if the client sets a sheet)
  accumulate seen handles into the client's ledger so future runs don't resurface them

Usage:
  export SCRAPECREATORS_KEY=...
  python recruit.py run --client ladder --target 20
  python recruit.py run --client speak  --target 200
"""
import argparse, csv, json, os, re, sys, time, urllib.parse, urllib.request
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

from clients import get_client, CLIENTS
from clients.base import norm

WORKERS = 12
BASE = "https://api.scrapecreators.com"
RECENCY_WEEKS = 4

_SEEN_KEYS = ("handle", "handles", "tiktok", "tiktok_handle", "instagram",
              "instagram_handle", "roster_handles", "username", "seen")


def call(key, path, params):
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": key})
    last = "unknown error"
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            last = str(e); time.sleep(1)
    return {"error": last}


def load_seen(cfg, use_ledger=True):
    seen = set()
    paths = list(cfg.seen_files) + ([cfg.ledger_file] if use_ledger else [])
    for path in paths:
        try:
            rec = json.load(open(path))
        except Exception:
            continue

        def harvest(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k.lower() in _SEEN_KEYS:
                        if isinstance(v, str):
                            seen.add(norm(v))
                        elif isinstance(v, list):
                            for x in v:
                                if isinstance(x, str):
                                    seen.add(norm(x))
                    harvest(v)
            elif isinstance(o, list):
                for x in o:
                    harvest(x)
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
        if cursor:
            params["cursor"] = cursor
        d = call(key, "/v1/instagram/search/profiles", params)
        profs = d.get("profiles", []) or []
        for p in profs:
            links = [l.get("url") for l in (p.get("bio_links") or [])] + ([p["external_url"]] if p.get("external_url") else [])
            out.append(("instagram", p.get("username"), p.get("follower_count"),
                        p.get("biography"), f"ig:{q}", None, links))
        cursor = d.get("cursor"); pages += 1
        if not cursor or not profs:
            break
    return out


def discover(cfg, key, seen):
    tasks = ([(_task_tt_kw, q) for q in cfg.queries]
             + [(_task_tt_tag, t) for t in cfg.tiktok_hashtags]
             + [(_task_ig, q) for q in cfg.queries])
    pool, done = {}, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fn, key, arg): arg for fn, arg in tasks}
        for fut in as_completed(futs):
            done += 1
            for (platform, handle, followers, bio, src, ts, links) in (fut.result() or []):
                h = norm(handle)
                if not h or h in seen or cfg.pre_drop(h):
                    continue
                r = pool.setdefault((platform, h), {"handle": handle, "platform": platform,
                    "followers": followers, "bio": bio or "", "found_via": set(), "search_ts": [],
                    "ig_links": links or []})
                r["found_via"].add(src)
                if followers and not r["followers"]:
                    r["followers"] = followers
                if ts:
                    r["search_ts"].append(ts)
            if done % 10 == 0:
                print(f"discovery {done}/{len(tasks)} tasks, pool={len(pool)}", file=sys.stderr)
    for r in pool.values():
        r["found_via"] = sorted(r["found_via"])
    return list(pool.values())


# ---------------- enrichment ----------------
def _tt_hashtags(desc):
    return re.findall(r"#(\w+)", desc or "")


def enrich_tiktok(key, r):
    """Per-post dicts: caption, hashtags, date, url, engagement."""
    d = call(key, "/v3/tiktok/profile/videos", {"handle": r["handle"]})
    vids = d.get("aweme_list", []) or []
    if vids and not r.get("bio"):
        r["bio"] = (vids[0].get("author", {}) or {}).get("signature", "") or ""
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
    """Per-post dicts. IG's public payload has no share/save counts, so those are 0."""
    d = call(key, "/v1/instagram/profile", {"handle": r["handle"]})
    u = (d.get("data") or {}).get("user") or {}
    if u.get("biography") and not r.get("bio"):
        r["bio"] = u["biography"]
    for l in (u.get("bio_links") or []):
        if l.get("url"):
            r.setdefault("ig_links", []).append(l["url"])
    if u.get("external_url"):
        r.setdefault("ig_links", []).append(u["external_url"])
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


# ---------------- Google Sheet output (optional, append mode) ----------------
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
# Your own OAuth client ID (Desktop app), downloaded from the Cloud Console.
# Each user logs in as themselves on first run; the token is cached locally.
# gcloud's default client can't be used for Sheets/Drive scopes anymore, and
# Cousin Labs' org policy blocks service-account key files — so this is the path.
OAUTH_CLIENT_FILE = os.environ.get("SHEET_OAUTH_CLIENT", "oauth_client.json")
OAUTH_TOKEN_FILE = "oauth_token.json"


def sheet_client(creds_path):
    """Authorized gspread client. Uses a service-account key if present (not
    available under Cousin Labs' policy); otherwise logs in the current user via
    our own OAuth client, caching the token for reuse."""
    import gspread
    path = creds_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.exists(path):
        from google.oauth2.service_account import Credentials
        return gspread.authorize(Credentials.from_service_account_file(path, scopes=SHEET_SCOPES))
    if os.path.exists(OAUTH_CLIENT_FILE):
        return gspread.oauth(scopes=SHEET_SCOPES,
                             credentials_filename=OAUTH_CLIENT_FILE,
                             authorized_user_filename=OAUTH_TOKEN_FILE)
    raise RuntimeError(
        f"No Google auth found. Put your OAuth client file at '{OAUTH_CLIENT_FILE}' "
        "(Cloud Console -> APIs & Services -> Credentials -> Create OAuth client ID "
        "-> Desktop app -> Download JSON).")


def write_to_sheet(header, rows, sheet_id, tab, creds_path):
    """Append rows to a Google Sheet tab; write the header only when the tab is empty."""
    try:
        import gspread
    except ImportError:
        raise RuntimeError("gspread not installed — run: pip install -r requirements.txt")
    sh = sheet_client(creds_path).open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=max(len(rows) + 10, 100), cols=len(header))
    if not ws.get_all_values():
        ws.append_row(header, value_input_option="USER_ENTERED")
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


# ---------------- main run ----------------
def run(cfg, key, target, out_json, out_csv, fmin, fmax, use_ledger=True,
        creds_path=None, write_sheet=True):
    cfg.follower_min, cfg.follower_max = fmin, fmax
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    cutoff = now - RECENCY_WEEKS * 7 * 86400
    seen = load_seen(cfg, use_ledger=use_ledger)
    print(f"known/seen handles: {len(seen)}", file=sys.stderr)

    pool = discover(cfg, key, seen)
    print(f"\nDISCOVERED {len(pool)} new unique handles", file=sys.stderr)

    cand = [r for r in pool if fmin <= (r.get("followers") or 0) <= fmax]
    print(f"FREE-GATE (followers {fmin}-{fmax}): {len(cand)} candidates to enrich", file=sys.stderr)

    def enrich_one(r):
        try:
            posts = enrich_tiktok(key, r) if r["platform"] == "tiktok" else enrich_instagram(key, r)
        except Exception as e:
            r["enrich_error"] = str(e); return r
        r["posts_last_4wk"] = sum(1 for p in posts if (p.get("post_ts") or 0) >= cutoff)
        cfg.classify(r, posts)
        r["_qual"] = cfg.qualifies(r)
        return r

    stop_at = target * cfg.overcollect
    qualified, enriched_n, i, BATCH = [], 0, 0, WORKERS * 6
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        while i < len(cand) and len(qualified) < stop_at:
            chunk = cand[i:i + BATCH]; i += BATCH
            for r in ex.map(enrich_one, chunk):
                enriched_n += 1
                if r.get("_qual"):
                    qualified.append(r)
            print(f"  enriched={enriched_n} qualified={len(qualified)}", file=sys.stderr)

    print(f"\nQUALIFIED: {len(qualified)} (enriched {enriched_n})", file=sys.stderr)

    qualified.sort(key=cfg.rank_key)
    qualified = qualified[:target]

    # export — build the row set once, reuse for JSON, CSV, and the Google Sheet
    run_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    header = cfg.export_header()
    rows = [cfg.export_row(r, run_date) for r in qualified]

    json.dump({"generated": run_date, "client": cfg.name,
               "target": target, "qualified_count": len(qualified),
               "criteria": cfg.criteria(),
               "qualified": qualified}, open(out_json, "w"), indent=2, ensure_ascii=False)
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    # grow the client's seen ledger so future runs don't resurface these handles
    try:
        ledger = set(json.load(open(cfg.ledger_file)).get("seen", []))
    except Exception:
        ledger = set()
    ledger |= {norm(r["handle"]) for r in pool}
    json.dump({"seen": sorted(ledger)}, open(cfg.ledger_file, "w"))
    print(f"{cfg.ledger_file} now {len(ledger)} handles", file=sys.stderr)
    print(f"wrote {out_json}, {out_csv}", file=sys.stderr)

    # optional Google Sheet append; local files are already saved, so a Sheet/auth
    # failure is a warning, not a run-killer.
    if write_sheet and cfg.sheet_id:
        try:
            n = write_to_sheet(header, rows, cfg.sheet_id, cfg.sheet_tab, creds_path)
            print(f"appended {n} rows to Google Sheet tab '{cfg.sheet_tab}'", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: Google Sheet write skipped ({e}); local CSV/JSON still written",
                  file=sys.stderr)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    p.add_argument("--key", default=os.environ.get("SCRAPECREATORS_KEY"))
    r = sub.add_parser("run")
    r.add_argument("--client", required=True, choices=sorted(CLIENTS),
                   help="which client config to run")
    r.add_argument("--target", type=int, default=None)
    r.add_argument("--out-json", default=None)
    r.add_argument("--out-csv", default=None)
    r.add_argument("--fmin", type=int, default=None)
    r.add_argument("--fmax", type=int, default=None)
    r.add_argument("--no-ledger", action="store_true", help="don't dedup against the client's seen ledger")
    r.add_argument("--creds", default=None, help="path to service account JSON key (or set GOOGLE_APPLICATION_CREDENTIALS)")
    r.add_argument("--no-sheet", action="store_true", help="skip the Google Sheet write, local files only")
    a = p.parse_args()
    if not a.key:
        sys.exit("set SCRAPECREATORS_KEY or pass --key")
    cfg = get_client(a.client)
    run(cfg, a.key,
        target=a.target if a.target is not None else cfg.target_default,
        out_json=a.out_json or cfg.out_json_default,
        out_csv=a.out_csv or cfg.out_csv_default,
        fmin=a.fmin if a.fmin is not None else cfg.follower_min,
        fmax=a.fmax if a.fmax is not None else cfg.follower_max,
        use_ledger=not a.no_ledger, creds_path=a.creds, write_sheet=not a.no_sheet)
