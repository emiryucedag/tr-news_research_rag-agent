# Tr News Agent

A news digest and question-answering system for Turkish readers. Every hour it collects stories from 42
Turkish and English feeds, reads the articles, and publishes short, source-linked summaries in eight
sections. Readers can ask questions about a story or about the news in general and get answers that name
their sources. The live site is https://emiryucedag.github.io/tr-news_research_rag-agent/.

It runs entirely on free tiers (GitHub Actions, Supabase, the Gemini API free tier, GitHub Pages), and
that limit shaped the design: roughly 500 generation requests and 1,000 embeddings per day have to cover
about 2,000 new articles a day plus every reader's questions.

## What makes it work

- **The model writes; code decides what is true.** A model never produces a link, outlet name or date; those
  come from the database. Every generated sentence is checked in code against the article text it came from:
  numbers must appear in the source, names must overlap with it, filler such as "drew wide attention" is
  removed unless the source says it, and a headline that only says someone "made a statement" is rewritten
  to say what was said. A failed check drops or downgrades the item instead of publishing it.
- **Summaries come from articles, not feed teasers.** Feeds give two lines. The job reads the article page
  (only where robots.txt allows it and the outlet's terms do not forbid it; text is never stored) and writes
  from that. Measuring the result showed which outlets cannot be read, and those stories keep the teaser.
- **Hybrid retrieval.** Questions run a pgvector similarity search and a keyword search together. The
  keyword path exists because the free embedding quota left about two thirds of recent articles without a
  vector, so vector search alone could not see them. That gap was found by checking why a plain question
  returned nothing, not by guessing.
- **A story question has an order of fallbacks.** It reads the story's own page, then searches the three-day
  archive, then a free news search, and streams each step to the reader. If nothing answers it, it says so
  and gives the quota unit back instead of inventing an answer.
- **Operated like a service.** Atomic per-user and per-day quotas with refunds on failure, JWT verification,
  row-level security with a public key that can read only the published summaries, a CORS allow-list, two
  generation models with a plain-list fallback, request timeouts, retries, per-section failure isolation, and
  a heartbeat the site shows when updates fall behind.
- **Evaluated, including the evaluator.** About 280 checks look at properties of an answer rather than its
  wording (links exist, numbers are supported, unrelated questions return nothing, latency stays in budget).
  A separate judge model grades relevance, was itself tested on obvious cases, and a self-test proves the
  checks can fail. Changes were kept or reverted by these results; the lessons section lists what they caught.

## Stack

Python jobs (feedparser, httpx) on GitHub Actions; Postgres with pgvector and row-level security on Supabase;
a TypeScript Edge Function on Deno; Gemini models for generation and embeddings; a static JavaScript front end
with no build step; a local-model command-line agent (v1, `v1-cli` tag) with researcher and writer agents,
human review and tracing, documented in the second half of this file.

## Limits, stated plainly

Summaries are machine-written and can be wrong, so each links to its source. Some outlets cannot be read
for their full text, and the internet-search fallback uses a free service that sometimes returns nothing.
The free tiers cap volume: not every article is embedded, and a few questions per reader per day are allowed.

## Web app

Goal: users pick a category (politics, sports, economy, technology, world, AI, pop
culture, general news) and read hourly-refreshed summaries; they can also ask a chatbot
about a specific topic. The summaries are the same for everyone and cost no model call per
reader, so they are public. The chat is limited: invited users (about ten, by hand) get 10
questions per day, guests get one trial question per day. Everything uses free tiers only.

```
GitHub Actions (hourly)                         Supabase                       Browser
RSS feeds -> filter -> store -> embed  ->  Postgres + pgvector  <-  Edge Function "ask"
                                           (articles, 3-day window)  question -> embedding
                                                                     -> vector search
                                                                     -> one Gemini call
                                                                     -> answer
```

- **Ingestion** (`backend/jobs/`): 42 feeds in 8 categories. Junk and duplicates are
  dropped, one feed's wrong timezone label is corrected, and rows older than three days
  are deleted. Before a feed is fetched, the site's `robots.txt` is read and obeyed (`jobs/robots.py`);
  `jobs/check_publishers.py` also prints the notes publishers leave in `robots.txt` about AI use
  and scraping. Everything is stored; only as many articles as the embedding budget
  allows are embedded per run (round-robin across categories, newest first).
- **Section summaries** (`backend/jobs/summarize.py`): in two steps per section. First one model call picks up
  to 12 distinct stories from the last 48 hours (titles and feed teasers) and tags each with a sub-topic and
  the other outlets reporting it. Then, for stories that are new this run, the article page is read (only
  when the site's robots.txt allows it and the outlet's terms do not restrict it; the text is never stored)
  and the headline, one-sentence summary and detailed summary are written from what the article says, so a
  statement is reported as the statement and not as "he made a statement". Every text must pass the number,
  name and padding checks against the article text; a headline that only says something was said is
  rewritten or kept. The site reads the stored result and never calls a model for a section.
- **Chat path** (`supabase/functions/ask/`): the question is embedded, the 12 nearest
  articles are fetched with pgvector, and one model call selects the relevant ones. The
  model only sees numbered candidates and returns `[{n, sentence}]`; links, dates and
  source names are filled in by code, so the model cannot corrupt or invent them. Any
  failure falls back to a plain list of titles and links and gives the user's unit back. Model calls
  have timeouts and a second model takes over if the first fails. The model also lists the specific
  things the user asked for and whether it found them; the code turns the missing ones into a fixed
  sentence ("Haberlerde puan durumu yer almıyor."), so an answer says what the news does not contain.
  Signed-in users (Supabase Auth JWT, verified in the function against the project's
  public keys) get 10 questions per day; guests without a token get 1 per day per
  salted IP hash. Both are also capped as groups. Counters live in the database,
  reset at Turkish midnight and are incremented atomically before any model call.
- **Invitation requests** (`supabase/functions/request-invite/`): visitors leave an email
  address and a short note, never a password. The request is validated, rate limited
  (3 per visitor and 50 in total per day), protected by a hidden bot-trap field and stored in
  a table the owner reads in the dashboard; an invited person chooses their own password.
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

### Evaluating the web query path

`backend/evals/eval_web.py` checks the stored summaries and the deployed `ask` function
(about 180 checks, 7 questions, a few minutes). News changes every hour, so the checks look
at properties of an answer instead of exact wording: every link exists in the database, every
number and name in a sentence is supported by the article it links to, a question about
nothing returns nothing, and a judge model says whether the items fit the question or the
section. The judge is a different model from the writer so it does not grade its own work,
and it is calibrated on obvious cases before its verdicts are trusted (`--judge-test`).
`--self-test` feeds deliberately broken answers to the checks to prove they can fail. A
third group of checks uses the public key that ships with the website and requires that it
can read the summaries and nothing else: no articles, no usage counters, no invitation
requests, no writes, no server functions.

What the evaluations found and what was changed:

- **About 10 percent of generated sentences in one section were linked to the wrong
  article.** The model chose articles by number and sometimes wrote the wrong one. Now it
  copies the article title, the code matches on the title, and a grounding check requires the
  sentence to share a name or number with the linked article.
- **Section tabs contained stories from other subjects**, because a story's category came from
  its feed and some feeds are general. General feeds are now their own category, the summary
  prompt names the section's subject, and a section can draw extra candidates from the general
  feeds, balanced across outlets (the politics tab had been fed by a single outlet).
- **Chat answers merged too aggressively.** A rule to drop duplicates collapsed ten useful
  articles about one event into one sentence; the prompt now keeps distinct angles.
- **The detailed summaries invented filler.** Asked for "3 to 5 sentences" when a source held one, the
  model padded with sentences such as "the incident drew wide attention in the press", which no source
  said and which contain no number or name for the other checks to catch. The length demand was dropped
  and sentences claiming a reaction the sources do not state are removed in code.
- **Feed teasers are not content.** Summaries written from a feed's two-line teaser said things such as
  "X made a statement after the match", because the teaser said exactly that. Reading the article page
  fixed it for most outlets; for those whose terms forbid it or whose page cannot be read, the story keeps
  the teaser-based text.
- **The hourly job did not run hourly.** GitHub's scheduler fired twice in ten hours (a 321-minute gap),
  seen as a site that stopped updating. The job now tries three times an hour and writes a heartbeat
  that the site shows ("last checked 14 minutes ago", or a warning when it is late) and the evaluations
  check Still too unreliable (runs every 5 to 7 hours), so the database now starts the workflow itself: a
  `pg_cron` job calls GitHub's workflow-dispatch API every 20 minutes with a token limited to this one
  repository and kept in Supabase Vault; GitHub's own schedule stays as a backup.
- **Looking at screenshots found bugs the checks could not.** Two colour variables were defined where
  the variables they depend on did not exist yet, so they silently became invalid (pale backgrounds and
  borders vanished); a `hidden` attribute lost to a `display` rule; tabular figures widened commas in
  this font. Each was visible at a glance in a headless-browser capture.
- **A "what the news does not cover" note written by the model invented gaps.** On open questions
  it listed things nobody asked about in 4 of 9 answerable cases, and once contradicted its own
  answer. The model now only marks which specifically requested items it did not find, and the
  sentence is built in code: 0 unwanted notes in 12 tries, 6 of 6 correct on unanswerable ones.
- **"The chat is slow" was measured before being fixed.** The server needs about 1.6 s (model call
  about 1 s); cold start adds 0.6 s, token verification 0.1 s. What was missing was a timeout and a
  fallback model for the moments the model is overloaded, and a preflight cache header for browsers.
- **The first judge was wrong** (it called a TV-series question's correct answers irrelevant),
  caused by a confusing instruction and rate limits. A per-item yes/no verdict, pacing and the
  calibration test fixed it. The offline self test also exposed a gap in the grounding check
  (a sentence starting with a name like OpenAI), which is now covered by a test.

Free-tier limits that shaped the design (measured, not assumed):

- Every text in a batch embedding call counts as one request: 100 per minute and about
  1,000 per day per model. The job therefore embeds at most 35 articles per hourly run
  and at most 850 per rolling 24 hours, tracked in the database. Articles without an
  embedding still appear in category views but are not searchable in chat.
- The generation model has about 500 requests per day; summaries and chat share it.

The interface is a plain static page (no build step), named Tr News Agent. Its structure and type follow an
Apple-style design analysis: a parchment page with white cards, one blue for everything clickable, tight
display type on the system font (San Francisco on Apple devices, self-hosted Inter elsewhere), 17px body text,
pill-shaped actions, no shadows on interface elements, a frosted top bar. The one deliberate departure is that
each section keeps a small colour mark of its own (its drawn icon, the bar under the selected tab, the rule
above the lead story), which is never used for actions. A story is a card with a headline and a one-line
summary; opening it shows a longer summary, the other outlets reporting the same event, a link to the source
and a box to ask about that story. It follows the system light or dark setting and works at phone width.
Fonts and the browser library are served from this site, so visitors' browsers contact no third party.
The design also follows the contrast, touch-target and reduced-motion rules of Anthropic's `frontend-design`
guidance and the ui-ux-pro-max checklist. Earlier versions, a coloured newspaper look and then a too colourful
page, were replaced after review.

Personalisation stays in the browser: what a reader opens is remembered locally (with a two-week
half-life), sections are ordered by it once there is enough to go on, and when one sub-topic dominates
a section ("always football in sports") the site asks whether to give it a heading of its own. It can
be switched off and wiped in one place, and nothing is sent to the server.

Publisher terms were read where possible. The Guardian (which states that use for AI purposes is
not permitted), Collider and Screen Rant (automated retrieval prohibited without written permission)
and NTV (its RSS notice allows only non-commercial personal use and rejects adapting the content)
were removed from the feeds. TRT Haber's general terms forbid quoting, NPR's feeds are described as
for personal use; both are marked in `feeds.py` and undecided. Content policy: only titles and the short feed summaries are stored, for three days; no
article text or images; every answer links back to the source. Scraped data is not
committed to this repository. Each feed carries a flag for whether its publisher's terms
of use have been checked.

Status: database schema, the `ask` function with login and quotas, the invitation request
form, the hourly ingestion job, the cached category summaries, evaluations of the query path
and a plain web interface (`web/`) are done. The jobs run hourly on GitHub Actions and the
site is published to GitHub Pages by `.github/workflows/pages.yml` whenever `web/` changes:
https://emiryucedag.github.io/tr-news_research_rag-agent/ (summaries are public; the full
chat needs an invited account). Still to do: a nicer interface.

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
│   │   ├── run_evals.py          # v1: automated scenario checks (citations, dates, junk regressions)
│   │   ├── check_determinism.py  # v1: repeatability check for the relevance filter
│   │   ├── eval_web.py           # web app: checks on summaries and the ask function
│   │   ├── test_guards.py        # web app: unit tests for the sentence checks
│   │   ├── test_summarize.py     # web app: unit tests for turning model picks into stories
│   │   └── test_robots.py        # web app: unit tests for the robots.txt gate
│   ├── jobs/                     # web app: hourly ingestion (feeds.py, ingest.py, common.py)
│   ├── spike/                    # web app: throwaway scripts from the architecture spike
│   ├── traces/                   # created at runtime, ignored by git
│   └── requirements.txt          # v1 dependencies (the jobs have their own list)
├── supabase/
│   ├── migrations/               # database schema
│   └── functions/ask/            # chat Edge Function
├── web/                          # static site (Tr News Agent); fonts/ and vendor/ are served from here
└── .github/workflows/            # hourly jobs and the Pages deployment
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