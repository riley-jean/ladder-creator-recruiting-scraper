"""Ladder (fitness app) client config.

Ported unchanged from the Ladder single-client recruit.py: same discovery seeds,
signals, confidence split, ranking, and 21-column export (incl. the Google Sheet
append). Only the plumbing moved onto the shared core.
"""
import re

from .base import Client, norm, profile_url, eng_rate

MIN_POSTS_IN_WINDOW = 3

# ---- discovery ----
QUERIES = ["ladder app", "ladder workout", "ladder coach", "ladder fitness",
           "joinladder", "ladder training", "ladder gym"]
TIKTOK_HASHTAGS = ["ladder", "ladderapp", "ladderworkout", "define", "teamthrive", "limitless", "transform",
                   "joinladder", "laddercoach"]

# Coach roster (Viral App, 2026-07-08). Same handle on TikTok + IG.
COACH_NAMES = ["shelby robins", "corey perkins", "nicole winter", "maia henry",
               "kelly matthews", "jennifer jacobs", "allegra paris", "brian pruett"]
COACH_HANDLES = ["shelbyrobinss", "perkfitt", "nicolemwinter_", "maiahenryfit",
                 "kellylmatthews", "jmethod", "allegraparis", "brian_pruett"]

QUERIES = QUERIES + COACH_NAMES + COACH_HANDLES
TIKTOK_HASHTAGS = TIKTOK_HASHTAGS + COACH_HANDLES

# ---- signals ----
LADDER_UNAMBIGUOUS_PAT = re.compile(
    r"(ladder\s?app|ladderapp|#ladderapp\b|joinladder|ladder\s?coach|laddercoachspark)", re.I)
LADDER_AMBIGUOUS_HASHTAG_PAT = re.compile(r"#ladderworkout\b", re.I)
PROGRAM_NAME_PAT = re.compile(
    r"\b(define|limitless|transform|thrive|resilient|forged|vantage|ascend|vitality)\b|project\s?alpha", re.I)
LADDER_HANDLE_PAT = re.compile(r"(fromladder|withladder)$", re.I)
LADDER_PAT = re.compile(LADDER_UNAMBIGUOUS_PAT.pattern + "|" + LADDER_AMBIGUOUS_HASHTAG_PAT.pattern
                        + "|" + PROGRAM_NAME_PAT.pattern, re.I)
FITNESS_PAT = re.compile(
    r"(fitness|work\s?out|\bgym\b|training|trainer|personal\s?coach|\bfit\b|exercise|strength|lifting|cardio|nutrition|health\s?journey)",
    re.I)
COMPETITORS = ["beachbody", "bodi", "peloton", "orangetheory", "f45", "tonal",
               "future app", "fitbod", "caliber"]
AFFILIATE_PAT = re.compile(r"(use code|discount|link in bio|% off|sponsored|#ad\b|partner|affiliate|promo code)", re.I)

RELEVANCE_WEIGHT = 0.4
ENGAGEMENT_WEIGHT = 0.6


def _pick_representative_post(posts, followers):
    """Highest-engagement post that mentions Ladder; else highest-engagement overall."""
    matches = [p for p in posts if LADDER_PAT.search(p.get("caption", "") or "")]
    pool = matches if matches else posts
    if not pool:
        return None, 0.0
    best = max(pool, key=lambda p: eng_rate(p, followers))
    return best, eng_rate(best, followers)


def _relevance_score(handle, text_lower):
    if LADDER_HANDLE_PAT.search(handle):
        return 1.0
    if LADDER_UNAMBIGUOUS_PAT.search(text_lower):
        return 0.9
    if any(c in text_lower for c in COACH_NAMES):
        return 0.85
    if LADDER_AMBIGUOUS_HASHTAG_PAT.search(text_lower):
        return 0.5
    if PROGRAM_NAME_PAT.search(text_lower):
        return 0.5
    return 0.0


class LadderClient(Client):
    name = "ladder"
    follower_min, follower_max = 1500, 150000
    target_default = 20
    overcollect = 5
    out_json_default = "ladder_prospects.json"
    out_csv_default = "ladder_prospects.csv"

    queries = QUERIES
    tiktok_hashtags = TIKTOK_HASHTAGS

    seen_files = ["ladder-roster.json", "ladder-recruiting-history.json"]
    ledger_file = "ladder_seen.json"

    # Dedicated sourcing-output sheet (2026-07-29) — NOT Nic's roster/tracker sheet.
    sheet_id = "1XRIeMdRJiq1EeGRfdMEFZy6YVqNMUMPyj6AD6fco7WM"
    sheet_tab = "Sourcing Output"

    def pre_drop(self, handle):
        """Handles like davefromladder are almost certainly existing/alumni creators."""
        return bool(LADDER_HANDLE_PAT.search(norm(handle)))

    def classify(self, r, posts):
        h = r["handle"]
        all_caps = " ".join(p.get("caption", "") for p in posts)
        text = f"{h} {r.get('bio','')} {all_caps}"
        text_lower = text.lower()
        r["ladder_signal"] = bool(LADDER_PAT.search(text)) or bool(LADDER_HANDLE_PAT.search(h))
        r["fitness_confirmed"] = bool(FITNESS_PAT.search(text))
        r["relevance_score"] = _relevance_score(h, text_lower)
        r["signal_confidence"] = ("high" if (LADDER_UNAMBIGUOUS_PAT.search(text) or LADDER_HANDLE_PAT.search(h))
                                  else "low")
        hits = [c for c in COMPETITORS if c in text_lower]
        if hits:
            r["competitor_flag"] = "COMPETITOR_AFFILIATE:" + ",".join(sorted(set(hits)))
        elif AFFILIATE_PAT.search(text_lower):
            r["competitor_flag"] = "potential_affiliate_or_sponsored"
        else:
            r["competitor_flag"] = "clear"
        rep, er = _pick_representative_post(posts, r.get("followers") or 0)
        r["representative_post"] = rep
        r["eng_rate"] = round(er, 4)
        r["composite_score"] = round(RELEVANCE_WEIGHT * r["relevance_score"]
                                     + ENGAGEMENT_WEIGHT * min(er, 1.0), 4)

    def qualifies(self, r):
        return (r.get("posts_last_4wk", 0) >= MIN_POSTS_IN_WINDOW and r.get("ladder_signal")
                and r.get("fitness_confirmed")
                and not r.get("competitor_flag", "").startswith("COMPETITOR_AFFILIATE")
                and r.get("representative_post") is not None)

    def rank_key(self, r):
        return -r.get("composite_score", 0.0)

    def export_header(self):
        return ["Run Date", "Handle", "Profile URL", "Platform", "Followers", "Likes", "Comments", "Shares", "Saves",
                "Video Views", "Eng Rate", "Caption", "Hashtags Used", "Post Date", "Post URL", "Bio",
                "Niche Confirmed", "Link In Bio", "Composite Score", "Found Via", "Confidence"]

    def export_row(self, r, run_date):
        rep = r.get("representative_post") or {}
        link_in_bio = (r.get("ig_links") or [None])[0]
        confidence = ("HIGH" if r.get("signal_confidence") == "high"
                      else "LOW - VERIFY (matched only via #ladderworkout hashtag and/or a generic program-name word)")
        return [
            run_date, "@" + r["handle"], profile_url(r), r["platform"], r.get("followers"),
            rep.get("likes"), rep.get("comments"), rep.get("shares"), rep.get("saves"),
            rep.get("views"), r.get("eng_rate"),
            (rep.get("caption", "") or "").replace("\n", " ")[:200],
            " ".join(rep.get("hashtags", []) or []),
            rep.get("post_date"), rep.get("post_url"),
            (r.get("bio", "") or "").replace("\n", " ")[:200],
            r.get("fitness_confirmed"), link_in_bio, r.get("composite_score"),
            "; ".join(r.get("found_via", [])), confidence,
        ]

    def criteria(self):
        return {"followers": [self.follower_min, self.follower_max], "min_posts_4wk": MIN_POSTS_IN_WINDOW,
                "ladder_fitness_only": True, "platforms": ["tiktok", "instagram"]}
