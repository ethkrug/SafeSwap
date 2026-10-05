# SafeSwap: an allergy-aware kitchen agent

SafeSwap helps people with food allergies and diets **check packaged foods** and **adapt recipes they love**.
It doesn't answer from memory. It looks up real product labels and traces every ingredient to its
allergen family in the [Open Food Facts](https://world.openfoodfacts.org) database, then shows its work
under each answer.

**Who it's for:** anyone who has stood in a grocery aisle wondering whether "malt flavoring" means gluten,
or wanted to cook a recipe that's full of things they can't eat.

## What it does that a plain chatbot doesn't

- **Reads real labels.** Ask about a product by name or barcode and SafeSwap pulls its actual ingredient
  list, label allergens, and "may contain" warnings. It also reads the raw ingredient text for sources the
  database tags miss (Kellogg's Corn Flakes: the database shows no gluten, but "malt flavor" is barley).
- **Checks every ingredient in code, every time.** Your allergy profile is applied to each ingredient
  by following the database's ingredient categories (ghee → butterfat → milkfat → dairy → *milk*), not by the
  model remembering what you said 20 messages ago.
- **Breaks down compound ingredients.** Sauces and other prepared items (pesto, soy sauce) are marked
  compound. The model breaks them into components, and those are checked against the database too.
- **Shops from real data.** "Find me a granola" searches the product database, drops anything that conflicts,
  and ranks the rest in code. Certifications like "GFCO gluten-free" come from the product's labels, not the
  model's memory.
- **Checks its own work.** Substitutes are checked before they're suggested (it rejects "cashew cream" for a
  tree-nut allergy), and the final adapted recipe is checked again before you see it.
- **Says where each fact came from.** Answers mark facts as *(database)* or *(my knowledge)*. The UI labels
  every check as "database" or "model breakdown · database check."
- **Severity-aware.** A *severe* allergy treats "may contain" cross-contact warnings as conflicts; a *moderate*
  one treats them as cautions.

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
   agent breaks soy sauce down (it finds wheat), uses `find_alternatives` to check substitutes (e.g. canola oil,
   olive oil, pumpkin seeds, tamari, which comes back as "verify further" because it's a compound sauce, so the agent breaks it down too),
   runs a final check on the adapted recipe, then writes it.

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
| `set_dietary_profile` / `get_dietary_profile` | Saves allergies (with severity), diets, and avoided foods for the session. Merges updates, maps "celiac" → gluten and "shellfish" → crustaceans (and asks about molluscs), and reports anything it can't save. Tracks the 14 major allergens plus corn, coconut, sunflower seeds, and red meat (alpha-gal); anything else goes in the free-text avoid list. | session state |
| `check_ingredients` | **The core original tool.** Resolves each ingredient in the Open Food Facts taxonomy, follows its full ancestor chain, and matches it against the profile. Returns `flagged` (with the chain that triggered it), `no_known_conflict`, `unclassified`, `not_found`, or `unchecked`, and marks compound items for breakdown. A `part_of` argument records when the components are the model's breakdown of a compound ingredient. | Open Food Facts taxonomy API + suggestions API |
| `find_alternatives` | **Original.** The model proposes substitute ingredients for an ingredient's role in the dish; the tool checks each one against the profile and the ingredient database and sorts them into `usable` / `verify_further` / `rejected` (e.g. it rejects cashew cream for a tree-nut allergy). | Open Food Facts taxonomy API |
| `search_products` | **Original.** Finds real US store-bought products of one kind (`"granola"`) that fit the profile. It excludes label allergens in the search (plus "may contain" warnings for severe allergies), then re-checks every result's parsed ingredient list, because the search filters let through products with no allergen data. Results are ranked in code (no conflicts, then certified free-from labels like GFCO gluten-free, then label claims, then whether "may contain" info is on file, then record quality), at most two per brand, and each comes with a `why_ranked` reason, so the agent explains its pick from data rather than memory. | Open Food Facts search API |
| `lookup_product` | Looks up one specific packaged food by barcode or name and returns a verdict (`conflict` / `caution` / `no_known_conflict` / `insufficient_data`) using three signals: label allergens, "may contain" warnings, and the parsed ingredient list, plus the free-from and certification labels relevant to the profile. It also reports data-quality problems (no ingredient list, non-English text, a stale record, missing "may contain" info). | Open Food Facts product + search API |

### Error handling

Every tool returns an error the model can act on instead of raising one. For example: `"'123' is not a valid barcode:
barcodes have 8-14 digits. Ask the user to re-check, or search by name."` Open Food Facts rate-limits heavily
(measured at ~15 requests / 20 s), so `off_api.py` caches responses, paces requests just under the limit,
and retries 429s. If a check still fails, the result says exactly which ingredients went unchecked, and the
agent is told to say so rather than guess.

### Notes on the data (found while building)

Open Food Facts is crowd-sourced, so the tools are built around its weak spots:
- Many products have several records. Name search prefers records with a real ingredient list
  (counting *recognized* ingredients, since some "completed" records contain junk) and every word of the
  product name must match, so "vegan pesto" can't turn into vegan pepperoni.
- The taxonomy API sometimes leaves out an ancestor (e.g. `en:tree-nut`), so missing parents are fetched
  until the chain is complete.
- Compound ingredients (pesto, mayonnaise, almond milk) have no allergen data. The tool says so instead of
  reporting them as clear, and the model breaks them down.
- If any allergy is severe, the server adds a reminder that names those allergies, because the database
  can't see cross-contact. It's added in code rather than left to the model.
- Corn, coconut, sunflower, and red meat aren't label allergens in Open Food Facts, so products are matched on
  the words in their ingredient list instead ("stone ground corn"), and there's no "may contain" data for them.
- The database records free-from labels and certifications, but not manufacturing facilities, so the agent
  is told to say so rather than guess.

## Project layout

```
app.py         FastAPI server, agent loop (LiteLLM → Gemini), session store, system prompt
tools.py       Tool functions, their JSON schemas, and run_tool()
profiles.py    Allergen vocabulary, per-session profiles, ingredient/product matching
off_api.py     Open Food Facts client: caching, pacing, retries, model-readable errors
index.html     Frontend: profile sidebar, quick setup, example prompts, tool trace cards
```

`/chat` keeps the starter's response shape (`response`, `session_id`, `tool_calls` with `name`, `args`,
`result`). The web page uses `/chat/stream` instead: the same turn, sent as server-sent events
(`thinking`, `tool_start`, `tool_end`, then `done` with the `/chat` payload, or `error`), so the page shows
each tool call live as it runs. `GET /profile?session_id=…` feeds the sidebar; `POST /clear` resets a session and its profile.

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
