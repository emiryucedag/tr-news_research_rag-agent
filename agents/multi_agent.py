import os
import sys
import json
import re
from datetime import datetime
from ollama import chat

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from observability.tracer import get_client, observe
from rag.retrieval import search_news
from rag.refresh import refresh_index

tracer = get_client()

MODEL = "qwen2.5:7b"

# Settings for the JUDGING calls (relevance filter, refine). Ollama's default
# temperature is 0.8, which makes the model SAMPLE from its probability distribution:
# the same 38 candidates produced 11 relevant picks in one run and 14 in the next.
# For a yes/no judgment we want the same answer every time, so we use greedy
# decoding (temperature 0) plus a fixed seed. The writer keeps the default on purpose:
# some variety in wording is harmless there.
JUDGE_OPTIONS = {"num_ctx": 8192, "temperature": 0, "seed": 42}


# ---------------------------------------------------------------------------
# TRACING HELPERS
# Every model call in this file goes through traced_chat(), so each one shows up
# in the trace as its own "generation": the exact prompt sent, the raw output,
# token counts, and latency. Everything else (agents, tool calls, retrieval,
# checks) is wrapped with @observe, and nesting follows the call hierarchy.
# The tracer lives in observability/tracer.py; read traces with show_trace.py.
# ---------------------------------------------------------------------------

def _plain(obj):
    """Ollama returns pydantic Message objects; the tracer stores plain JSON-able data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)  # drop the empty (null) fields
    return obj


def traced_chat(name, **kwargs):
    """Drop-in replacement for ollama.chat() that records the call as a "generation" span."""
    options = kwargs.get("options") or {}
    with tracer.start_as_current_observation(
        as_type="generation",
        name=name,
        model=kwargs.get("model"),
        input=[_plain(m) for m in kwargs.get("messages", [])],
        model_parameters={
            "num_ctx": options.get("num_ctx", 0),
            "temperature": options.get("temperature", "default"),
            "json_mode": kwargs.get("format") == "json",
        },
    ) as generation:
        response = chat(**kwargs)

        total_s = (response.get("total_duration") or 0) / 1e9
        load_s = (response.get("load_duration") or 0) / 1e9
        generation.update(
            output=_plain(response["message"]),
            usage_details={
                "input_tokens": response.get("prompt_eval_count") or 0,
                "output_tokens": response.get("eval_count") or 0,
            },
            metadata={
                "ollama_total_seconds": round(total_s, 2),
                "ollama_model_load_seconds": round(load_s, 2),
            },
        )
    return response

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


@observe(as_type="retriever", name="vector-search")
def retrieve(query, n_results):
    """Thin wrapper so the vector search appears as its own 'retriever' step inside the tool call."""
    return search_news(query, n_results)


@observe(as_type="tool", name="tool-call")
def call_tool(tool_name, tool_input):
    if tool_name == "search_news":
        query = tool_input["query"]
        requested = tool_input.get("n_results", MIN_N_RESULTS)
        n_results = max(requested, MIN_N_RESULTS)  # the model's choice can only raise this, not lower it
        return retrieve(query, n_results)
    return {"error": f"Unknown tool: {tool_name}"}


@observe(name="relevance-filter")
def filter_candidates(user_question: str, raw_results: list[dict]) -> list[dict]:
    """Step B of the researcher: judge which retrieved candidates are genuinely relevant."""
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
        filter_response = traced_chat(
            f"relevance-filter-batch-{batch_num}",
            model=MODEL,
            messages=[{"role": "user", "content": filter_prompt}],
            format="json",
            options=JUDGE_OPTIONS,
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
    tracer.update_current_span(
        metadata={"candidates": len(raw_results), "judged_relevant": len(findings), "batches": len(batches)}
    )
    return findings


@observe(as_type="agent", name="researcher")
def research(user_question: str, max_steps: int = 1):
    """
    Step A: lets the model call search_news. max_steps is the number of model<->tool
    rounds; the default is ONE round (see the note inside the loop for why).
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
        response = traced_chat(f"researcher-step-{step + 1}", model=MODEL, messages=messages, tools=TOOLS)
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

        # With max_steps=1 the loop ends right here, after the tools ran, without asking
        # the model again. We used to let it go round once more so it could issue
        # follow-up searches, but traces showed that second model call (~30s) never
        # produced a new tool call: its answer was thrown away every single time.
        # Raise max_steps to bring follow-up rounds back. (The model can still request
        # several searches in ONE response - message["tool_calls"] is a list.)

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

    return filter_candidates(user_question, raw_results)


# ---------------------------------------------------------------------------
# WRITER AGENT - never sees raw search results, only the vetted findings
# handed off by the Researcher. Its job is purely synthesis, not judgment.
# ---------------------------------------------------------------------------

WRITER_SYSTEM_PROMPT = """You are a news editor. Using the list of PRE-VETTED, relevance-approved
articles given to you, write a summary IN TURKISH that answers the user's question.

STRICT RULES, follow these for EVERY SINGLE article in the list, with NO EXCEPTIONS - these
apply even when the list is long, and even when the user later asks you to revise your answer
(a revision changes WHAT you focus on, never whether these rules apply):
1. Write one separate paragraph or bullet per article - never merge multiple articles into one
   sentence without each getting its own link.
2. EVERY item MUST include, in this order: (a) one sentence in your own words describing what
   the article actually says, (b) its markdown link, (c) its publish date/time, in this exact
   form: <your one-sentence description> [Kaynak adı](link) - tarih: TARIH_DEGERI. Never produce
   a bare link with no description, even if the user's feedback asked you to narrow the topic -
   narrowing the topic means including fewer articles, never stripping the description from the
   ones you do include.
3. Do NOT state any specific number, score, date, or quote unless that exact number/quote
   literally appears in the article's "text" field you were given. If "text" doesn't contain a
   specific figure, describe the event in general terms instead of guessing a number.
4. CRITICAL - never invent a result for a match/event that hasn't happened yet. If the article's
   "text" describes an upcoming or scheduled match (words like "hazırlık maçı", "oynanacak",
   "maçı öncesi", a future date, broadcast/watch information) rather than one that has already
   concluded, you MUST only say that the match is scheduled (and when/where to watch, if given) -
   NEVER state or guess a score or winner for it. Only report a result if "text" explicitly
   describes the match as already finished (e.g. "kazandı", "mağlup oldu", "yendi", a final score
   given in the text itself).
5. Do not invent any information beyond what's in the list. If the list is empty, say so clearly,
   in Turkish.
6. CRITICAL - if the user's feedback asks for additional coverage, a specific team/person/topic,
   or "more news about X" and NONE of the articles in your list actually cover that, you MUST
   NOT invent a new article, source name, or link to satisfy the request. Never fabricate a
   source that wasn't given to you. Instead, write the articles you do have, then add one
   sentence in Turkish saying that no article matching that specific request was found in the
   available data."""


@observe(as_type="agent", name="writer")
def write(user_question: str, findings: list[dict], feedback: str = None) -> str:
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

    # If the user rejected a previous draft, append their feedback - the writer
    # still sees the exact same vetted findings, only the instructions on HOW
    # to present them change. Re-running the Researcher would be wasteful here:
    # the usual rejection reason is about presentation, not wrong article choice.
    if feedback:
        user_message += f"""

Your previous draft was rejected by the user with this feedback: "{feedback}"
Revise your answer to address this feedback. The feedback may tell you to change WHICH
articles to focus on, or how long/short to be - but it never overrides the strict rules
above. In particular, every article you include must still get its own one-sentence
description, link, and date (rule 2) - do not turn into a bare list of links.
"""

    print("\n[WRITER] Synthesizing final answer from vetted findings...")
    response = traced_chat(
        "writer-draft",
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

@observe(name="refine-findings")
def refine_findings(user_question: str, findings: list[dict], feedback: str) -> list[dict]:
    """
    Re-evaluates the EXISTING vetted findings against the user's rejection feedback,
    one item at a time (same batched index-based judging as the Researcher's initial
    filter). This is deliberately a separate, narrow decision for every single item -
    not "revise the draft" (a loose, holistic task where a model tends to only act on
    what was explicitly named, e.g. removing "Fenerbahçe" but leaving other club-level
    news untouched when the user actually meant "ONLY national team news"). Generic
    across any topic/category - it never hardcodes what "national team" or similar
    means, it just asks the model to re-apply the user's own stated criterion per item.
    """
    FILTER_BATCH_SIZE = 10
    refined = []
    batches = [findings[i:i + FILTER_BATCH_SIZE] for i in range(0, len(findings), FILTER_BATCH_SIZE)]

    print(f"\n[REFINE] Re-checking {len(findings)} item(s) against feedback, in {len(batches)} batch(es)...")

    for batch_num, batch in enumerate(batches, start=1):
        slim_batch = [
            {"index": i, "title": item["title"], "text": item["text"][:200]}
            for i, item in enumerate(batch)
        ]

        refine_prompt = f"""Original topic: "{user_question}"
User's additional feedback after seeing a first draft: "{feedback}"

Below is a list of articles already judged relevant to the ORIGINAL topic. Decide, for
EACH article independently, whether it should STILL be included given the feedback:

- If the feedback is purely about writing style, length, or tone (not about which
  topics/subjects to include), then KEEP every article - do not remove anything.
- If the feedback narrows the topic (mentions a specific sub-topic, team, person, or
  says "only X"), then an article must POSITIVELY match that narrower criterion to be
  kept - do not keep an article just because it wasn't explicitly named as excluded.
  Check what each article is actually about against the new, narrower criterion.
- Judge this by the underlying meaning of the criterion, not by whether its exact
  wording literally appears in the article's text. Consider what the article is
  genuinely about in relation to what the user asked for, rather than performing a
  surface-level keyword match.

Articles:
{json.dumps(slim_batch, ensure_ascii=False, indent=2)}

Return ONLY JSON in this exact format, nothing else - "keep" is the list of indices to keep:
{{"keep": [0, 2, 3]}}
"""

        response = traced_chat(
            f"refine-batch-{batch_num}",
            model=MODEL,
            messages=[{"role": "user", "content": refine_prompt}],
            format="json",
            options=JUDGE_OPTIONS,
        )

        try:
            parsed = json.loads(response["message"]["content"])
            for idx in parsed.get("keep", []):
                if isinstance(idx, int) and 0 <= idx < len(batch):
                    refined.append(batch[idx])
        except (json.JSONDecodeError, AttributeError, TypeError):
            print(f"[REFINE] Could not parse batch {batch_num}'s output, keeping its items unchanged.")
            refined.extend(batch)

    print(f"[REFINE] {len(findings)} item(s) -> {len(refined)} kept after applying feedback")
    return refined


MAX_REVISIONS = 2  # safety limit - don't loop forever if the user keeps rejecting


LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")


@observe(as_type="guardrail", name="citation-check")
def verify_citations(answer: str, findings: list[dict]) -> list[str]:
    """
    Checks every link the writer cited against the links it was ACTUALLY given
    in `findings` - not the whole news database, just this call's own vetted
    list, since the writer should never cite anything outside what it was
    handed. Returns any URLs found in the answer that don't match - these are
    either fully invented sources, or a real article's content with a
    plausible-but-wrong URL attached (seen in practice: a model reconstructing
    a link from a domain's typical pattern instead of copying the exact string
    it was given). Either way, a mismatch here means the citation can't be
    trusted without a human checking it.
    """
    known_links = {item["link"] for item in findings}
    cited_links = [url for _, url in LINK_PATTERN.findall(answer)]
    return [url for url in cited_links if url not in known_links]


@observe(as_type="chain", name="news-agent-pipeline")
def run_pipeline(user_question: str):
    with tracer.start_as_current_observation(as_type="span", name="refresh-index") as refresh_span:
        added = refresh_index()
        refresh_span.update(output={"new_articles_indexed": added})

    print("\n================ RESEARCHER PHASE ================")
    base_findings = research(user_question)  # the original, full vetted pool - never shrunk permanently

    print("\n================ WRITER PHASE ================")
    feedback_history = []  # every piece of feedback the user has given so far, in order
    findings = base_findings
    combined_feedback = None

    for attempt in range(1, MAX_REVISIONS + 2):  # +2 = first draft + up to MAX_REVISIONS redos
        # findings decides WHICH articles are in scope (content filtering, via refine_findings
        # below); combined_feedback is passed separately so the writer can also apply purely
        # stylistic requests (length, tone) that don't change which articles are included.
        answer = write(user_question, findings, feedback=combined_feedback)

        print(f"\n--- DRAFT (attempt {attempt}) ---")
        print(answer)

        unverified_links = verify_citations(answer, findings)
        if unverified_links:
            print("\n[WARNING] This draft cites link(s) that are NOT in the list of sources")
            print("the writer was given - the model may have invented or reconstructed them:")
            for url in unverified_links:
                print(f"  - {url}")
            print("Check these before approving.")

        if attempt > MAX_REVISIONS:
            print(f"\n[PIPELINE] Reached the revision limit ({MAX_REVISIONS}) - returning this draft as final.")
            return answer

        approval = input("\nDo you approve this answer? (y = yes / n = no, request revision): ").strip().lower()

        if approval == "y":
            print("\n--- FINAL ANSWER (approved by user) ---")
            print(answer)
            return answer

        new_feedback = input("What do you want me to fix? (e.g., 'write more briefly', 'only keep football news'): ").strip()
        feedback_history.append(new_feedback)

        # Always re-filter from the ORIGINAL, full vetted pool using the FULL feedback
        # history combined - never chain off an already-narrowed list. This is what
        # lets a later request like "actually, include more coverage" recover an item
        # an earlier (possibly overly strict) narrowing step had incorrectly dropped -
        # that item is never permanently lost, since we always start from base_findings.
        combined_feedback = " ".join(f"({i+1}) {fb}" for i, fb in enumerate(feedback_history))
        findings = refine_findings(user_question, base_findings, combined_feedback)

        if not findings:
            print("\n[PIPELINE] No articles remain after applying this feedback - cannot write an answer.")
            return "Bu geri bildirime uyan haber kalmadı."


if __name__ == "__main__":
    try:
        run_pipeline("Bugün spor ile ilgili hangi haberler var, özetler misin?")
    finally:
        # No-op for this tracer (the trace file is written when the root span closes).
        # Kept so switching to a background-exporting backend needs no code change.
        tracer.flush()