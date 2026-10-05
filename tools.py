"""The tools the harness can run, and the JSON that describes them to the model.

Data comes from Open Food Facts (see off_api.py). Every tool returns a JSON
string. Errors come back as {"error": ..., "hint": ...} so the model can recover
or tell the user what went wrong, instead of the harness crashing.
"""

import json
import re

import off_api
import profiles
from off_api import OFFError

# Guardrail (tool input): caps on list sizes, enforced in _valid_list and search_products.
MAX_INGREDIENTS = 25
MAX_CANDIDATES = 8
MAX_PRODUCTS = 5

# Taxonomy categories whose members are made of other ingredients. A sauce can
# carry allergens the database doesn't list (pesto -> pine nuts, parmesan).
COMPOUND_CATEGORIES = {
    "en:sauce", "en:condiment", "en:preparation", "en:dough", "en:coating",
    "en:broth", "en:spread", "en:dressing", "en:pastry", "en:soy-preparation",
}

ALIASES = {
    "dairy": "milk", "lactose": "milk", "casein": "milk", "whey": "milk",
    "egg": "eggs", "peanut": "peanuts", "nuts": "tree_nuts", "tree nuts": "tree_nuts",
    "tree nut": "tree_nuts", "treenuts": "tree_nuts", "soya": "soy", "soybeans": "soy",
    "wheat": "gluten", "celiac": "gluten", "coeliac": "gluten", "shellfish": "crustaceans",
    "crustacean": "crustaceans", "shrimp": "crustaceans", "mollusc": "molluscs", "mollusks": "molluscs",
    "sulfites": "sulphites", "sulfite": "sulphites", "sesame seeds": "sesame",
    "maize": "corn", "coconuts": "coconut", "sunflower seed": "sunflower", "sunflower seeds": "sunflower",
    "red meat": "red_meat", "alpha gal": "red_meat", "mammalian meat": "red_meat",
}

PREP_WORDS = (
    "fresh|freshly|chopped|minced|diced|sliced|grated|shredded|softened|melted|large|small|"
    "medium|finely|roughly|crushed|packed|heaping|cold|warm|room-temperature|organic|to taste|"
    "store-bought|store bought|homemade|jarred|prepared|plain|optional"
)
QUANTITY = re.compile(
    r"^[\d\s/½¼¾⅓⅔.,-]*\s*(cups?|c\.|tbsp|tbs|tablespoons?|tsp|teaspoons?|g|grams?|kg|oz|ounces?|"
    r"lbs?|pounds?|ml|l|liters?|cloves?|pinch(es)?|dash(es)?|cans?|sticks?|handfuls?|slices?)?\s*(of\s+)?\b",
    re.I,
)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _error(message: str, hint: str | None = None) -> str:
    return _json({"error": message, **({"hint": hint} if hint else {})})


def _clean_tag(tag: str) -> str:
    """'en:sesame-seeds' -> 'sesame seeds'. Other languages keep their prefix."""
    return tag[3:].replace("-", " ") if tag.startswith("en:") else tag


def clean_ingredient(raw: str) -> str:
    """'2 tbsp unsalted butter, softened' -> 'unsalted butter'."""
    s = re.sub(r"\([^)]*\)", " ", raw.lower())
    s = s.split(",")[0]
    s = QUANTITY.sub("", s.strip())
    s = re.sub(rf"\b({PREP_WORDS})\b", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -.")


def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("oes"):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


# --- Shared ingredient checking (check_ingredients and find_alternatives) ---


def _resolve(names: list[str]) -> tuple[dict[str, tuple[str, str | None]], str | None]:
    """Map each cleaned name to (taxonomy tag, canonical name if it was rewritten).

    Tries the name as-is and singularized in one batch call; only names that miss
    go to the (rate-limited) suggestion endpoint, one at a time.
    Returns the mapping and an error message if the API stopped answering.
    """
    candidates = {n: list(dict.fromkeys([off_api.to_tag(n), off_api.to_tag(_singular(n))])) for n in names}
    off_api.fetch_nodes([t for tags in candidates.values() for t in tags])

    resolved, error = {}, None
    for name, tags in candidates.items():
        if found := next((t for t in tags if off_api.node_exists(t)), None):
            resolved[name] = (found, None)

    suggested = {}
    # Long descriptive names ('dairy-free nut-free vegan pesto') never match and would
    # spend the suggestion endpoint's small rate limit, so only short names are tried.
    for name in [n for n in names if n not in resolved and len(n.split()) <= 3]:
        try:
            if canonical := off_api.suggest_ingredient(name):
                suggested[name] = canonical
        except OFFError as e:
            error = str(e)
            break
    if suggested:
        off_api.fetch_nodes([off_api.to_tag(c) for c in suggested.values()])
        for name, canonical in suggested.items():
            if off_api.node_exists(tag := off_api.to_tag(canonical)):
                resolved[name] = (tag, canonical)
    return resolved, error


def _check(raw_names: list[str], profile: dict) -> tuple[list[dict], str | None]:
    """Check ingredient names against `profile`. Shared by two tools."""
    cleaned = {raw: clean_ingredient(raw) or raw.lower().strip() for raw in raw_names}
    try:
        resolved, error = _resolve(list(dict.fromkeys(cleaned.values())))
    except OFFError as e:
        return [{"ingredient": raw, "status": "unchecked"} for raw in raw_names], str(e)

    results = []
    for raw, name in cleaned.items():
        entry: dict = {"ingredient": raw}
        if name not in resolved:
            entry["status"] = "unchecked" if error else "not_found"
            results.append(entry)
            continue

        tag, canonical = resolved[name]
        if canonical:
            entry["matched_as"] = canonical
        chain = off_api.ancestors(tag)
        flags = profiles.match_chain(chain, off_api._nodes, off_api.node_name, profile)
        parents = [off_api.node_name(t) for t in chain[1:4]]
        compound = bool(COMPOUND_CATEGORIES & set(chain))

        if flags:
            entry["status"] = "flagged"
            entry["flags"] = flags
        elif parents or any(off_api._nodes.get(t, {}).get("allergens") for t in chain):
            entry["status"] = "no_known_conflict"
        else:
            entry["status"] = "unclassified"
        if parents:
            entry["classified_as"] = " → ".join(parents)
        if compound:
            entry["compound"] = True
        if uncertain := [d for d in profile["diets"] if profiles.diet_status(chain, off_api._nodes, d) == "maybe"]:
            entry["diet_uncertain"] = uncertain
        results.append(entry)
    return results, error


def _profile_or_all(session_id: str) -> tuple[dict, bool]:
    """The session's profile, or every allergen if none is set (so checks still say something)."""
    profile = profiles.get(session_id)
    if not profiles.is_empty(profile):
        return profile, True
    return {"allergies": {k: "moderate" for k in profiles.MAJOR}, "diets": [], "avoid": []}, False


def _valid_list(value, name: str, limit: int) -> list[str] | str:
    """Guardrail (tool input): reject empty or oversized lists with an error the model can act on.

    A cleaned list of strings, or an error JSON string."""
    if isinstance(value, str):
        value = [v for v in re.split(r",|\n", value)]
    if not isinstance(value, list) or not value:
        return _error(f"'{name}' must be a non-empty list of strings.", f'Example: {{"{name}": ["butter", "basil"]}}')
    items = [str(v).strip() for v in value if str(v).strip()]
    if not items:
        return _error(f"'{name}' contained only empty strings.")
    if len(items) > limit:
        return _error(f"Too many items in '{name}' ({len(items)}); the limit is {limit}.", "Split them into several calls.")
    return items


# --- Tools ---


def set_dietary_profile(
    session_id: str,
    allergies: list | None = None,
    diets: list | None = None,
    avoid_ingredients: list | None = None,
    remove: list | None = None,
    clear_all: bool = False,
) -> str:
    """Add to, update, or remove parts of the user's dietary profile."""
    profile = profiles.get(session_id)
    if clear_all:
        profiles.clear(session_id)
        profile = profiles.get(session_id)
    changes, problems = [], []

    for item in allergies or []:
        if isinstance(item, str):
            item = {"allergen": item}
        if not isinstance(item, dict):
            problems.append(f"Ignored allergy entry {item!r}: expected an object with 'allergen' and 'severity'.")
            continue
        raw = str(item.get("allergen", "")).strip().lower().replace("-", " ")
        # Guardrail (tool input): only allergens from the fixed vocabulary can be saved.
        key = raw.replace(" ", "_") if raw.replace(" ", "_") in profiles.ALLERGENS else ALIASES.get(raw)
        if not key:
            problems.append(
                f"'{raw}' is not one of the tracked allergens ({', '.join(profiles.ALLERGENS)}). "
                "If the user reacts to it, add it to avoid_ingredients instead."
            )
            continue
        severity = str(item.get("severity") or "").lower()
        if severity not in profiles.SEVERITIES:
            problems.append(f"No valid severity for {key}; defaulted to 'severe'. Confirm with the user.")
            severity = "severe"
        profile["allergies"][key] = severity
        changes.append(f"{key}: {severity}")
        if raw in ("shellfish", "shell fish"):
            # "Shellfish" covers both families in everyday speech; err on the side of both.
            profile["allergies"]["molluscs"] = severity
            changes.append(f"molluscs: {severity}")
            problems.append(
                "'shellfish' was saved as crustaceans AND molluscs (clams, scallops, squid). "
                "Tell the user, and offer to remove molluscs if only crustaceans are a problem."
            )

    for diet in diets or []:
        d = str(diet).strip().lower()
        if d in profiles.DIETS:
            if d not in profile["diets"]:
                profile["diets"].append(d)
                changes.append(f"diet: {d}")
        elif d.endswith("-free") or d.endswith(" free"):
            problems.append(f"'{d}' is not a diet here. Record it as an allergy (e.g. gluten-free -> gluten) instead.")
        else:
            problems.append(f"Diet '{d}' is not supported. Supported diets: {', '.join(profiles.DIETS)}.")

    for term in avoid_ingredients or []:
        t = clean_ingredient(str(term)) or str(term).strip().lower()
        if t and t not in profile["avoid"]:
            profile["avoid"].append(t)
            changes.append(f"avoid: {t}")

    for term in remove or []:
        t = str(term).strip().lower()
        key = t.replace(" ", "_") if t.replace(" ", "_") in profiles.ALLERGENS else ALIASES.get(t)
        if key and profile["allergies"].pop(key, None):
            changes.append(f"removed {key}")
        elif t in profile["diets"]:
            profile["diets"].remove(t)
            changes.append(f"removed diet {t}")
        elif t in profile["avoid"]:
            profile["avoid"].remove(t)
            changes.append(f"removed avoid {t}")
        else:
            problems.append(f"Nothing named '{t}' in the profile to remove.")

    out = {"profile": profiles.describe(profile), "changes": changes or ["no changes"]}
    if problems:
        out["problems"] = problems
    return _json(out)


def get_dietary_profile(session_id: str) -> str:
    """The user's current dietary profile."""
    profile = profiles.get(session_id)
    out: dict = {"profile": profiles.describe(profile)}
    if profiles.is_empty(profile):
        out["note"] = "No profile yet. Ask the user about allergies (and severity), diets, and foods they avoid."
    return _json(out)


def check_ingredients(session_id: str, ingredients: list | None = None, part_of: str | None = None) -> str:
    """Check ingredients against the profile using the Open Food Facts ingredient taxonomy."""
    items = _valid_list(ingredients, "ingredients", MAX_INGREDIENTS)
    if isinstance(items, str):
        return items
    profile, has_profile = _profile_or_all(session_id)
    results, error = _check(items, profile)

    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r["ingredient"])
    compound = [r["ingredient"] for r in results if r.get("compound")]

    out = {
        "checked_against": profiles.summary_line(profile) if has_profile else "no profile set: listing every major allergen",
        "source": f"model's breakdown of '{part_of}', checked against Open Food Facts" if part_of else "Open Food Facts ingredient taxonomy",
        "results": results,
        "summary": by_status,
    }
    if part_of:
        out["part_of"] = part_of

    steps = []
    if by_status.get("flagged") and has_profile:
        steps.append(f"Flagged by the database: {', '.join(by_status['flagged'])}. Use find_alternatives for each one you need to replace.")
    if compound:
        steps.append(
            f"Compound ingredients ({', '.join(compound)}) can hide allergens the database doesn't list. "
            "Break each into its typical components, including common variants, and call check_ingredients "
            "again with those components and part_of set to the compound's name."
        )
    if by_status.get("unclassified") or by_status.get("not_found"):
        names = by_status.get("unclassified", []) + by_status.get("not_found", [])
        steps.append(
            f"The database has no allergen classification for: {', '.join(names)}. For single whole foods "
            "(salt, water, rice) use your own knowledge. For prepared or multi-ingredient items "
            "(e.g. almond milk, marzipan, hummus) break them down and check the components."
        )
    if rewritten := [f"{r['ingredient']} → {r['matched_as']}" for r in results if r.get("matched_as")]:
        steps.append(
            f"Names were matched to database entries: {'; '.join(rewritten)}. If a match is a different food than "
            "intended (e.g. 'cashew' → 'cashew apples'), ignore that result and retry with a more specific name."
        )
    if by_status.get("no_known_conflict"):
        steps.append(
            "'no_known_conflict' means the database links no profile conflict. It is not a guarantee: if you know "
            "of a conflict it missed, say so and label it as your own knowledge."
        )
    if error:
        steps.append(f"Some ingredients were not checked: {error} Tell the user which ones.")
    if not has_profile:
        steps.append("No dietary profile is set. Ask the user about their allergies before judging what is safe for them.")
    out["next_steps"] = steps
    return _json(out)


def _words(text: str) -> set[str]:
    return {_singular(w) for w in re.findall(r"[a-z0-9]+", re.sub(r"['’]s\b", "", text.lower())) if len(w) > 1}


def _rank(hits: list[dict], query: str, min_score: float = 0.5, brand: str = "") -> list[dict]:
    """Re-rank search hits by how many query words appear in their name and brand.

    The search engine's own ranking is loose ('corn flakes' returns Frosted Flakes
    first, 'vegan pesto' returns vegan pepperoni), so relevance is checked here.
    """
    wanted = _words(query)
    if not wanted:
        return hits
    brand_words = _words(brand)
    scored = []
    for i, h in enumerate(hits):
        brands = h.get("brands") or ""
        brands = " ".join(brands) if isinstance(brands, list) else brands
        name_words = _words(h.get("product_name") or "")
        have = name_words | _words(brands)
        score = len(wanted & have) / len(wanted)
        if score < min_score:
            continue
        brand_miss = bool(brand_words) and not brand_words & have
        # Extra words in the name suggest a different product ('Nutella' vs 'Nutella & Go
        # breadsticks'); a record without an ingredient list is worth two extra words.
        cost = len(name_words - wanted - brand_words) + (0 if profiles.usable_ingredients(h) else 2)
        # Ties go to the most-scanned record: popular records get corrected most.
        scored.append((-score, brand_miss, cost, -(h.get("unique_scans_n") or 0), i, h))
    return [h for *_, h in sorted(scored, key=lambda t: t[:5])]


# Words that describe what a product is free of. The profile filters handle those, and
# product names rarely contain them, so they're dropped if the full query finds too little.
INGREDIENTS_CAP = 3000

FREE_FROM = re.compile(r"\b([a-z]+-free|free|non|no|without|dairy|gluten|nut|nuts|peanut|allergy|allergen|friendly|safe)\b", re.I)
MIN_SEARCH_RESULTS = 3


FREE_FROM_NAME = re.compile(r"free|non[- ]?dairy|vegan|plant[- ]?based|alternative|substitute|imitation", re.I)


def _name_warnings(products: list[dict], profile: dict) -> list[list[dict]]:
    """For each product, allergens its NAME implies that its ingredient list doesn't show.

    Some records are half-entered: 'Ice Cream Sandwiches' whose ingredient list only covers
    the wafer. The name's words are looked up in the same taxonomy (one batch call), and a
    two-word phrase overrides its single words, so 'coconut cream' isn't read as dairy.
    """
    grams_per = []
    for p in products:
        words = re.findall(r"[a-z]+", (p.get("product_name") or p.get("name") or "").lower())
        bigrams = [f"{a} {b}" for a, b in zip(words, words[1:])]
        grams_per.append((words, bigrams))
    tags = {off_api.to_tag(g) for words, bigrams in grams_per for g in [*words, *bigrams] if len(g) > 2}
    try:
        off_api.fetch_nodes(sorted(tags))
    except OFFError:
        return [[] for _ in products]  # an optional check; skip it if the API is busy

    out = []
    for p, (words, bigrams) in zip(products, grams_per):
        found = []
        if not FREE_FROM_NAME.search(p.get("product_name") or p.get("name") or ""):
            covered = set()
            grams = []
            for bg in bigrams:
                if _informative_phrase(bg):
                    grams.append(bg)
                    covered |= set(bg.split())
            grams += [w for w in words if w not in covered and len(w) > 2 and off_api.node_exists(off_api.to_tag(w))]
            seen = set()
            for g in grams:
                for f in profiles.match_chain(off_api.ancestors(off_api.to_tag(g)), off_api._nodes, off_api.node_name, profile):
                    if f["type"] == "allergen" and f["conflict"] not in seen:
                        seen.add(f["conflict"])
                        found.append({"allergen": f["conflict"], "word": g})
        out.append(found)
    return out


PLANT_GROUPS = {"en:nut", "en:cereal", "en:fruit", "en:legume", "en:pulse", "en:seed", "en:vegetable", "en:plant", "en:soya"}


def _informative_phrase(phrase: str) -> bool:
    """Whether a two-word name like 'coconut cream' should override its single words.

    Yes if the taxonomy links it to an allergen, marks it vegan (cocoa butter), or its first
    word is a plant (oat milk). No for entries without that information ('ice cream'), where
    'cream' -> dairy is the better signal.
    """
    tag = off_api.to_tag(phrase)
    if not off_api.node_exists(tag):
        return False
    chain = off_api.ancestors(tag)
    if any(off_api._nodes.get(t, {}).get("allergens") for t in chain):
        return True
    if profiles.diet_status(chain, off_api._nodes, "vegan") == "yes":
        return True
    first = off_api.to_tag(phrase.split()[0])
    return bool(off_api.node_exists(first) and PLANT_GROUPS & set(off_api.ancestors(first)))


def _apply_name_warnings(assessment: dict, warnings: list[dict]) -> list[str]:
    """Turn name/ingredient mismatches into cautions. Returns the allergen keys involved."""
    flagged = []
    for w in warnings:
        label = profiles.ALLERGENS[w["allergen"]]["label"]
        if any(label in c for c in assessment["conflicts"]):
            continue  # the ingredient list already shows it
        assessment["cautions"].append(
            f"the name mentions '{w['word']}' (usually {label}) but the ingredient list on file doesn't: "
            "the record is probably incomplete"
        )
        flagged.append(w["allergen"])
    if flagged and assessment["verdict"] == "no_known_conflict":
        assessment["verdict"] = "caution"
    return flagged


def _product_filters(profile: dict, has_profile: bool) -> list[str]:
    """Search clauses: US products with ingredient lists, minus the profile's label allergens."""
    filters = ['countries_tags:"en:united-states"', 'states_tags:"en:ingredients-completed"']
    if not has_profile:
        return filters
    for key, sev in profile["allergies"].items():
        a = profiles.ALLERGENS[key]
        if not a["off"]:  # not a label allergen: exclude by ingredient category instead
            filters += [f'-ingredients_tags:"{g}"' for g in a["groups"]]
            continue
        filters.append(f'-allergens_tags:"{a["off"]}"')
        if sev == "severe":
            filters.append(f'-traces_tags:"{a["off"]}"')
    for diet in profile["diets"]:
        filters.append(f'-ingredients_analysis_tags:"en:non-{diet}"')
    return filters


def _brand_key(brand: str) -> str:
    """'Made Good • Riverside Foods', 'MadeGood', "Mary’s Gone Crackers" -> 'madegood', 'marysgonecrackers'."""
    first = re.split(r"[,•;/]", brand or "")[0]
    return re.sub(r"[^a-z0-9]", "", re.sub(r"['’]s\b", "s", first.lower()))


def _score_product(p: dict, profile: dict, has_profile: bool) -> tuple[float, list[str]]:
    """Rank a verified product and say why, so the answer can explain the pick from data."""
    score, why = 0.0, []
    if p["verdict"] == "no_known_conflict":
        score += 3
        why.append("no conflicts with your profile" if has_profile else "no major allergens on the label")
    elif p["verdict"] == "caution":
        score += 1
        why.append("caution: " + "; ".join(p["cautions"]))
    else:
        why.append("ingredient list incomplete")
    for label in p["labels"]:
        score += 2 if label["certified"] else 1
        why.append(("certified: " if label["certified"] else "label claims: ") + label["label"])
    severe = [k for k, sev in profile["allergies"].items() if sev == "severe"]
    if severe and not any("may contain" in n for n in p["data_quality"]):
        score += 1
        why.append("'may contain' info on file, none for your allergens")
    elif severe:
        why.append("no 'may contain' info on file")
    if (scans := p.get("_scans") or 0) >= 50:
        score += 0.5
        why.append(f"well-documented record ({scans} scans)")
    return score, why


def search_products(session_id: str, query: str | None = None, max_results: int = MAX_PRODUCTS) -> str:
    """Find store-bought products that fit the profile, ranked with reasons."""
    query = _lucene_safe(str(query or ""))
    if not query:
        return _error("'query' is required: the kind of product, e.g. 'granola' or 'pesto'.")
    try:
        max_results = max(1, min(int(max_results), 8))  # Guardrail (tool input): clamp to 1-8
    except (TypeError, ValueError):
        max_results = MAX_PRODUCTS
    profile, has_profile = _profile_or_all(session_id)
    filters = _product_filters(profile, has_profile)

    def run(q: str) -> list[dict]:
        words = _words(q)
        # Short queries must fully match a name; longer ones may miss a word.
        min_score = 1.0 if len(words) <= 2 else 0.67
        return _rank(off_api.search_products(q, filters, limit=40), q, min_score=min_score)

    try:
        searched_as = query
        hits = run(query)
        relaxed = re.sub(r"\s+", " ", FREE_FROM.sub(" ", query)).strip()
        if len(hits) < MIN_SEARCH_RESULTS and relaxed and relaxed != query:
            searched_as, hits = relaxed, hits + run(relaxed)
    except OFFError as e:
        return _error(str(e), "Tell the user product search is unavailable; don't name products from memory instead.")

    products, seen, excluded = [], set(), 0
    for h in hits:
        name = (h.get("product_name") or "").strip()
        brand = ", ".join(h.get("brands") or []) if isinstance(h.get("brands"), list) else (h.get("brands") or "")
        key = (frozenset(_words(name)), _brand_key(brand))
        if not name or key in seen:
            continue
        seen.add(key)
        # The search filters only see label allergens; verify each hit's parsed ingredient list too.
        assessment = profiles.evaluate_product(h, profile)
        if assessment["verdict"] == "conflict":
            excluded += 1
            continue
        products.append({"name": name, "brand": brand, "barcode": h.get("code"), "_scans": h.get("unique_scans_n"), **assessment})

    suspect = 0
    if has_profile:
        kept = []
        for p, warnings in zip(products, _name_warnings(products, profile)):
            flagged = _apply_name_warnings(p, warnings)
            if any(profile["allergies"].get(a) == "severe" for a in flagged):
                suspect += 1  # too risky to recommend for a severe allergy
                continue
            kept.append(p)
        products = kept

    for p in products:
        p["_score"], p["why_ranked"] = _score_product(p, profile, has_profile)
    products.sort(key=lambda p: -p["_score"])  # stable: equal scores keep search relevance order
    # At most two per brand first, so a shopping list isn't five flavors of one product.
    picked, per_brand = [], {}
    for p in products:
        b = _brand_key(p["brand"])
        if per_brand.get(b, 0) < 2:
            picked.append(p)
            per_brand[b] = per_brand.get(b, 0) + 1
    picked += [p for p in products if p not in picked]
    results = []
    for i, p in enumerate(picked[:max_results], 1):
        results.append({"rank": i, **{k: v for k, v in p.items() if not k.startswith("_")}})

    out: dict = {
        "query": query,
        "checked_against": profiles.summary_line(profile) if has_profile else "no profile set: results not filtered",
        "results": results,
    }
    if searched_as != query:
        out["searched_as"] = searched_as
    notes = []
    if excluded:
        notes.append(f"{excluded} more matching product(s) were dropped because their ingredient lists conflict with the profile.")
    if suspect:
        notes.append(
            f"{suspect} product(s) were dropped because their name suggests one of the user's severe allergens "
            "(e.g. 'ice cream') but their ingredient list on file doesn't mention it, so the record is probably incomplete."
        )
    if not results:
        notes.append(f"No US products matching '{query}' passed the profile check. Try a broader product type.")
    else:
        notes.append(
            "Results are ranked in code; explain the top pick using its why_ranked reasons. Only describe "
            "certifications, facilities, or ingredients that appear in these results. The search index can lag "
            "behind the product database, so confirm your top pick with lookup_product (its barcode) before "
            "recommending it."
        )
    if not has_profile:
        notes.append("No dietary profile is set, so results weren't filtered. Ask the user about their allergies.")
    out["notes"] = notes
    return _json(out)


def find_alternatives(
    session_id: str,
    ingredient: str | None = None,
    candidates: list | None = None,
    purpose: str | None = None,
) -> str:
    """Verify proposed substitute ingredients against the profile."""
    ingredient = str(ingredient or "").strip()
    if not ingredient:
        return _error("'ingredient' is required: the ingredient being replaced, e.g. 'butter'.")
    items = _valid_list(candidates, "candidates", MAX_CANDIDATES)
    if isinstance(items, str):
        return _error(
            "Provide 'candidates': 2-6 substitutes you would suggest for this purpose, e.g. "
            "['olive oil', 'vegan butter', 'coconut oil']. This tool verifies them.",
        )
    profile, has_profile = _profile_or_all(session_id)
    if not has_profile:
        return _error(
            "No dietary profile is set, so substitutes can't be checked against anything.",
            "Ask the user for their allergies and call set_dietary_profile first.",
        )

    results, error = _check([ingredient] + items, profile)
    original, checked = results[0], results[1:]
    order = {"no_known_conflict": 0, "unclassified": 1, "not_found": 2, "unchecked": 3, "flagged": 4}
    checked.sort(key=lambda r: order.get(r["status"], 5))

    out = {
        "replacing": ingredient,
        "purpose": purpose,
        "why_replace": original.get("flags") or f"database status: {original['status']}",
        "usable": [r for r in checked if r["status"] == "no_known_conflict" and not r.get("compound")],
        "verify_further": [r for r in checked if r["status"] != "flagged" and (r["status"] != "no_known_conflict" or r.get("compound"))],
        "rejected": [r for r in checked if r["status"] == "flagged"],
    }
    notes = [
        "Only recommend 'usable' candidates, or 'verify_further' ones after breaking them down with check_ingredients. "
        "Never recommend a 'rejected' candidate. If the best substitute is a store-bought product, use search_products."
    ]
    if error:
        notes.append(f"Some candidates were not checked: {error}")
    out["notes"] = notes
    return _json(out)


def _lucene_safe(text: str) -> str:
    """Guardrail (tool input): strip search-query syntax from model-written text."""
    text = re.sub(r"['’]s\b", "", text)  # Kellogg's -> Kellogg
    return re.sub(r'[+\-!(){}\[\]^"~*?:\\/&|\'’,.]', " ", text).strip()


def lookup_product(session_id: str, barcode: str | None = None, name: str | None = None, brand: str | None = None) -> str:
    """Look up a packaged food by barcode or name and assess it against the profile."""
    barcode = re.sub(r"\D", "", str(barcode or ""))
    name = str(name or "").strip()
    if not barcode and not name:
        return _error("Provide a 'barcode' (digits from the package) or a product 'name'.", "e.g. name='Corn Flakes', brand=\"Kellogg's\"")
    # Guardrail (tool input): barcodes are digits only and 8-14 long.
    if barcode and not 8 <= len(barcode) <= 14:
        return _error(f"'{barcode}' is not a valid barcode: barcodes have 8-14 digits.", "Ask the user to re-check, or search by name.")

    others, fallback_note = [], None
    try:
        if barcode:
            product = off_api.get_product(barcode)
            if not product:
                return _error(
                    f"Barcode {barcode} is not in Open Food Facts.",
                    "Search by product name instead, or ask the user to read the ingredients from the package.",
                )
        else:
            query = _lucene_safe(f"{name} {brand or ''}")
            raw = off_api.search_products(query, ['countries_tags:"en:united-states"'], limit=20)
            # Every word of the product name must match; the brand only breaks ties.
            hits = _rank(raw, _lucene_safe(name), min_score=1.0, brand=_lucene_safe(brand or ""))
            if not hits:
                raw = raw or off_api.search_products(query, limit=20)
                hits = _rank(raw, _lucene_safe(name), min_score=1.0, brand=_lucene_safe(brand or ""))
            if not hits:
                close = [
                    {"name": h.get("product_name"), "brand": h.get("brands"), "barcode": h.get("code")}
                    for h in _rank(raw, query, min_score=0.5)[:4]
                ]
                return _error(
                    f"No product exactly matching '{query}' in Open Food Facts.",
                    (f"Closest records: {_json(close)}. Ask the user if one of these is it; don't assume. "
                     if close else "") + "Otherwise try a shorter name or the barcode from the package.",
                )
            best = hits[0]
            product = off_api.get_product(best["code"]) or best
            if not profiles.usable_ingredients(product):
                # The US record is often a stub; another country's record may have the ingredient list.
                world = _rank(off_api.search_products(query, limit=20), _lucene_safe(name), min_score=1.0,
                              brand=_lucene_safe(brand or ""))
                if fuller := next((h for h in world if profiles.usable_ingredients(h)), None):
                    product = off_api.get_product(fuller["code"]) or fuller
                    fallback_note = (
                        "The US record had no ingredient list, so this is a record from another market. "
                        "Recipes can differ by country; say so and suggest checking the package."
                    )
            others = [
                {"name": h.get("product_name"), "brand": h.get("brands"), "barcode": h.get("code")}
                for h in hits[1:4] if h.get("product_name")
            ]
    except OFFError as e:
        return _error(str(e), "Tell the user the product database is unavailable; don't guess the ingredients.")

    profile, has_profile = _profile_or_all(session_id)
    assessment = profiles.evaluate_product(product, profile)
    if has_profile:
        _apply_name_warnings(assessment, _name_warnings([product], profile)[0])
    ingredients = (product.get("ingredients_text_en") or product.get("ingredients_text") or "").strip()
    # Guardrail (tool output): cap crowd-sourced label text so one record can't flood the context.
    # Keep the start and the end, where "contains" and "may contain" statements usually are.
    if len(ingredients) > INGREDIENTS_CAP:
        ingredients = f"{ingredients[:2500]} [... {len(ingredients) - 3000} characters omitted ...] {ingredients[-500:]}"
    brands = product.get("brands")
    out = {
        "product": {
            "name": product.get("product_name_en") or product.get("product_name"),
            "brand": ", ".join(brands) if isinstance(brands, list) else brands,
            "quantity": product.get("quantity"),
            "barcode": product.get("code"),
            "url": f"https://world.openfoodfacts.org/product/{product.get('code')}",
        },
        "checked_against": profiles.summary_line(profile) if has_profile else "no profile set: listing every major allergen",
        **assessment,
        "label_allergens": [_clean_tag(t) for t in product.get("allergens_tags") or []],
        "may_contain": [_clean_tag(t) for t in product.get("traces_tags") or []],
        "ingredients_text": ingredients or None,
        "next_steps": [
            "Read the ingredient list yourself for allergen sources the tags can miss (e.g. malt = barley = gluten, "
            "casein/whey = milk, albumin = egg) and call anything you find 'my reading of the ingredient list'."
            if ingredients else
            "There is no ingredient list for this product, so you can't read the label. Tell the user that, "
            "don't describe its ingredients from memory, and suggest they read the package or give the barcode.",
        ],
    }
    if fallback_note:
        out["data_quality"].insert(0, fallback_note)
    if not barcode:
        out["match_note"] = f"Best search match for '{name}'. If it's the wrong product, use a barcode or one of other_matches."
        out["other_matches"] = others
    if not has_profile:
        out["next_steps"].append("No dietary profile is set. Ask the user about their allergies before judging safety.")
    return _json(out)


# --- What the model sees ---

ALLERGEN_KEYS = list(profiles.ALLERGENS)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_dietary_profile",
            "description": (
                "Save the user's allergies, diets, and foods they avoid. Call it whenever the user states or changes "
                "them. It MERGES into the existing profile: entries you don't mention are kept. Use 'remove' to drop "
                "entries and 'clear_all' only when the user asks to start over."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "allergies": {
                        "type": "array",
                        "description": "Allergies to add or update. Map the user's words to an allergen: celiac -> gluten, dairy/lactose -> milk, alpha-gal -> red_meat. If the user says 'shellfish', pass allergen 'shellfish' and the tool saves both crustaceans and molluscs.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "allergen": {"type": "string", "enum": [*ALLERGEN_KEYS, "shellfish"]},
                                "severity": {
                                    "type": "string",
                                    "enum": profiles.SEVERITIES,
                                    "description": "'severe': even traces / 'may contain' are a problem (anaphylaxis, celiac). 'moderate': avoid as an ingredient, traces are tolerable.",
                                },
                            },
                            "required": ["allergen", "severity"],
                        },
                    },
                    "diets": {"type": "array", "items": {"type": "string", "enum": profiles.DIETS}, "description": "Diets to add."},
                    "avoid_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Other specific foods to avoid that aren't in the allergen list, e.g. ['pork', 'cilantro', 'corn'].",
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Allergens, diets, or avoided foods to remove, e.g. ['eggs', 'vegan'].",
                    },
                    "clear_all": {"type": "boolean", "description": "Erase the whole profile before applying the other fields."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dietary_profile",
            "description": "Read the user's saved allergies (with severity), diets, and avoided foods. Use when the user asks what's in their profile.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_ingredients",
            "description": (
                "Check a list of ingredients against the user's profile using the Open Food Facts ingredient database, "
                "which traces each ingredient to its allergen family (ghee -> butterfat -> dairy -> milk). Use it on every "
                "recipe's ingredient list BEFORE suggesting changes, and again on your modified list BEFORE presenting it. "
                "Results: 'flagged' (conflicts with the profile), 'no_known_conflict', 'unclassified' (database has no "
                "allergen data), 'not_found', 'unchecked' (API error); 'compound': true marks sauces and other prepared "
                "items that may hide more allergens."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Plain ingredient names, one per item, without amounts: ['butter', 'pesto', 'parmesan', 'pine nuts']. Max 25.",
                    },
                    "part_of": {
                        "type": "string",
                        "description": "Set when these ingredients are YOUR breakdown of a compound ingredient, e.g. 'pesto'. Leave out for the recipe's own ingredients.",
                    },
                },
                "required": ["ingredients"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_alternatives",
            "description": (
                "Verify substitute INGREDIENTS for one problem ingredient in a recipe. YOU propose the candidates "
                "(good substitutes for its role in the dish); the tool checks each against the profile and the "
                "ingredient database and rejects any that conflict, e.g. cashew cream for a tree-nut allergy. "
                "For store-bought products, use search_products instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ingredient": {"type": "string", "description": "The ingredient being replaced, e.g. 'butter'."},
                    "candidates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-6 plain substitute names you would suggest, e.g. ['olive oil', 'vegan butter', 'coconut oil'].",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "What the ingredient does in this dish, e.g. 'fat for browning', 'binder in baking', 'creamy sauce base'.",
                    },
                },
                "required": ["ingredient", "candidates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Find real store-bought US products of one kind that fit the user's profile. Use it for every "
                "shopping request ('find me a granola', 'a pesto I can buy', 'what crackers can I get'). Never "
                "name products from memory. It excludes products whose label or ingredient list conflicts with "
                "the profile, and returns them ranked in code, with reasons (why_ranked), certification labels, "
                "and data-quality notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The product type, e.g. 'granola', 'pesto', 'crackers'. Don't add 'nut-free' etc.; the profile is applied automatically.",
                    },
                    "max_results": {"type": "integer", "description": "How many products to return, 1-8. Default 5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_product",
            "description": (
                "Look up ONE specific packaged food the user named, by barcode or by name, and check it against the "
                "profile: label allergens, 'may contain' warnings, free-from/certification labels, and the parsed "
                "ingredient list. Returns a verdict ('conflict', 'caution', 'no_known_conflict', 'insufficient_data'), "
                "data-quality notes, and the raw ingredient text. Use it whenever the user asks about a specific brand "
                "or product; don't answer from memory. To find products to buy, use search_products instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "barcode": {"type": "string", "description": "UPC/EAN digits from the package, e.g. '016000275287'. Most precise; prefer it if the user gave one."},
                    "name": {"type": "string", "description": "Product name, e.g. 'Corn Flakes'. Used when there is no barcode."},
                    "brand": {"type": "string", "description": "Brand, e.g. \"Kellogg's\". Improves name matches."},
                },
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "set_dietary_profile": set_dietary_profile,
    "get_dietary_profile": get_dietary_profile,
    "check_ingredients": check_ingredients,
    "find_alternatives": find_alternatives,
    "search_products": search_products,
    "lookup_product": lookup_product,
}


def run_tool(name: str, args: dict, session_id: str) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop.

    session_id comes from the harness, never from the model, so one user can't
    read or change another's profile.
    """
    # Guardrail (tool input): only registered tools run.
    if name not in TOOL_MAP:
        return _error(f"Unknown tool '{name}'.", f"Available: {list(TOOL_MAP)}")
    # Guardrail (tool input): the session comes from the harness, never the model.
    args = {k: v for k, v in (args or {}).items() if k != "session_id"}
    try:
        return TOOL_MAP[name](session_id=session_id, **args)
    except TypeError as e:
        return _error(f"Bad arguments for {name}: {e}", "Check the parameter names in the tool definition.")
    except OFFError as e:
        return _error(str(e))
    except Exception as e:  # a bug in a tool shouldn't take down the chat
        return _error(f"{name} failed unexpectedly: {type(e).__name__}: {e}")
