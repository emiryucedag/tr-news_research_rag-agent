-- Step S0: articles table with vector search.

-- pgvector adds the "vector" column type and distance operators.
-- Supabase keeps extensions in their own schema.
create extension if not exists vector with schema extensions;

create table public.articles (
    id           text primary key,            -- md5 of the link, same as the CLI version
    title        text not null,
    summary      text,
    link         text not null unique,
    source       text not null,               -- outlet name, e.g. "NTV"
    feed         text,                        -- which RSS feed it came from
    category     text,                        -- assigned at ingestion time from the feed config
    language     text not null default 'tr',  -- 'tr' or 'en'
    published_at timestamptz not null,
    fetched_at   timestamptz not null default now(),
    embedding    extensions.vector(768)       -- Gemini embedding, truncated to 768 dims
);

-- Vector index: approximate nearest neighbour search by cosine distance.
create index articles_embedding_idx
    on public.articles using hnsw (embedding extensions.vector_cosine_ops);

-- Category tabs and recency windows filter on these two columns.
create index articles_category_published_idx
    on public.articles (category, published_at desc);

-- Lock the table. With RLS on and no policies, the public API cannot read or
-- write it. Only the service role (used by Edge Functions and the ingestion
-- job) bypasses RLS.
alter table public.articles enable row level security;
revoke all on public.articles from anon, authenticated;

-- Nearest-neighbour search used by the chat path.
-- <=> is cosine distance, so similarity = 1 - distance.
create function public.match_articles(
    query_embedding  extensions.vector(768),
    match_count      int default 10,
    filter_category  text default null,
    since            timestamptz default null
)
returns table (
    id           text,
    title        text,
    summary      text,
    link         text,
    source       text,
    category     text,
    language     text,
    published_at timestamptz,
    similarity   float
)
language sql stable
set search_path = public, extensions
as $$
    select
        a.id, a.title, a.summary, a.link, a.source, a.category, a.language,
        a.published_at,
        1 - (a.embedding <=> query_embedding) as similarity
    from public.articles a
    where a.embedding is not null
      and (filter_category is null or a.category = filter_category)
      and (since is null or a.published_at >= since)
    order by a.embedding <=> query_embedding
    limit match_count;
$$;

-- Only the server side may call it.
revoke execute on function public.match_articles from public, anon, authenticated;
