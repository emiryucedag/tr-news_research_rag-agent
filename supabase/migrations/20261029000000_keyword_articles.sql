-- Keyword search over titles and summaries, used next to the vector search. About two thirds of recent
-- articles have no embedding (the free embedding quota is smaller than the feed volume), so the vector
-- search alone cannot see them. Terms are matched as prefixes anywhere in the text, which copes with
-- Turkish suffixes ("galatasaray" also finds "Galatasaray'ın"); articles matching more terms rank first.
create or replace function public.keyword_articles(
    terms text[],
    since timestamptz default null,
    match_count integer default 10
)
returns table (
    id text, title text, summary text, link text, source text, category text,
    language text, published_at timestamptz, hits integer
)
language sql
stable
set search_path to 'public'
as $$
    select a.id, a.title, a.summary, a.link, a.source, a.category, a.language, a.published_at,
           (select count(*)::integer from unnest(terms) t
             where a.title ilike '%' || t || '%' or coalesce(a.summary, '') ilike '%' || t || '%') as hits
    from public.articles a
    where (since is null or a.published_at >= since)
      and exists (select 1 from unnest(terms) t
                  where a.title ilike '%' || t || '%' or coalesce(a.summary, '') ilike '%' || t || '%')
    order by hits desc, a.published_at desc
    limit match_count;
$$;

revoke execute on function public.keyword_articles(text[], timestamptz, integer) from public, anon, authenticated;
grant execute on function public.keyword_articles(text[], timestamptz, integer) to service_role;
