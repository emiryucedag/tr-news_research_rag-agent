-- Track when each article was embedded, so the ingestion job can enforce the
-- daily embedding budget from the database instead of from local state.
alter table public.articles add column embedded_at timestamptz;

-- Rows that already have an embedding were embedded by the spike scripts.
update public.articles set embedded_at = fetched_at where embedding is not null;

-- The job repeatedly asks for "newest rows that still need an embedding".
create index articles_unembedded_idx
    on public.articles (published_at desc)
    where embedding is null;

-- Retention cleanup and the daily budget count both filter on these.
create index articles_embedded_at_idx
    on public.articles (embedded_at)
    where embedded_at is not null;
