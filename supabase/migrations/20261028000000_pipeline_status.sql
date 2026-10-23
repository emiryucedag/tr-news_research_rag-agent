-- Heartbeat of the scheduled jobs. The site shows how long ago the last check happened, so a
-- stalled scheduler is visible to readers instead of leaving silently stale pages.
-- (The summaries' own generated_at only changes when the news changes, so it cannot tell a quiet
-- night from a stopped job.)
create table public.pipeline_status (
    id            text primary key default 'main',
    ingested_at   timestamptz,   -- last time the feeds were fetched
    summarized_at timestamptz    -- last time the section summaries were checked
);

alter table public.pipeline_status enable row level security;

create policy "anyone can read pipeline status"
    on public.pipeline_status for select to anon, authenticated using (true);

grant select on public.pipeline_status to anon, authenticated;
grant select, insert, update on public.pipeline_status to service_role;

insert into public.pipeline_status (id) values ('main');
