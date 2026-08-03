"""Client adapter base for the recruiting scraper.

The pipeline in recruit.py is client-agnostic. Everything that differs between
clients (Speak, Ladder, ...) lives on a Client subclass: the discovery seed set,
follower range, dedup sources, and the classify / qualify / rank / export hooks
the core calls at each stage.
"""


def norm(h):
    return (h or "").strip().lstrip("@").lower()


def profile_url(r):
    return (f"https://www.tiktok.com/@{r['handle']}" if r["platform"] == "tiktok"
            else f"https://instagram.com/{r['handle']}")


def eng_rate(post, followers):
    """Interactions over the larger of views / followers. Shared engagement math."""
    interactions = ((post.get("likes", 0) or 0) + (post.get("comments", 0) or 0)
                    + (post.get("shares", 0) or 0) + (post.get("saves", 0) or 0))
    denom = max(post.get("views") or 0, followers or 0, 1)
    return interactions / denom


class Client:
    # --- identity / defaults ---
    name = "base"
    follower_min = 0
    follower_max = 10 ** 9
    target_default = 20
    # over-collect multiple: enrich until qualified >= target * overcollect, then
    # rank and trim to target. 1 = stop at target (Speak); >1 = rank a wider pool.
    overcollect = 1
    out_json_default = "prospects.json"
    out_csv_default = "prospects.csv"

    # --- discovery ---
    queries = []
    tiktok_hashtags = []

    # --- dedup ---
    # static known-handle sources + the growing per-run ledger (rewritten each run)
    seen_files = []
    ledger_file = "master_seen.json"

    # --- optional Google Sheet append (None disables) ---
    sheet_id = None
    sheet_tab = None

    # ---- hooks the core calls ----
    def pre_drop(self, handle):
        """Cheap discovery-time drop (before spending any enrich credits)."""
        return False

    def classify(self, r, posts):
        """Set every field qualify/rank/export will read, from the enriched posts."""
        raise NotImplementedError

    def qualifies(self, r):
        """True if r is a keeper after classify."""
        raise NotImplementedError

    def rank_key(self, r):
        """Sort key for the final ranking (ascending sort — negate for descending)."""
        raise NotImplementedError

    def export_header(self):
        raise NotImplementedError

    def export_row(self, r, run_date):
        raise NotImplementedError

    def criteria(self):
        """Metadata block written into the JSON output."""
        return {"followers": [self.follower_min, self.follower_max],
                "platforms": ["tiktok", "instagram"]}
