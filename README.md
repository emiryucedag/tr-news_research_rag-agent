# Turkish News Research Agent

A local, fully free-to-run system that researches and summarizes current Turkish news
on any topic the user asks about. It combines a retrieval-augmented generation (RAG)
pipeline with a multi-agent architecture to find, filter, write, and verify a
source-cited answer - with a human review step before anything is treated as final.

Everything runs on-device through [Ollama](https://ollama.com) - no paid API key
is required.

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
back into the output. This turns "did my change break something" from a guess
into a measurable, repeatable check.

## Tech stack

- **Ingestion**: `feedparser` against a configurable set of Turkish RSS feeds
- **Embeddings**: `sentence-transformers` (`paraphrase-multilingual-MiniLM-L12-v2`)
- **Vector store**: `ChromaDB`, cosine similarity, metadata-based recency and
  category filtering
- **LLM / agents**: `qwen2.5:7b` served locally via `Ollama`, with tool calling and
  structured (JSON) output
- **Language**: Python 3

## Project structure

```
news-agent/
├── ingestion/
│   ├── fetch_news.py          # RSS fetching, dedup, junk filtering
│   └── news_data.json         # raw article store
├── rag/
│   ├── build_index.py         # full and incremental vector index builds
│   ├── retrieval.py           # semantic + category-based retrieval
│   └── refresh.py             # fetch + index update, run before every query
├── agents/
│   └── multi_agent_ollama.py  # researcher + writer pipeline, review loop, citation check
├── evals/
│   └── run_evals.py           # automated scenario checks (citations, dates, junk regressions)
└── requirements.txt
```

## Setup

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# Install Ollama from https://ollama.com, then pull the model:
ollama pull qwen2.5:7b
```

## Usage

```bash
python3 agents/multi_agent_ollama.py
```

Edit the question at the bottom of `multi_agent_ollama.py`, or import `run_pipeline()`
from your own script. The pipeline will show you a draft and ask for approval; you
can reject it with free-text feedback (e.g. "only football news", "write more
briefly") and it will revise accordingly, up to a small revision limit.

To run the automated evaluation suite instead of the interactive pipeline:

```bash
python3 evals/run_evals.py
```

## Known limitations

- Retrieval and relevance judgments depend on a 7B local model; a larger model
  (hosted or local) would likely improve consistency further
- The writer occasionally produces a citation for a real story using a slightly
  wrong, reconstructed URL rather than the exact one it was given - this is caught
  by the citation check and flagged, but not auto-corrected
- With longer result lists, the writer occasionally omits the date field for one
  item out of many - a known small-model limitation with strict per-item formatting
  over long lists
- RSS feed coverage is limited to the sources configured in `fetch_news.py`
- No persistent conversation memory between runs - each query is independent