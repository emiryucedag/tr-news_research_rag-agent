-- Precomputed category summaries. The hourly job writes one row per category;
-- the web app reads them and never calls a model for a tab.
create table public.category_summaries (
    category          text primary key,
    mode              text not null check (mode in ('summary', 'fallback', 'empty')),
    items             jsonb not null default '[]'::jsonb,  -- [{sentence, title, source, link, published_at}]
    model             text,                                -- model that wrote the sentences, null for fallback/empty
    article_count     int not null default 0,              -- candidates the summary was built from
    fingerprint       text,                                -- hash of the candidate ids; unchanged means skip regeneration
    newest_article_at timestamptz,
    generated_at      timestamptz not null default now()
);

alter table public.category_summaries enable row level security;

-- Tabs require a login (invited users only; public signups are disabled).
create policy "signed-in users can read summaries"
    on public.category_summaries for select to authenticated using (true);

-- No automatic grants on this project, so grant explicitly.
grant select on public.category_summaries to authenticated;
grant select, insert, update, delete on public.category_summaries to service_role;
