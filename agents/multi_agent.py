import os
import sys
import json
from datetime import datetime
from ollama import chat

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rag.retrieval import search_news
from rag.refresh import refresh_index

MODEL = "qwen2.5:7b"

# ---------------------------------------------------------------------------
# RESEARCHER AGENT - the only one allowed to call the search tool.
# Its single responsibility: find candidates, then judge which ones are
# genuinely relevant. Nothing it discards ever reaches the Writer.
# ---------------------------------------------------------------------------

RESEARCHER_SYSTEM_PROMPT = """You are a news researcher. Your task: use the search_news tool
to find news articles related to the user's topic. If the topic is broad (e.g. "sports",
"technology"), a single generic search query often misses articles that are clearly on-topic
but don't literally contain that word (e.g. a specific match preview won't contain the word
"sports" itself). In that case, call search_news MULTIPLE TIMES with different, more specific
sub-queries (e.g. for "sports": "football match", "basketball", "league standings", relevant
team names) to maximize coverage. Once you believe you've covered the topic well, stop calling
the tool. After you're done searching, you will separately be asked to judge which of the
results found so far are GENUINELY relevant to the topic."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "Runs a semantic search against a vector database of current Turkish news. "
                "Returns the most semantically relevant articles, including source and link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The topic to search for"},
                    "n_results": {"type": "integer", "description": "How many results to retrieve (aim high, e.g. 20-30, for broad topic coverage)"},
                },
                "required": ["query"],
            },
        },
    }
]


MIN_N_RESULTS = 20  # enforced floor - don't rely on the model picking a large-enough number


def call_tool(tool_name, tool_input):
    if tool_name == "search_news":
        query = tool_input["query"]
        requested = tool_input.get("n_results", MIN_N_RESULTS)
        n_results = max(requested, MIN_N_RESULTS)  # the model's choice can only raise this, not lower it
        return search_news(query, n_results)
    return {"error": f"Unknown tool: {tool_name}"}


def research(user_question: str, max_steps: int = 5):
    """
    Step A: lets the model call search_news (ReAct loop, same as Faz 3).
    Step B: asks the model, in a SEPARATE call with forced JSON output, to
    judge which of the raw results are genuinely relevant.
    Returns a clean list of vetted findings (dicts) - this is the "state"
    handed off to the Writer agent.
    """
    messages = [
        {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_question},
    ]

    raw_results = []

    for step in range(max_steps):
        print(f"\n[RESEARCHER] Step {step + 1}: sending request to {MODEL}")
        response = chat(model=MODEL, messages=messages, tools=TOOLS)
        message = response["message"]

        if not message.get("tool_calls"):
            # Model didn't call the tool at all (rare) - nothing to research
            print("[RESEARCHER] Model did not call the search tool.")
            break

        messages.append(message)

        for tool_call in message["tool_calls"]:
            tool_name = tool_call["function"]["name"]
            tool_input = tool_call["function"]["arguments"]
            print(f"[RESEARCHER] [TOOL_USE] {tool_name}({tool_input})")

            result = call_tool(tool_name, tool_input)
            raw_results.extend(result)  # keep accumulating across every search round
            messages.append({"role": "tool", "content": json.dumps(result, ensure_ascii=False)})

        # NOTE: no forced break here anymore - the loop goes back to the top,
        # lets the model see its own search history, and decide on its own
        # whether to search again (e.g. with a different sub-query) or stop.
        # The "if not message.get('tool_calls')" check above is what ends it.

    if not raw_results:
        return []

    # The same article can come back from more than one sub-query - dedupe by link
    # before handing results to the filtering step, so we don't judge/count it twice.
    seen_links = set()
    deduped_results = []
    for item in raw_results:
        if item["link"] not in seen_links:
            seen_links.add(item["link"])
            deduped_results.append(item)
    raw_results = deduped_results

    # --- Step B: explicit, structured-output filtering, done in small BATCHES ---
    #
    # Asking a 7B model to judge 30+ articles in one pass tends to suffer from
    # "lost in the middle": it reliably evaluates the first few items and
    # trails off on the rest, rather than genuinely weighing each one. Instead,
    # we split raw_results into fixed-size batches and filter each separately -
    # the same principle as RAG chunking, applied here to the filtering task.
    FILTER_BATCH_SIZE = 10
    findings = []

    batches = [raw_results[i:i + FILTER_BATCH_SIZE] for i in range(0, len(raw_results), FILTER_BATCH_SIZE)]
    print(f"\n[RESEARCHER] Filtering {len(raw_results)} candidate(s) in {len(batches)} batch(es) of up to {FILTER_BATCH_SIZE}...")

    for batch_num, batch in enumerate(batches, start=1):
        # Local indices (0..len(batch)-1) within this batch only - simpler for
        # the model to reason about than global indices, and we map them back
        # to the actual item right after parsing.
        slim_batch = [
            {
                "index": i,
                "source": item["source"],
                "title": item["title"],
                "text": item["text"][:200],
                "link": item["link"],
            }
            for i, item in enumerate(batch)
        ]

        filter_prompt = f"""User's topic: "{user_question}"

Below are news articles retrieved by search (each with an "index"). Select ALL of the ones
that are GENUINELY relevant to the topic - if several articles are about the same general
subject, include EACH of them separately, not just one representative example. For each
selected one, return its "index", and a short "reason" explaining why it's relevant. Do not
include irrelevant ones at all. The "title" and "text" fields are in Turkish - do not translate
them, we will look them up by index afterwards.

IMPORTANT CONSISTENCY CHECK: before finalizing your answer, re-read each "reason" you wrote.
If a reason says the article is NOT about the topic, or describes a different/unrelated
subject, you MUST remove that item from "findings" - never include an item whose own stated
reason contradicts relevance.

Articles:
{json.dumps(slim_batch, ensure_ascii=False, indent=2)}

Return ONLY JSON in this exact format, nothing else:
{{"findings": [{{"index": 0, "reason": "..."}}]}}
"""

        print(f"[RESEARCHER] Filtering batch {batch_num}/{len(batches)} ({len(batch)} item(s))...")
        filter_response = chat(
            model=MODEL,
            messages=[{"role": "user", "content": filter_prompt}],
            format="json",
            options={"num_ctx": 8192},
        )

        raw_output = filter_response["message"]["content"]

        try:
            parsed = json.loads(raw_output)
            for entry in parsed.get("findings", []):
                idx = entry.get("index")
                if idx is not None and 0 <= idx < len(batch):
                    item = dict(batch[idx])
                    item["reason"] = entry.get("reason", "")
                    findings.append(item)
        except (json.JSONDecodeError, AttributeError, TypeError):
            print(f"[RESEARCHER] Could not parse batch {batch_num}'s output, skipping this batch.")

    print(f"[RESEARCHER] {len(raw_results)} raw result(s) -> {len(findings)} judged relevant (across {len(batches)} batch(es))")
    return findings


# ---------------------------------------------------------------------------
# WRITER AGENT - never sees raw search results, only the vetted findings
# handed off by the Researcher. Its job is purely synthesis, not judgment.
# ---------------------------------------------------------------------------

WRITER_SYSTEM_PROMPT = """You are a news editor. Using the list of PRE-VETTED, relevance-approved
articles given to you, write a summary IN TURKISH that answers the user's question.

STRICT RULES, follow these for EVERY SINGLE article in the list, with no exceptions even when
the list is long:
1. Write one separate paragraph or bullet per article - never merge multiple articles into one
   sentence without each getting its own link.
2. Every single item MUST end with its own markdown link AND its publish date/time, in this
   EXACT form: [Kaynak adı](link) - tarih: TARIH_DEGERI - copy both the link and the "tarih"
   value exactly as given, never omit either, never invent your own date.
3. Do NOT state any specific number, score, date, or quote unless that exact number/quote
   literally appears in the article's "text" field you were given. If the "text" doesn't contain
   a specific figure, describe the event in general terms instead of guessing a number.
4. Do not invent any information beyond what's in the list. If the list is empty, say so clearly,
   in Turkish."""


def write(user_question: str, findings: list[dict]) -> str:
    if not findings:
        return "Bu konuyla ilgili yeterince alakalı haber bulunamadı."

    # Build a clean, human-readable date/time string ourselves (deterministic,
    # code-level formatting) rather than asking the model to parse/format the
    # messy raw RSS date string on its own - same philosophy as the link fix:
    # don't rely on the model for something we can just hand it pre-formatted.
    def format_date(ts):
        if not ts:
            return "tarih bilinmiyor"
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

    # Drop fields the writer doesn't need (distance is meaningless here, and
    # dropping it avoids a stray "None" value confusing the model) - keep the
    # prompt focused on exactly what it should use.
    clean_findings = [
        {
            **{k: v for k, v in item.items() if k not in ("distance", "published", "published_ts")},
            "tarih": format_date(item.get("published_ts")),
        }
        for item in findings
    ]

    user_message = f"""User's question: "{user_question}"

Pre-vetted, relevant articles you can use (fields are in Turkish, keep them as-is).
There are {len(clean_findings)} articles below - make sure your answer includes a line for
EVERY one of them, each ending with its own [Kaynak adı](link) - tarih: TARIH_DEGERI,
using the "tarih" field already provided for each item:

{json.dumps(clean_findings, ensure_ascii=False, indent=2)}

Write your answer in Turkish, following the strict rules from the system prompt.
"""

    print("\n[WRITER] Synthesizing final answer from vetted findings...")
    response = chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        options={"num_ctx": 8192},
    )
    return response["message"]["content"]


# ---------------------------------------------------------------------------
# ORCHESTRATION - wires the two agents together with a plain dict/list as
# the handoff ("state"). No framework needed for two sequential steps.
# ---------------------------------------------------------------------------

def run_pipeline(user_question: str):
    refresh_index()

    print("\n================ RESEARCHER PHASE ================")
    findings = research(user_question)

    print("\n================ WRITER PHASE ================")
    final_answer = write(user_question, findings)

    print("\n--- FINAL ANSWER ---")
    print(final_answer)
    return final_answer


if __name__ == "__main__":
    run_pipeline("bana spor haberlerini ver")