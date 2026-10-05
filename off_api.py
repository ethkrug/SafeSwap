"""A small client for the Open Food Facts APIs the tools use.

Four endpoints, all free and keyless:
- product by barcode            world.openfoodfacts.org/api/v2/product/{code}
- product search                search.openfoodfacts.org/search (search-a-licious)
- ingredient taxonomy (batch)   world.openfoodfacts.org/api/v2/taxonomy
- ingredient name suggestions   world.openfoodfacts.org/api/v3/taxonomy_suggestions

Open Food Facts rate-limits aggressively: measured at about 15 requests per 20
seconds on world.openfoodfacts.org, recovering within ~5 seconds. So every
response is cached in memory, requests are paced to stay under the limit, and a
429 is retried after a short wait. Failures raise OFFError with a message
written for the model, not for a log.
"""

import re
import threading
import time
from collections import deque
from urllib.parse import urlparse

import requests

WORLD = "https://world.openfoodfacts.org"
SEARCH = "https://search.openfoodfacts.org/search"
# Open Food Facts asks every client to identify itself.
HEADERS = {"User-Agent": "AllergyAwareKitchenAgent/1.0 (class project; contact via GitHub repo)"}
TIMEOUT = 10
CACHE_TTL = 60 * 60  # product data changes slowly; an hour is plenty

PRODUCT_FIELDS = ",".join([
    "code", "product_name", "product_name_en", "brands", "quantity", "lang",
    "ingredients_text", "ingredients_text_en", "ingredients_tags",
    "allergens_tags", "traces_tags", "ingredients_analysis_tags",
    "states_tags", "countries_tags", "last_modified_t",
    "ingredients_n", "unknown_ingredients_n", "unique_scans_n", "labels_tags",
])
SEARCH_FIELDS = ",".join([
    "code", "product_name", "brands", "allergens_tags", "traces_tags",
    "ingredients_tags", "ingredients_analysis_tags", "states_tags",
    "ingredients_n", "unknown_ingredients_n", "unique_scans_n", "labels_tags",
])


class OFFError(Exception):
    """An Open Food Facts call failed. str(e) is safe to show the model."""


_session = requests.Session()
_session.headers.update(HEADERS)
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _cached(key: str):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value):
    with _lock:
        _cache[key] = (time.time(), value)
    return value


# Sliding-window pacing per host, kept just under the measured limit.
RATE_WINDOW = 20.0
RATE_MAX = 14
MAX_PACING_WAIT = 8.0
_recent: dict[str, deque] = {}
_rate_lock = threading.Lock()


def _pace(host: str):
    """Block until a request to `host` fits in the window (or MAX_PACING_WAIT passes)."""
    deadline = time.time() + MAX_PACING_WAIT
    while True:
        with _rate_lock:
            q = _recent.setdefault(host, deque())
            now = time.time()
            while q and now - q[0] > RATE_WINDOW:
                q.popleft()
            # Product reads are documented at 100/min; the taxonomy endpoints are much stricter.
            limit = RATE_MAX * 2 if host.endswith(":product") else RATE_MAX
            if len(q) < limit or now >= deadline:
                q.append(now)
                return
            wait = RATE_WINDOW - (now - q[0]) + 0.05
        time.sleep(min(wait, max(0.0, deadline - time.time()), 1.0))


def _get_json(url: str, params: dict, what: str) -> dict:
    """GET with pacing, plus retries on rate limits and server errors."""
    # Product reads have their own, larger limit (100/min); keep them out of the taxonomy bucket.
    host = urlparse(url).netloc + (":product" if "/api/v2/product/" in url else "")
    attempts = 3
    for attempt in range(attempts):
        _pace(host)
        try:
            r = _session.get(url, params=params, timeout=TIMEOUT)
        except requests.Timeout:
            if attempt < attempts - 1:
                continue
            raise OFFError(f"Open Food Facts timed out during {what}. Try again in a moment.")
        except requests.RequestException as e:
            raise OFFError(f"Could not reach Open Food Facts during {what}: {type(e).__name__}.")

        if r.status_code in (429, 502, 503, 504):
            if attempt < attempts - 1:
                time.sleep(3 * (attempt + 1))  # the limit clears within ~5s
                continue
            if r.status_code == 429:
                raise OFFError(
                    f"Open Food Facts is rate-limiting requests ({what}). "
                    "Wait ~30 seconds before retrying; tell the user what could not be checked."
                )
            raise OFFError(f"Open Food Facts is temporarily unavailable ({what}, HTTP {r.status_code}).")
        if r.status_code >= 400:
            raise OFFError(f"Open Food Facts rejected the request ({what}, HTTP {r.status_code}).")
        try:
            return r.json()
        except ValueError:
            # Outages return an HTML maintenance page with a 200.
            raise OFFError(f"Open Food Facts returned a non-JSON page during {what}; it may be down.")
    raise OFFError(f"Open Food Facts failed during {what}.")


# --- Products ---


def get_product(barcode: str) -> dict | None:
    """Full product record, or None if the barcode isn't in the database."""
    key = f"product:{barcode}"
    if (hit := _cached(key)) is not None:
        return hit or None
    data = _get_json(f"{WORLD}/api/v2/product/{barcode}.json", {"fields": PRODUCT_FIELDS}, "product lookup")
    product = data.get("product") if data.get("status") == 1 else None
    _store(key, product or {})
    return product


def search_products(query: str, filters: list[str] | None = None, limit: int = 10) -> list[dict]:
    """Search products by text. `filters` are Lucene clauses ANDed onto the query.

    Words are ANDed one by one: search-a-licious returns nothing for a
    parenthesized group or a bare multi-word phrase combined with filters.
    """
    words = [w for w in query.split() if len(w) > 1]
    if not words:
        return []
    q = " AND ".join([*words, *(filters or [])])
    key = f"search:{q}:{limit}"
    if (hit := _cached(key)) is not None:
        return hit
    data = _get_json(SEARCH, {"q": q, "page_size": limit, "fields": SEARCH_FIELDS, "langs": "en"}, "product search")
    return _store(key, data.get("hits") or [])


# --- Ingredient taxonomy ---

# tag -> taxonomy node ({} when the tag doesn't exist). Shared across sessions.
_nodes: dict[str, dict] = {}


def to_tag(name: str) -> str:
    """'Pine nuts' -> 'en:pine-nuts', the taxonomy's id format."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"en:{slug}"


def fetch_nodes(tags: list[str]) -> dict[str, dict]:
    """Taxonomy nodes for `tags` and all of their ancestors.

    The API's include_parents flag sometimes omits an ancestor (en:tree-nut, for
    one), so missing parents are fetched in follow-up rounds until the chain closes.
    """
    wanted = set(tags)
    for _ in range(4):
        missing = sorted(t for t in wanted if t not in _nodes)
        if not missing:
            break
        data = _get_json(
            f"{WORLD}/api/v2/taxonomy",
            {
                "tagtype": "ingredients",
                "tags": ",".join(missing),
                "fields": "name,parents,allergens,vegan,vegetarian",
                "include_parents": 1,
            },
            "ingredient lookup",
        )
        for tag, node in data.items():
            _nodes[tag] = node or {}
        for tag in missing:  # requested but absent from the reply: doesn't exist
            _nodes.setdefault(tag, {})
        # Walk the full chain through what's cached so far; anything unfetched goes in the next round.
        wanted = {a for t in wanted for a in ancestors(t)}
    return {t: _nodes[t] for t in wanted if t in _nodes}


def ancestors(tag: str) -> list[str]:
    """The tag followed by every ancestor already in the node cache, nearest first."""
    out, queue = [], [tag]
    while queue:
        t = queue.pop(0)
        if t in out:
            continue
        out.append(t)
        queue += _nodes.get(t, {}).get("parents", [])
    return out


def node_exists(tag: str) -> bool:
    return bool(_nodes.get(tag))


def node_name(tag: str) -> str:
    return (_nodes.get(tag, {}).get("name") or {}).get("en") or tag.removeprefix("en:").replace("-", " ")


def suggest_ingredient(text: str) -> str | None:
    """Map free text to the taxonomy's canonical name ('parmesan' -> 'parmigiano reggiano')."""
    key = f"suggest:{text.lower()}"
    if (hit := _cached(key)) is not None:
        return hit or None
    data = _get_json(
        f"{WORLD}/api/v3/taxonomy_suggestions",
        {"tagtype": "ingredients", "lc": "en", "string": text, "limit": 1},
        "ingredient name matching",
    )
    suggestions = data.get("suggestions") or []
    return _store(key, suggestions[0] if suggestions else "") or None
