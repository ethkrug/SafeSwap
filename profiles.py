"""Dietary profiles, and matching ingredients and products against them.

The allergen vocabulary maps each profile allergen to the ids Open Food Facts
uses for it. This is not a list of foods: which foods contain an allergen comes
from the taxonomy (butter -> dairy -> milk). The `groups` are taxonomy nodes that
define the allergen family, a backstop for nodes whose allergen link is missing.
"""

import re

ALLERGENS = {
    "milk":        {"label": "milk / dairy", "off": "en:milk",          "groups": ["en:dairy", "en:milk", "en:milk-proteins"]},
    "eggs":        {"label": "eggs",         "off": "en:eggs",          "groups": ["en:egg"]},
    "peanuts":     {"label": "peanuts",      "off": "en:peanuts",       "groups": ["en:peanut"]},
    "tree_nuts":   {"label": "tree nuts",    "off": "en:nuts",          "groups": ["en:tree-nut"]},
    "soy":         {"label": "soy",          "off": "en:soybeans",      "groups": ["en:soya", "en:soya-bean"]},
    "gluten":      {"label": "gluten",       "off": "en:gluten",        "groups": ["en:wheat", "en:barley", "en:rye", "en:cereals-containing-gluten"]},
    "fish":        {"label": "fish",         "off": "en:fish",          "groups": ["en:fish"]},
    "crustaceans": {"label": "crustacean shellfish", "off": "en:crustaceans", "groups": ["en:crustacean"]},
    "molluscs":    {"label": "molluscs",     "off": "en:molluscs",      "groups": ["en:mollusc"]},
    "sesame":      {"label": "sesame",       "off": "en:sesame-seeds",  "groups": ["en:sesame"]},
    "mustard":     {"label": "mustard",      "off": "en:mustard",       "groups": ["en:mustard"]},
    "celery":      {"label": "celery",       "off": "en:celery",        "groups": ["en:celery"]},
    "lupin":       {"label": "lupin",        "off": "en:lupin",         "groups": []},
    "sulphites":   {"label": "sulphites",    "off": "en:sulphur-dioxide-and-sulphites", "groups": []},
    # Not label allergens in Open Food Facts ("off": None), so they're matched only
    # through ingredient categories. The groups list the categories the taxonomy
    # doesn't link back to the food (corn syrup isn't under corn there).
    "corn":        {"label": "corn",         "off": None, "groups": ["en:corn", "en:corn-starch", "en:corn-syrup", "en:corn-oil", "en:corn-maltodextrin"]},
    "coconut":     {"label": "coconut",      "off": None, "groups": ["en:coconut", "en:coconut-oil", "en:coconut-sugar"]},
    "sunflower":   {"label": "sunflower seeds", "off": None, "groups": ["en:sunflower", "en:sunflower-seed", "en:sunflower-oil", "en:sunflower-lecithin"]},
    "red_meat":    {"label": "red meat (alpha-gal)", "off": None, "groups": ["en:beef", "en:pork", "en:lamb", "en:mutton", "en:goat", "en:venison"]},
}
MAJOR = [k for k, a in ALLERGENS.items() if a["off"]]  # what a check covers when no profile is set
OFF_TO_KEY = {a["off"]: k for k, a in ALLERGENS.items() if a["off"]}
SEVERITIES = ["severe", "moderate"]
DIETS = ["vegan", "vegetarian"]

# session_id -> profile. In-memory, like the session store in app.py.
_profiles: dict[str, dict] = {}


def empty_profile() -> dict:
    return {"allergies": {}, "diets": [], "avoid": []}


def get(session_id: str) -> dict:
    return _profiles.setdefault(session_id, empty_profile())


def clear(session_id: str):
    _profiles.pop(session_id, None)


def is_empty(profile: dict) -> bool:
    return not (profile["allergies"] or profile["diets"] or profile["avoid"])


def describe(profile: dict) -> dict:
    """The profile as the model and the frontend see it."""
    return {
        "allergies": [
            {"allergen": k, "label": ALLERGENS[k]["label"], "severity": sev}
            for k, sev in profile["allergies"].items()
        ],
        "diets": profile["diets"],
        "avoid_ingredients": profile["avoid"],
    }


def summary_line(profile: dict) -> str:
    """One line for the system prompt."""
    if is_empty(profile):
        return "No dietary profile set yet."
    parts = [f"{ALLERGENS[k]['label']} ({sev})" for k, sev in profile["allergies"].items()]
    if profile["diets"]:
        parts.append("diet: " + ", ".join(profile["diets"]))
    if profile["avoid"]:
        parts.append("avoids: " + ", ".join(profile["avoid"]))
    return "; ".join(parts)


# --- Matching an ingredient's taxonomy chain ---


def match_chain(chain: list[str], nodes: dict[str, dict], name_of, profile: dict) -> list[dict]:
    """Conflicts between one ingredient's ancestor chain and the profile.

    chain: the ingredient tag followed by its ancestors, e.g.
           ['en:ghee', 'en:butterfat', 'en:milkfat', 'en:dairy'].
    Each conflict says which node triggered it, so the answer can explain
    "ghee -> dairy -> milk" instead of just "milk".
    """
    conflicts = []
    seen = set()

    def add(kind, key, via, detail):
        if (kind, key) in seen:
            return
        seen.add((kind, key))
        conflicts.append({"type": kind, "conflict": key, "via": " → ".join(via), "detail": detail})

    for i, tag in enumerate(chain):
        via = [name_of(t) for t in chain[: i + 1]]
        node = nodes.get(tag, {})
        off_allergen = (node.get("allergens") or {}).get("en")
        for key, sev in profile["allergies"].items():
            a = ALLERGENS[key]
            if (a["off"] and off_allergen == a["off"]) or tag in a["groups"]:
                add("allergen", key, via, f"contains {a['label']} ({sev} allergy)")
        for term in profile["avoid"]:
            if term_matches(term, tag, name_of(tag)):
                add("avoid", term, via, f"user avoids {term}")
    for diet in profile["diets"]:
        if diet_status(chain, nodes, diet) == "no":
            add("diet", diet, [name_of(t) for t in chain[:1]], f"not {diet}")
    return conflicts


def diet_status(chain: list[str], nodes: dict[str, dict], diet: str) -> str | None:
    """'yes' / 'no' / 'maybe' from the nearest node that says, as Open Food Facts inherits it."""
    for tag in chain:
        if status := (nodes.get(tag, {}).get(diet) or {}).get("en"):
            return status
    return None


def term_matches(term: str, tag: str, name: str) -> bool:
    """Whole-word match: 'pork' matches 'pork belly' and 'smoked pork', not 'porcini'."""
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
    return bool(slug) and (f"-{slug}-" in f"-{tag.removeprefix('en:')}-" or slug == re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))


# --- Package labels ---

# Free-from labels as Open Food Facts tags them, per profile entry. Claims are the
# maker's own; certifications are third-party (GFCO, NSF, ...), which matters for celiac.
LABEL_CLAIMS = {
    "gluten": ["en:no-gluten"],
    "tree_nuts": ["en:no-nuts"],
    "peanuts": ["en:no-peanuts", "en:no-nuts"],
    "milk": ["en:no-milk", "en:no-lactose", "en:non-dairy", "en:without-addition-of-dairy-products"],
    "eggs": ["en:no-eggs"],
    "soy": ["en:no-soy"],
    "sesame": ["en:no-sesame"],
    "vegan": ["en:vegan"],
    "vegetarian": ["en:vegetarian", "en:vegan"],
}


def profile_labels(p: dict, profile: dict) -> list[dict]:
    """The product's free-from and diet labels that are relevant to this profile."""
    tags = set(p.get("labels_tags") or [])
    out = []
    for key in [*profile["allergies"], *profile["diets"]]:
        certified = [t for t in tags if key == "gluten" and t.endswith("gluten-free")]
        claims = [t for t in LABEL_CLAIMS.get(key, []) if t in tags]
        if key in ("vegan", "vegetarian"):
            certified = [t for t in tags if t.startswith(f"en:{key}-")]  # e.g. en:vegan-action
        if certified:
            names = sorted(t[3:].replace("-", " ") for t in certified)
            out.append({"for": key, "label": " / ".join(names), "certified": True})
        elif claims:
            out.append({"for": key, "label": claims[0][3:].replace("-", " "), "certified": False})
    return out


# --- Matching a product record ---


def usable_ingredients(p: dict) -> bool:
    """Whether a record's ingredient list is real. Some records marked 'completed'
    hold junk like 'nutella serving suggestion ferrero', so count recognized
    ingredients instead of trusting the flag."""
    n, unknown = p.get("ingredients_n"), p.get("unknown_ingredients_n")
    if isinstance(n, (int, float)) and isinstance(unknown, (int, float)):
        return n - unknown >= 2
    return len(p.get("ingredients_tags") or []) >= 2


def evaluate_product(p: dict, profile: dict) -> dict:
    """Judge one Open Food Facts product against the profile.

    Uses three database signals: the label allergens, the 'may contain' traces,
    and the parsed ingredient list (ingredients_tags, already expanded to
    ancestors by Open Food Facts), which catches allergens the label tags missed.
    """
    allergens = set(p.get("allergens_tags") or [])
    traces = set(p.get("traces_tags") or [])
    ing_tags = set(p.get("ingredients_tags") or [])
    analysis = set(p.get("ingredients_analysis_tags") or [])
    states = set(p.get("states_tags") or [])
    has_ingredients = usable_ingredients(p)

    conflicts, cautions = [], []
    for key, sev in profile["allergies"].items():
        a = ALLERGENS[key]
        if a["off"] in allergens:
            conflicts.append(f"contains {a['label']} (label allergens)")
        elif hits := sorted(ing_tags & set(a["groups"])):
            conflicts.append(f"contains {a['label']} (ingredient list: {', '.join(h[3:] for h in hits)})")
        elif not a["off"] and (hits := sorted({t for t in ing_tags for g in a["groups"] if term_matches(g[3:], t, "")})):
            # No label-allergen tag to rely on, and products often keep the label's wording
            # ("en:stone-ground-corn"), so match the food word in the ingredient tags.
            conflicts.append(f"contains {a['label']} (ingredient list: {', '.join(h[3:] for h in hits)})")
        elif a["off"] in traces:
            msg = f"may contain {a['label']} (cross-contact warning)"
            # Severe allergies treat cross-contact as a conflict; moderate as a caution.
            (conflicts if sev == "severe" else cautions).append(msg)

    for diet in profile["diets"]:
        if f"en:non-{diet}" in analysis:
            conflicts.append(f"not {diet}")
        elif f"en:{diet}" not in analysis:
            cautions.append(f"{diet} status uncertain in database")

    text = (p.get("ingredients_text_en") or p.get("ingredients_text") or "").lower()
    for term in profile["avoid"]:
        if any(term_matches(term, t, t[3:].replace("-", " ")) for t in ing_tags) or re.search(rf"\b{re.escape(term.lower())}\b", text):
            conflicts.append(f"contains {term} (user avoids)")

    notes = []
    if not has_ingredients:
        notes.append("No usable ingredient list on file: allergens cannot be verified from the database.")
    elif "en:ingredients-completed" not in states:
        notes.append("Ingredient list marked incomplete by contributors.")
    if has_ingredients and not traces:
        notes.append("No 'may contain' info on file. That means unknown, not none; check the package.")
    if p.get("ingredients_text") and not p.get("ingredients_text_en") and p.get("lang") not in (None, "en"):
        notes.append(f"Ingredient list is in '{p.get('lang')}', not English; read it carefully.")
    if (t := p.get("last_modified_t")) and t < 1_700_000_000:  # before Nov 2023
        notes.append("Record not updated since before 2024; the recipe may have changed.")

    if conflicts:
        verdict = "conflict"
    elif not has_ingredients:
        verdict = "insufficient_data"
    elif cautions:
        verdict = "caution"
    else:
        verdict = "no_known_conflict"
    return {
        "verdict": verdict,
        "conflicts": conflicts,
        "cautions": cautions,
        "labels": profile_labels(p, profile),
        "data_quality": notes,
    }
