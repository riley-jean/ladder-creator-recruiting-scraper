#!/usr/bin/env python3
"""
TikTok data-API bake-off: ScrapeCreators vs EnsembleData.

Runs the SAME set of handles through both APIs and reports:
  - latency (ms) per call
  - success / failure per call
  - completeness (# videos returned, profile fields populated)
  - field coverage (which canonical metrics each provider exposes per video)
  - cost consumed (credits / units), when the API reports it

Raw JSON for every call is saved under ./out/ so we can diff field-by-field.

Usage:
  export SCRAPECREATORS_API_KEY=sk_...
  export ENSEMBLEDATA_TOKEN=...
  python3 compare.py handles.txt            # one handle per line
  python3 compare.py charlidamelio khaby.lame   # or pass handles inline

Free tiers are small (ScrapeCreators 100 credits, EnsembleData 50 units/day),
so keep the handle list to ~5-8.
"""
import os, sys, json, time, urllib.parse, urllib.request, urllib.error

SC_KEY = os.environ.get("SCRAPECREATORS_API_KEY", "").strip()
ED_TOK = os.environ.get("ENSEMBLEDATA_TOKEN", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)

# Canonical fields we care about for a creative library / tracking tool.
# We check whether each provider surfaces them ANYWHERE in a video object.
CANON_VIDEO = [
    "play_count/views", "like_count", "comment_count", "share_count", "save_count",
    "create_time", "description", "duration", "music", "hashtags",
    "video_url/download", "cover/thumbnail", "is_ad/sponsored",
]
# substrings that indicate the field is present (case-insensitive, recursive key scan)
FIELD_HINTS = {
    "play_count/views": ["playcount", "play_count", "views", "viewcount", "video_view"],
    "like_count":       ["diggcount", "digg_count", "like_count", "likecount", "likes"],
    "comment_count":    ["commentcount", "comment_count", "comments"],
    "share_count":      ["sharecount", "share_count", "shares", "forward"],
    "save_count":       ["collectcount", "collect_count", "save", "favorite"],
    "create_time":      ["createtime", "create_time", "created", "timestamp"],
    "description":      ["desc", "description", "title", "caption"],
    "duration":         ["duration"],
    "music":            ["music", "sound", "song"],
    "hashtags":         ["hashtag", "challenge", "textextra", "tags"],
    "video_url/download": ["downloadaddr", "download_url", "playaddr", "play_url", "video_url", "bitrate"],
    "cover/thumbnail":  ["cover", "thumbnail", "origincover", "dynamiccover"],
    "is_ad/sponsored":  ["is_ad", "isad", "sponsor", "commerce", "branded", "ad_authorization"],
}
CANON_PROFILE = ["follower_count", "following_count", "heart/likes", "video_count",
                 "bio/signature", "verified", "region", "nickname", "user_id"]
PROFILE_HINTS = {
    "follower_count":  ["followercount", "follower_count", "followers", "fans"],
    "following_count": ["followingcount", "following_count", "following"],
    "heart/likes":     ["heartcount", "heart", "total_favorited", "likes", "diggcount"],
    "video_count":     ["videocount", "video_count", "aweme_count", "post"],
    "bio/signature":   ["signature", "bio", "desc"],
    "verified":        ["verified", "verification", "is_verified"],
    "region":          ["region", "country", "location"],
    "nickname":        ["nickname", "display_name", "name"],
    "user_id":         ["uid", "user_id", "id", "sec_uid", "secuid"],
}


def get(url, headers=None, timeout=40):
    """GET -> (status, elapsed_ms, parsed_json_or_text, err)."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            ms = int((time.time() - t0) * 1000)
            try:
                return r.status, ms, json.loads(body), None
            except json.JSONDecodeError:
                return r.status, ms, body, "non-json body"
    except urllib.error.HTTPError as e:
        ms = int((time.time() - t0) * 1000)
        detail = e.read().decode("utf-8", "replace")[:300]
        return e.code, ms, None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return None, ms, None, f"{type(e).__name__}: {e}"


def all_keys(obj, acc=None):
    """Recursively collect every dict key (lowercased) in a nested structure."""
    acc = acc if acc is not None else set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(str(k).lower())
            all_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj[:5]:  # sample first few items
            all_keys(v, acc)
    return acc


def coverage(keyset, hints):
    present = {}
    for field, subs in hints.items():
        present[field] = any(any(s in k for k in keyset) for s in subs)
    return present


def find_video_list(obj):
    """Heuristically locate the list of video objects in a response."""
    candidates = []
    def walk(o):
        if isinstance(o, list):
            if o and isinstance(o[0], dict):
                candidates.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
    walk(obj)
    return max(candidates, key=len) if candidates else []


def scrapecreators(handle):
    base = "https://api.scrapecreators.com"
    h = {"x-api-key": SC_KEY}
    res = {"provider": "ScrapeCreators", "handle": handle}
    st, ms, prof, err = get(f"{base}/v1/tiktok/profile?handle={urllib.parse.quote(handle)}", h)
    res["profile_status"], res["profile_ms"], res["profile_err"] = st, ms, err
    json.dump(prof, open(f"{OUT}/sc_{handle}_profile.json", "w"), indent=2, default=str)
    res["profile_fields"] = coverage(all_keys(prof), PROFILE_HINTS) if prof else {}
    st, ms, vids, err = get(f"{base}/v3/tiktok/profile/videos?handle={urllib.parse.quote(handle)}", h)
    res["videos_status"], res["videos_ms"], res["videos_err"] = st, ms, err
    json.dump(vids, open(f"{OUT}/sc_{handle}_videos.json", "w"), indent=2, default=str)
    vlist = find_video_list(vids) if vids else []
    res["video_count"] = len(vlist)
    res["video_fields"] = coverage(all_keys(vlist[0]) if vlist else set(), FIELD_HINTS)
    return res


def ensembledata(handle):
    base = "https://ensembledata.com/apis"
    res = {"provider": "EnsembleData", "handle": handle}
    st, ms, info, err = get(f"{base}/tt/user/info?username={urllib.parse.quote(handle)}&token={ED_TOK}")
    res["profile_status"], res["profile_ms"], res["profile_err"] = st, ms, err
    json.dump(info, open(f"{OUT}/ed_{handle}_profile.json", "w"), indent=2, default=str)
    res["profile_fields"] = coverage(all_keys(info), PROFILE_HINTS) if info else {}
    st, ms, posts, err = get(f"{base}/tt/user/posts?username={urllib.parse.quote(handle)}&depth=1&token={ED_TOK}")
    res["videos_status"], res["videos_ms"], res["videos_err"] = st, ms, err
    json.dump(posts, open(f"{OUT}/ed_{handle}_videos.json", "w"), indent=2, default=str)
    # EnsembleData reports units consumed in the body
    if isinstance(posts, dict):
        res["units_charged"] = posts.get("units_charged") or posts.get("unitsCharged")
    vlist = find_video_list(posts) if posts else []
    res["video_count"] = len(vlist)
    res["video_fields"] = coverage(all_keys(vlist[0]) if vlist else set(), FIELD_HINTS)
    return res


def fmt_cov(cov, canon_keys):
    return " ".join(("✓" if cov.get(k) else "·") for k in canon_keys)


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: python3 compare.py <handles.txt | handle1 handle2 ...>"); sys.exit(1)
    if len(args) == 1 and os.path.isfile(args[0]):
        handles = [l.strip().lstrip("@") for l in open(args[0]) if l.strip() and not l.startswith("#")]
    else:
        handles = [a.lstrip("@") for a in args]

    missing = [n for n, v in [("SCRAPECREATORS_API_KEY", SC_KEY), ("ENSEMBLEDATA_TOKEN", ED_TOK)] if not v]
    if missing:
        print(f"⚠️  Missing env keys: {', '.join(missing)} — that provider will be skipped.\n")

    rows = []
    for hd in handles:
        print(f"→ {hd}")
        if SC_KEY:
            r = scrapecreators(hd); rows.append(r)
            print(f"   SC  profile {r['profile_status']} {r['profile_ms']}ms | videos {r['videos_status']} {r['videos_ms']}ms | {r['video_count']} vids"
                  + (f" | err:{r['videos_err'] or r['profile_err']}" if (r['videos_err'] or r['profile_err']) else ""))
        if ED_TOK:
            r = ensembledata(hd); rows.append(r)
            print(f"   ED  profile {r['profile_status']} {r['profile_ms']}ms | posts {r['videos_status']} {r['videos_ms']}ms | {r['video_count']} vids"
                  + (f" | units:{r.get('units_charged')}" if r.get('units_charged') else "")
                  + (f" | err:{r['videos_err'] or r['profile_err']}" if (r['videos_err'] or r['profile_err']) else ""))
        time.sleep(1.0)  # be polite to free tiers

    # ---- summary ----
    print("\n" + "=" * 78)
    print("LATENCY & COMPLETENESS")
    print(f"{'provider':<14}{'handle':<22}{'prof ms':>8}{'vids ms':>9}{'#vids':>7}")
    for r in rows:
        print(f"{r['provider']:<14}{r['handle']:<22}{r.get('profile_ms',0):>8}{r.get('videos_ms',0):>9}{r.get('video_count',0):>7}")

    print("\nVIDEO FIELD COVERAGE   (" + " ".join(CANON_VIDEO) + ")")
    for r in rows:
        print(f"  {r['provider']:<14}{r['handle']:<20} {fmt_cov(r.get('video_fields',{}), FIELD_HINTS.keys())}")

    print("\nPROFILE FIELD COVERAGE (" + " ".join(CANON_PROFILE) + ")")
    for r in rows:
        print(f"  {r['provider']:<14}{r['handle']:<20} {fmt_cov(r.get('profile_fields',{}), PROFILE_HINTS.keys())}")

    json.dump(rows, open(f"{OUT}/summary.json", "w"), indent=2, default=str)
    print(f"\nRaw JSON + summary.json written to {OUT}/")


if __name__ == "__main__":
    main()
