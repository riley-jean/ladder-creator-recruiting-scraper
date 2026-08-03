"""Speak (Spanish-learning app) client config.

Ported unchanged from the original single-client recruit.py so Speak's output
and ranking are byte-for-byte the same — only the plumbing moved.
"""
import re

from .base import Client, norm, profile_url

MIN_POSTS_IN_WINDOW = 3

COMPETITORS = ["duolingo", "babbel", "busuu", "pimsleur", "rosetta", "memrise", "lingoda", "preply", "italki",
    "rocket languages", "drops", "lingvist", "parrot", "tryparrot", "jumpspeak", "pingo", "loora", "langua",
    "elsaspeak", "speakly", "lingq", "lingopie", "fluentu", "mango languages", "speakeasy"]
AFFILIATE_PAT = re.compile(r"(use code|descuento|discount|link in bio|% off|sponsored|#ad\b|partner|affiliate|promo code|cdigo)", re.I)
BRAND_PROGRAM_PAT = re.compile(r"(spanish|espanol|espa|lingo|langua|bilingual|spanglish)[\._]?(with|by|w|con|teacher|tutor|coach|teach|learn|habla)[\._]?\w+", re.I)
SPANISH_PAT = re.compile(r"(spanish|espa[nñ]ol|espanol|spanglish|habla|hispano|hispanic|latin|latina|latino|boricua|mexican|colombian|nosabo|no\s?sabo|aprend|bilingual.*span|spain)", re.I)


class SpeakClient(Client):
    name = "speak"
    follower_min, follower_max = 2000, 50000
    target_default = 200
    overcollect = 1
    out_json_default = "prospects.json"
    out_csv_default = "prospects.csv"

    queries = [
        "learn spanish", "learning spanish", "study spanish", "spanish lessons", "spanish for beginners",
        "spanish teacher", "spanish tutor", "easy spanish", "spanish vocabulary", "spanish phrases", "spanish class",
        "no sabo", "no sabo kids", "heritage speaker spanish", "heritage language", "spanglish", "habla espanol",
        "bilingual mom", "bilingual kids", "raising bilingual kids", "bilingual family", "spanish for kids",
        "teaching kids spanish", "bilingual household",
        "spanish slang", "mexican slang", "latina creator", "aprender espanol",
    ]
    tiktok_hashtags = ["NoSaboKids", "BilingualKids", "RaisingBilingualKids", "SpanishForKids", "HeritageLanguage",
                       "learnspanish", "spanishteacher", "spanglish", "nosabo", "aprenderespanol"]

    seen_files = ["speak-creator-recruiting.json"]
    ledger_file = "master_seen.json"

    def classify(self, r, posts):
        h = r["handle"]
        caps = " ".join(p.get("caption", "") for p in posts)
        if BRAND_PROGRAM_PAT.search(h):
            r["handle_pattern"], r["handle_pattern_score"] = "brand_program_pattern", 1
        elif SPANISH_PAT.search(h):
            r["handle_pattern"], r["handle_pattern_score"] = "organic_niche_handle", 1
        else:
            r["handle_pattern"], r["handle_pattern_score"] = "generic_handle", 0
        text = f"{r.get('bio','')} {caps} {' '.join(r.get('ig_links', []))}".lower()
        hits = [c for c in COMPETITORS if c in text]
        if hits:
            r["competitor_flag"] = "COMPETITOR_AFFILIATE:" + ",".join(sorted(set(hits)))
        elif r["handle_pattern"] == "brand_program_pattern" or AFFILIATE_PAT.search(text):
            r["competitor_flag"] = "potential_competing_program_could_poach"
        else:
            r["competitor_flag"] = "clear"
        r["is_spanish"] = bool(SPANISH_PAT.search(f"{h} {r.get('bio','')} {caps}"))

    def qualifies(self, r):
        return (r.get("posts_last_4wk", 0) >= MIN_POSTS_IN_WINDOW and r.get("is_spanish")
                and not r.get("competitor_flag", "").startswith("COMPETITOR_AFFILIATE"))

    def rank_key(self, r):
        return (-(r.get("handle_pattern_score") or 0), -(r.get("followers") or 0))

    def export_header(self):
        return ["Platform", "Handle", "Pattern", "URL", "Followers", "Posts Last 4wk",
                "Competitor Check", "Bio", "Found Via"]

    def export_row(self, r, run_date):
        return [r["platform"], "@" + r["handle"], r.get("handle_pattern", ""), profile_url(r),
                r.get("followers"), r.get("posts_last_4wk"), r.get("competitor_flag", ""),
                (r.get("bio", "") or "").replace("\n", " ")[:200], "; ".join(r.get("found_via", []))]

    def criteria(self):
        return {"followers": [self.follower_min, self.follower_max], "min_posts_4wk": MIN_POSTS_IN_WINDOW,
                "spanish_only": True, "platforms": ["tiktok", "instagram"]}
