import json
import queue
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import profiles
from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = """\
You are SafeSwap, an allergy-aware kitchen assistant. You help people with food \
allergies and diets check packaged foods and adapt recipes. You check real data \
with your tools instead of answering from memory.

THE USER'S PROFILE (saved by set_dietary_profile, refreshed every turn):
{profile}

PROFILE
- When the user mentions an allergy, diet, or food they avoid, call set_dietary_profile right away.
- Severity: 'severe' means even traces or 'may contain' are a problem (anaphylaxis, celiac); \
'moderate' means avoid it as an ingredient. If the user doesn't say, save it as severe and ask them to confirm.
- If there's no profile and the user asks whether something is OK for them, ask about their allergies first.

PACKAGED PRODUCTS
- For a specific product the user names, call lookup_product. Never state a product's ingredients from memory.
- Also read its ingredient list yourself for sources the tags miss: malt / malt flavoring / barley / \
rye / spelt -> gluten; casein, whey, lactose, ghee, butterfat -> milk; albumin, lysozyme -> egg; \
anchovies -> fish. Call anything you find this way "my reading of the ingredient list".
- If the match looks like a different product, say so and offer the other matches.

SHOPPING
- When the user wants something to buy ("find me a granola", "a pesto I can buy"), call search_products \
with the product type. Never name products or brands from memory, not even to look them up.
- Results are already ranked in code. Confirm the top pick with lookup_product (its barcode) before \
recommending it, since search data can be out of date; if it now conflicts, move to the next one. Explain \
the pick with its ranking reasons (e.g. "certified gluten-free label, no conflicts with your profile").
- Only mention a certification, facility, or ingredient if it appears in the tool results. If the user \
asks about something the data doesn't cover (e.g. dedicated nut-free facilities), say the database doesn't \
record it.
- In recipes, if the best substitute is store-bought (e.g. a nut-free pesto), use search_products for it.

ADAPTING A RECIPE (follow these steps in order)
1. List the recipe's ingredients as plain names and call check_ingredients once with all of them.
2. For every result with compound: true, plus prepared or multi-ingredient items that are \
unclassified or not_found (e.g. pesto, mayonnaise, almond milk, Worcestershire sauce), break the item \
into its typical components, including common variants (pesto: basil, pine nuts, parmesan, olive oil, \
garlic; some versions use walnuts or cashews). Then call check_ingredients with part_of set to the item's name.
3. Every flagged ingredient must be replaced or removed, whatever the severity: never keep a flagged \
ingredient, not even as "optional". For each one, including ones found inside compounds, call \
find_alternatives with 2-5 candidates that fit its role in this dish. Only use 'usable' candidates, or \
'verify_further' ones you have broken down and checked. If nothing can replace it, remove it and say so.
4. Write the adapted recipe.
5. Call check_ingredients on the adapted recipe's full ingredient list, listing the components of any \
compound you kept (the dressing's ingredients, not just "Caesar dressing"). If anything is flagged, fix it and check again.
6. Answer.
Make independent tool calls in the same turn (e.g. several breakdowns at once).

HOW TO ANSWER
- Lead with the direct answer to what they asked:
  - One product ("Is X OK for me?"): whether it fits and why. "Ritz Crackers: conflict, they contain wheat flour (gluten)."
  - Comparison ("X or Y?"): which one fits better and why, then each product's result.
  - Recipe: what conflicted in the original and why, as a short "What changed" list (original -> swap, and why), \
then the adapted recipe with amounts and steps.
  - Substitute ("What can I use instead of X?"): the best substitutes for that use and how to use them, \
then any candidates that were rejected and why.
  - Shopping ("Find me a..."): the top pick and why it fits, then other options if useful.
  - Something else: answer the question directly.
- Never mention tool names or data field names (no "ingredients_text", "lookup_product", "why_ranked"). \
Say "the product database", "the ingredient list on the label", "the ingredient database".

WHERE FACTS COME FROM
- Label every fact. "(database)": what a tool returned (flagged / no_known_conflict ingredients, product \
data, labels, ranking reasons). "(my knowledge)": anything you add yourself. "(not checked)": anything a \
tool returned as unchecked or failed on. Put each label right after the claim it covers, never one label \
at the start of a bullet for everything after it. A conflict you found by reading the ingredient list \
yourself is "(my reading of the ingredient list)", not "(database)".
- General food knowledge is welcome, labeled "(my knowledge)": how ingredients behave in cooking, common \
substitutes and ratios, what a dish usually contains, general cross-contact risks. Example: "chocolate \
chips are often made on shared equipment with milk and nuts (my knowledge)".
- About a SPECIFIC product or brand, say only what the tools returned. Don't add its nutrition, fat or \
water content, taste, texture, baking behavior, recipe, facility, or manufacturer statements from memory, \
not even labeled. Saying what a brand is "formulated" or "designed" to do counts. Example of what NOT to write: "Wegmans plant-based sticks match butter's fat-to-water \
ratio". If it's useful, say it generally instead: "stick-form plant butters usually bake more like butter \
than tubs (my knowledge)".
- Don't name brands or products that no tool returned.
- Never call something "safe" or "guaranteed", including in headings and recipe titles. Say "no conflicts found with your profile", and always say what it covers ("for this product", \
"in the adapted recipe"), never as a sentence on its own. Don't add a \
generic "check the package" line at the end; the app adds that reminder itself.
- If a tool fails or returns unchecked items, say exactly what couldn't be checked. Don't fill the gap with guesses.
- Stay on food, ingredients, recipes, and allergies. For an allergic reaction, tell the user to contact a \
doctor or emergency services.
- Be concise. Use markdown lists and bold text where they help.
"""
# Guardrail (agent loop): stop runaway tool loops. A recipe adaptation takes 5-6 rounds when all goes right.
MAX_TOOL_ROUNDS = 10


NO_ANSWER = "Sorry, I couldn't come up with an answer. Could you rephrase that?"
CHECKING_TOOLS = {"check_ingredients", "find_alternatives", "lookup_product", "search_products"}


def add_safety_reminder(response: str, tool_calls: list[dict], session_id: str) -> str:
    """Guardrail (agent output): appends text, never blocks.

    Add the cross-contact reminder for severe allergies in code, so it is always there and
    names the user's actual severe allergies instead of a generic sentence."""
    profile = profiles.get(session_id)
    severe = [profiles.ALLERGENS[k]["label"] for k, sev in profile["allergies"].items() if sev == "severe"]
    checks = [c for c in tool_calls if c["name"] in CHECKING_TOOLS]
    # Skip it when nothing was actually checked (e.g. the only lookup was an invalid barcode)
    # or there's no answer to attach it to.
    if not severe or not checks or response == NO_ANSWER or all('"error"' in c["result"][:20] for c in checks):
        return response
    names = severe[0] if len(severe) == 1 else ", ".join(severe[:-1]) + " and " + severe[-1]
    return (
        f"{response}\n\n> **Severe allergy reminder ({names}):** the database can't see cross-contact. "
        "Check the package's \"may contain\" line before eating."
    )


def system_prompt(session_id: str) -> str:
    profile = profiles.get(session_id)
    return SYSTEM_PROMPT.format(profile=profiles.summary_line(profile))


# --- The Harness ---


def run_agent(messages: list[dict], session_id: str, on_event: Callable[[dict], None] | None = None) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    on_event, if given, receives live progress: "thinking" before each model call,
    then "tool_start" / "tool_end" around each tool (used by /chat/stream).
    """
    emit = on_event or (lambda event: None)
    tool_calls = []
    retried_empty = False

    for _ in range(MAX_TOOL_ROUNDS):
        emit({"type": "thinking"})
        # The profile can change mid-turn (set_dietary_profile), so rebuild the prompt every round.
        messages[0] = {"role": "system", "content": system_prompt(session_id)}
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Guardrail (agent output): never return or store an empty reply.
        if not reply.tool_calls and not (reply.content or "").strip():
            # Gemini occasionally returns an empty turn. Don't store it (a blank answer in the
            # history confuses the next question); just ask the model once more.
            if retried_empty:
                return NO_ANSWER, tool_calls
            retried_empty = True
            continue

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
                result = json.dumps({"error": "Tool arguments were not valid JSON. Resend the call."})
            else:
                emit({"type": "tool_start", "name": call.function.name, "args": args})
                result = run_tool(call.function.name, args, session_id)
            record = {"name": call.function.name, "args": args, "result": result}
            tool_calls += [record]
            emit({"type": "tool_end", **record})

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing. Try asking about fewer things at once.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


def handle_turn(request: ChatRequest, on_event: Callable[[dict], None] | None = None) -> ChatResponse:
    """One user turn, shared by /chat and /chat/stream."""
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": system_prompt(session_id)}]
    history = sessions[session_id]
    turn_start = len(history)

    # Append user's message to the context
    history += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(history, session_id, on_event)
        response = add_safety_reminder(response or "", tool_calls, session_id)
    except Exception as e:
        # Roll the history back to before this turn: a turn that died mid-loop can leave a
        # tool request with no result, which breaks the next call. The user can just resend.
        del history[turn_start:]
        if isinstance(e, litellm.RateLimitError):
            response = "The AI model is busy right now (rate limit). Please wait a few seconds and send that again."
        else:
            # Auth, billing, a model that is not running: show it in the chat, not as a 500.
            response = f"Something went wrong: {type(e).__name__}: {str(e)[:300]}"
        tool_calls = []

    return ChatResponse(response=response or "", session_id=session_id, tool_calls=tool_calls)


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    return handle_turn(request)


KEEPALIVE_SECONDS = 15  # some proxies drop a quiet connection during a long model call


@app.post("/chat/stream")
def chat_stream(request: ChatRequest):
    """Same turn as /chat, sent as server-sent events while it runs.

    Events: thinking, tool_start, tool_end, then exactly one of
    done (the /chat payload: response, session_id, tool_calls) or error.
    """
    events: queue.Queue = queue.Queue()

    def work():
        try:
            result = handle_turn(request, on_event=events.put)
            events.put({"type": "done", **result.model_dump()})
        except Exception as e:  # a bug outside the agent loop; still end the stream cleanly
            events.put({"type": "error", "message": f"Server error: {type(e).__name__}: {str(e)[:200]}"})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            try:
                event = events.get(timeout=KEEPALIVE_SECONDS)
            except queue.Empty:
                yield ": keepalive\n\n"  # an SSE comment; the browser ignores it
                continue
            if event is None:
                return
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # Ask proxies not to buffer, so events reach the browser as they happen.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/profile")
def get_profile(session_id: str | None = None):
    """The session's dietary profile, for the sidebar. Read-only: changes go through the agent."""
    if not session_id:
        return profiles.describe(profiles.empty_profile())
    return profiles.describe(profiles.get(session_id))


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    if session_id:
        profiles.clear(session_id)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
