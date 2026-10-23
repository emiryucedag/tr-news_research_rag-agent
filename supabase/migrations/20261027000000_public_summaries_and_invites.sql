-- Category summaries are the same for everyone and cost no model call per reader, so they
-- are readable without logging in. Everything else stays server-side only.
drop policy "signed-in users can read summaries" on public.category_summaries;

create policy "anyone can read summaries"
    on public.category_summaries for select to anon, authenticated using (true);

grant select on public.category_summaries to anon;

-- Invitation requests: visitors leave an email and a short note; the owner reads them in the
-- dashboard and invites people by hand. No password is ever collected.
create table public.invite_requests (
    id         uuid primary key default gen_random_uuid(),
    email      text not null,
    note       text,
    ip_hash    text,
    created_at timestamptz not null default now()
);

-- One request per address, whatever the capitalisation.
create unique index invite_requests_email_idx on public.invite_requests (lower(email));

alter table public.invite_requests enable row level security;
grant select, insert on public.invite_requests to service_role;
