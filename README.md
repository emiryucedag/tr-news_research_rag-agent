# Turkish News Research Agent

A free-to-run system that researches and summarizes current news (Turkish and English
sources, Turkish output) with source-cited answers. It is being built in two stages:

1. **v1, a local command-line agent** (git tag `v1-cli`): RSS, a local vector store, a
   researcher and a writer agent on a local model, a human review step and tracing.
   Documented in the second half of this file.
2. **A free web app** (branch `web-app`, in progress): category tabs refreshed hourly
   without any model call, plus a chat limited to a few questions per user per day.

## Web app (in progress)

Goal: users pick a category (politics, sports, economy, technology, world, AI, pop
culture, general news) and read hourly-refreshed summaries; inside the app they can
also ask a chatbot about a specific topic. Access is by invitation (about ten users,
10 chat questions per user per day), plus one trial question per day for guests.
Everything uses free tiers only.

```
GitHub Actions (hourly)                         Supabase                       Browser
RSS feeds -> filter -> store -> embed  ->  Postgres + pgvector  <-  Edge Function "ask"
                                           (articles, 3-day window)  question -> embedding
                                                                     -> vector search
                                                                     -> one Gemini call
                                                                     -> answer
```

- **Ingestion** (`backend/jobs/`): 41 feeds in 8 categories. Junk and duplicates are
  dropped, one feed's wrong timezone label is corrected, and rows older than three days
  are deleted. Everything is stored; only as many articles as the embedding budget
  allows are embedded per run (round-robin across categories, newest first).
- **Chat path** (`supabase/functions/ask/`): the question is embedded, the 12 nearest
  articles are fetched with pgvector, and one model call selects the relevant ones. The
  model only sees numbered candidates and returns `[{n, sentence}]`; links, dates and
  source names are filled in by code, so the model cannot corrupt or invent them. Any
  failure falls back to a plain list of titles and links.
- **Database** (`supabase/migrations/`): `articles` with a 768-dimension embedding,
  row level security on and no public policies; only the server side can read or write.

What was measured on a 50-article table with `gemini-3.5-flash-lite`:

- A question took 1.2 to 2.0 seconds warm and 2.3 seconds on the first call, using
  exactly two model requests (one embedding, one generation). The v1 pipeline took about
  four minutes and five or more model calls.
- An unrelated question returned no results instead of invented ones. Two generated
  sentences that contained specific facts were checked against their sources and matched.
- Similarity scores alone do not separate relevant from irrelevant articles well
  (about 0.62 for a relevant story versus 0.57 for an unrelated query), which is why
  the model makes the final selection.

Free-tier limits that shaped the design (measured, not assumed):

- Every text in a batch embedding call counts as one request: 100 per minute and about
  1,000 per day per model. The job therefore embeds at most 35 articles per hourly run
  and at most 850 per rolling 24 hours, tracked in the database. Articles without an
  embedding still appear in category views but are not searchable in chat.
- The generation model has about 500 requests per day; summaries and chat share it.

Content policy: only titles and the short feed summaries are stored, for three days; no
article text or images; every answer links back to the source. Scraped data is not
committed to this repository. Each feed carries a flag for whether its publisher's terms
of use have been checked.

Status: database schema, the `ask` function (spike) and the hourly ingestion job are
done. Still to do: scheduled run on GitHub Actions, cached category summaries,
invitation login with per-user quotas, the web interface, and evaluations of the new
query path.

---

# v1: local command-line agent

Everything below documents the v1 agent (tag `v1-cli`). It runs on-device through
[Ollama](https://ollama.com) with no paid API key. Run its commands from the `backend/`
directory.

## What it does

Given a question like "What's the latest sports news today?", the system:

1. Refreshes its news index by checking RSS feeds for new articles since the last run
2. Searches a local vector database for semantically relevant articles, combined with
   a deterministic category-based lookup for broad topics (e.g. sports, technology)
   so that relevant articles aren't missed just because they don't share vocabulary
   with the query
3. Filters the candidates in batches to judge genuine relevance, rather than trusting
   a single large pass
4. Synthesizes the vetted findings into a Turkish-language summary, with every claim
   tied to its source name, link, and publish date/time
5. Checks every citation in the draft against the actual source list it was given,
   and presents the draft to the user for approval before treating it as final
6. Records every step of the run (prompts, outputs, token counts, timings) as a trace
   that can be read back after the fact

## Architecture

```
RSS feeds → ingestion & dedup → chunking & embedding → vector store (Chroma)
                                                               |
                                                               v
                                        researcher agent (search + relevance filtering)
                                                               |
                                                               v
                                   writer agent (cited summary synthesis + citation check)
                                                               |
                                                               v
                                              human review (approve / request revision)
```

Every step above is recorded by a small tracer (see [Observability](#observability)).

### Researcher / Writer separation

A single agent that both searches and judges relevance tends to be inconsistent -
it can include an article it never actually evaluated, especially once the number
of candidates grows. Splitting the work into two agents with a narrow,
single-purpose job each - and never letting the writer see anything the researcher
didn't explicitly vet - removes an entire class of these failures structurally.

### Retrieval strategy

Semantic similarity search is biased toward recent articles, with an automatically
widening time window (72 hours, then 1 week, then 30 days, then unrestricted) so a
niche query still gets an answer without burying "today's news" under older,
semantically-similar articles. For broad topic keywords, a direct metadata filter
pulls recent articles from known category-specific feeds as a deterministic
complement to semantic search, since a specific match preview or product
announcement often won't contain the literal topic word itself.

### Batched relevance filtering

Asking a small local model to judge the relevance of 30+ candidate articles in a
single pass degrades noticeably - it evaluates the first few items well and loses
reliability on the rest. Candidates are filtered in fixed-size batches instead,
each judged independently, with the selections merged afterward.

### Repeatable judging

Relevance decisions are yes/no judgments, so the filter and the revision step call
the model with temperature 0 and a fixed seed instead of Ollama's default sampling
temperature (0.8 according to its documentation). Before this change, running the
filter three times on exactly the same 38 stored candidates selected 13, 14 and 15
articles, and 8 of the 18 distinct articles chosen were picked in some runs but not
in others. The writer keeps the default settings, since some variety in wording is
harmless there. `evals/check_determinism.py` repeats the measurement on demand.

### Human review and revision

The writer's draft is shown to the user before it's treated as final. If rejected,
the user's feedback is used to re-filter the *original* vetted candidate pool -
not the already-narrowed list from the previous draft - so an earlier, overly
strict narrowing step never permanently loses an article that a later, broader
request should have recovered. Feedback accumulates across rounds (each revision
re-applies the full feedback history, not just the latest comment), and a revision
limit prevents an unbounded back-and-forth.

### Citation verification

Every link the writer cites is checked against the exact set of articles it was
given for that answer. A model can occasionally produce a citation for a real
story using a plausible-looking but incorrect URL (e.g. guessing a source's usual
link pattern instead of copying the exact one provided) rather than inventing an
entirely fake source - this is checked for automatically, and any unverified link
is flagged to the user before approval rather than silently trusted.

### Automated evaluation suite

A small set of scripted scenarios across different topics runs the research and
writing steps end to end and checks, automatically: whether any cited link fails
citation verification, whether every cited article has an attached date, and
whether previously-fixed junk content (e.g. broadcast-guide spam) has regressed
back into the output. Each scenario is recorded as its own trace, so a failing
scenario can be opened and followed step by step.

A separate script, `evals/check_determinism.py`, reruns the relevance filter several
times on the candidates stored in a trace and reports which selections change
between runs.

## Observability

### Tracing

`observability/tracer.py` is a small tracer built on the Python standard library
only. It uses the same vocabulary as OpenTelemetry:

- **trace**: everything that happened for one top-level call (one pipeline run)
- **span**: one step inside a trace, with a start, a duration, an input, an output
  and a parent
- **generation**: a span that is a model call; it also records the model, the
  sampling settings and the token counts

Nesting is automatic. A `ContextVar` holds "the span that is open right now"; a new
span reads it to find its parent and restores it when it ends, so no function has to
pass a parent around. When the outermost span ends, the whole trace is written to
one JSON file in `traces/` (ignored by git). The function names (`observe`,
`start_as_current_observation`) intentionally mirror the Langfuse Python SDK, so
moving to a hosted tool later is intended to be a small change.

Every model call goes through one wrapper, and every agent, tool call, retrieval,
filter and check is an `@observe`-decorated function, so the call hierarchy becomes
the tree.

### Reading a trace

```
news-agent-pipeline  [chain]  257.7s  (100%)
├─ refresh-index  [span]  9.0s  (4%)
├─ researcher  [agent]  81.8s  (32%)
│  ├─ researcher-step-1  [generation]  6.3s  (2%)  tokens in=385 out=29  model_load=3.32s
│  ├─ tool-call  [tool]  5.7s  (2%)
│  │  └─ vector-search  [retriever]  5.7s  (2%)
│  └─ relevance-filter  [span]  69.8s  (27%)  {"candidates": 38, "judged_relevant": 14, "batches": 4}
│     ├─ relevance-filter-batch-1  [generation]  19.2s  (7%)  tokens in=2004 out=190  model_load=0.85s
│     ├─ relevance-filter-batch-2  [generation]  16.1s  (6%)  tokens in=2018 out=177
│     ├─ relevance-filter-batch-3  [generation]  22.5s  (9%)  tokens in=1926 out=285
│     └─ relevance-filter-batch-4  [generation]  12.0s  (5%)  tokens in=1737 out=93
├─ writer  [agent]  116.6s  (45%)
│  └─ writer-draft  [generation]  116.6s  (45%)  tokens in=5075 out=1748
└─ citation-check  [guardrail]  0ms  (0%)
```

The root span's duration includes the time the pipeline spent waiting for the user's
approval, so latency is best read from the child spans.

```bash
python3 observability/show_trace.py                  # list recent traces
python3 observability/show_trace.py 1                # span tree of the latest trace
python3 observability/show_trace.py 1 --io           # tree with input/output previews
python3 observability/show_trace.py 1 --span writer  # full prompt and output of a step
```

### What the traces revealed

- **A wasted model call.** The researcher originally looped so the model could issue
  follow-up searches. The traces showed that its second call never requested another
  search in the runs observed, and its answer was discarded every time. It cost about
  30 seconds per run, so the default is now a single search round. In one
  before/after comparison the researcher span dropped from 105 s to 82 s.
- **Where the time goes.** Model calls dominate a run, and the writer is the largest
  single span. Its generation speed was about 15 tokens per second in two separate
  runs (1177 tokens in 78.5 s, 1748 tokens in 116.6 s), so its duration follows how
  much text it writes rather than how much it reads.

## Tech stack

- **Ingestion**: `feedparser` against a configurable set of Turkish RSS feeds
- **Embeddings**: `sentence-transformers` (`paraphrase-multilingual-MiniLM-L12-v2`)
- **Vector store**: `ChromaDB`, cosine similarity, metadata-based recency and
  category filtering
- **LLM / agents**: `qwen2.5:7b` served locally via `Ollama`, with tool calling and
  structured (JSON) output
- **Tracing**: a custom tracer, Python standard library only
- **Language**: Python 3

## Project structure

```
news-agent/
├── backend/
│   ├── ingestion/
│   │   └── fetch_news.py         # v1: RSS fetching, dedup, junk filtering
│   ├── rag/
│   │   ├── build_index.py        # v1: full and incremental vector index builds
│   │   ├── retrieval.py          # v1: semantic + category-based retrieval
│   │   └── refresh.py            # v1: fetch + index update, run before every query
│   ├── agents/
│   │   └── multi_agent.py        # v1: researcher + writer pipeline, review loop, citation check
│   ├── observability/
│   │   ├── tracer.py             # trace / span / generation recording
│   │   └── show_trace.py         # list traces, render span trees, dump a step's prompt
│   ├── evals/
│   │   ├── run_evals.py          # automated scenario checks (citations, dates, junk regressions)
│   │   └── check_determinism.py  # repeatability check for the relevance filter
│   ├── jobs/                     # web app: hourly ingestion (feeds.py, ingest.py, common.py)
│   ├── spike/                    # web app: throwaway scripts from the architecture spike
│   ├── traces/                   # created at runtime, ignored by git
│   └── requirements.txt          # v1 dependencies (the jobs have their own list)
├── supabase/
│   ├── migrations/               # database schema
│   └── functions/ask/            # chat Edge Function
├── .github/workflows/            # scheduled jobs
└── HANDOFF.md                    # working notes for continuing the project
```

Scraped article data (`backend/ingestion/news_data.json`) is created locally by the
fetch step and is not tracked by git.

## Setup

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r backend/requirements.txt
cd backend

# Install Ollama from https://ollama.com, then pull the model:
ollama pull qwen2.5:7b
```

## Usage

```bash
python3 agents/multi_agent.py
```

Edit the question at the bottom of `multi_agent.py`, or import `run_pipeline()`
from your own script. The pipeline will show you a draft and ask for approval; you
can reject it with free-text feedback (e.g. "only football news", "write more
briefly") and it will revise accordingly, up to a small revision limit. When the run
ends, the trace is saved under `traces/`.

To run the automated evaluation suite instead of the interactive pipeline:

```bash
python3 evals/run_evals.py
```

To check how repeatable the relevance filter is on the candidates of a saved trace:

```bash
python3 evals/check_determinism.py --runs 3
```

## Known limitations

- Retrieval and relevance judgments depend on a 7B local model; a larger model
  (hosted or local) would likely improve consistency further
- Relevance judgments are imperfect: in a repeatability check, clearly relevant sports
  stories were left out in some runs, and two outlets' versions of the same story were
  treated inconsistently
- Some non-news items, such as betting tips and broadcast guides, still pass the junk
  filters and can appear in answers
- The writer occasionally produces a citation with a link that does not match the
  one it was given - this is caught by the citation check and flagged, but not
  auto-corrected
- With longer result lists, the writer occasionally omits a date or silently drops
  one of the supplied articles - a known small-model limitation with strict per-item
  formatting over long lists
- A run takes a few minutes on a 16 GB laptop; most of it is model generation
- RSS feed coverage is limited to the sources configured in `fetch_news.py`
- No persistent conversation memory between runs - each query is independent