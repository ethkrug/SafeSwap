# SafeSwap: an allergy-aware kitchen agent

SafeSwap helps people with food allergies **check packaged foods** and **adapt recipes they love**, using real
product labels from [Open Food Facts](https://world.openfoodfacts.org) instead of memory, and it shows its work
under each answer. It's for anyone who's wondered in a grocery aisle whether "malt flavoring" means gluten.

## What it does that a plain chatbot doesn't

- **Reads real labels.** Product lookups (by name or database barcode) return the actual ingredient list, label allergens, and
  "may contain" warnings, plus the raw ingredient text for sources the tags miss
  (Corn Flakes: no gluten tag, but "malt flavor" is barley).
- **Checks in code, not memory.** Every ingredient is matched to your profile through the database's
  ingredient categories (ghee → butterfat → milkfat → *milk*), including the parts of compound items
  like pesto or soy sauce, which the model breaks down for checking.
- **Checks its own work.** Substitutes are checked before they're suggested, and adapted recipes
  are checked again before you see them. Severe allergies treat "may contain" as a conflict.
- **Cites its sources.** Each fact is marked *(database)* or *(my knowledge)*, and the UI shows every tool call.

## Sample queries

Run these in order in one session. The first sets the profile, and the next three use it from memory:

0. **Set the profile:**
   `I have a severe peanut allergy and celiac disease, and I'm lactose intolerant (moderate). Set this as my profile.`
   *Expect:* `set_dietary_profile` saves peanuts (severe), gluten (severe), and milk (moderate); the sidebar updates.

1. **Compare real products:**
   `Which is better for me, Doritos Nacho Cheese or Lay's Classic chips?`
   *Expect:* two `lookup_product` calls. The database flags Doritos for milk (cheddar, whey, buttermilk) and
   finds no conflict in Lay's Classic (potatoes, vegetable oil, salt), so the agent picks Lay's.

2. **Adapt a recipe:**
   `Make me a version of this I can eat: Chicken stir-fry: 1 lb chicken breast, 3 tbsp soy sauce, 2 tbsp peanut oil, 1 tbsp butter, 2 cups broccoli, 1/4 cup chopped peanuts, 2 cups cooked rice.`
   *Expect:* `check_ingredients` flags peanut oil, peanuts, and butter, and marks soy sauce as compound. The
   agent breaks it down (wheat), checks substitutes with `find_alternatives`, re-checks the adapted recipe,
   then writes it.

3. **Shop for a product:**
   `Find me a store-bought granola bar that works for me.`
   *Expect:* `search_products` excludes conflicting products, ranks the rest, and the agent confirms its top
   pick with `lookup_product`. It recommends MadeGood Chocolate Chip Granola Bars and explains why from the
   data: GFCO certified gluten-free, a "no nuts" label, and no milk in the ingredient list.

More things to try: `Check barcode 044000032029` · `What can I use instead of butter in chocolate chip cookies?` ·
`Find me a store-bought granola without tree nuts` (then: `Why did you pick that one?`)

## Tools

| Tool | What it does | Data |
|---|---|---|
| `set_dietary_profile` / `get_dietary_profile` | Saves allergies (with severity), diets, and avoided foods for the session, mapping terms like "celiac" → gluten. Covers the 14 major allergens plus corn, coconut, sunflower seeds, and red meat. | session state |
| `check_ingredients` | **The core original tool.** Follows each ingredient's full ancestor chain in the taxonomy and matches it against the profile, returning `flagged` (with the chain that triggered it), `no_known_conflict`, or why it couldn't be checked, and marks compound items for breakdown. | Open Food Facts taxonomy API + suggestions API |
| `find_alternatives` | **Original.** Checks the model's proposed substitutes against the profile and sorts them into `usable` / `verify_further` / `rejected` (e.g. it rejects cashew cream for a tree-nut allergy). | Open Food Facts taxonomy API |
| `search_products` | **Original.** Finds US products of one kind (`"granola"`) that fit the profile, re-checks each result's ingredient list, and ranks them in code (certified free-from labels first) with a `why_ranked` reason for each. | Open Food Facts search API |
| `lookup_product` | Looks up one product by barcode or name and returns a verdict (`conflict` / `caution` / `no_known_conflict` / `insufficient_data`) from label allergens, "may contain" warnings, and the ingredient list, flagging data-quality problems. | Open Food Facts product + search API |

## Handling unreliable data

Tools never raise errors; they return messages the model can act on (e.g. `"'123' is not a valid barcode:
barcodes have 8-14 digits. Ask the user to re-check, or search by name."`). Open Food Facts is crowd-sourced
and rate-limited (~15 requests / 20 s), so:
- `off_api.py` caches, paces requests, and retries 429s; anything left unchecked is reported, not guessed.
- Name search prefers records with real ingredient lists and requires every word to match.
- Missing taxonomy ancestors are fetched until each chain is complete.
- Corn, coconut, sunflower, and red meat aren't label allergens there, so they're matched on ingredient text.

## Project layout

```
app.py         FastAPI server, agent loop (LiteLLM → Gemini), session store, system prompt
tools.py       Tool functions, their JSON schemas, and run_tool()
profiles.py    Allergen vocabulary, per-session profiles, ingredient/product matching
off_api.py     Open Food Facts client: caching, pacing, retries, model-readable errors
index.html     Frontend: profile sidebar, quick setup, example prompts, tool trace cards
```

`/chat` keeps the starter's response shape; the page uses `/chat/stream` (server-sent events) to show tool calls live.

## Run locally

1. A GCP project with billing and the Vertex AI API enabled.
2. `gcloud auth application-default login`
3. `uv run app.py`, then open http://localhost:8000

## Deploy

Cloud Run with continuous deployment from GitHub (Developer Connect, buildpack), entrypoint:

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Sessions and profiles live in memory, so they reset when Cloud Run starts a new instance.

## Limitations

SafeSwap is a helper, not a medical device. Open Food Facts can be wrong, incomplete, or out of date, and
recipes differ by country. Always read the package, especially for severe allergies.
