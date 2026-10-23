-- Daily usage counters for the chat function. The day is the Turkish calendar day
-- (Turkey has had no daylight saving time since 2016, so it is always UTC+3).
create table public.usage_daily (
    day     date not null,
    subject text not null,            -- 'user:<uuid>', 'guest:<ip hash>', or a pool name
    count   int  not null default 0,
    primary key (day, subject)
);

-- Only the server side (service_role) touches this table.
alter table public.usage_daily enable row level security;
grant select, insert, update, delete on public.usage_daily to service_role;

-- Take one unit from a subject's limit AND from a shared pool limit, atomically.
-- The ON CONFLICT ... WHERE makes the increment a no-op once the limit is reached,
-- so two simultaneous requests cannot both slip past the last unit.
-- Returns {"status": "ok" | "subject_limit" | "pool_limit", "used": <subject's count>}.
create function public.consume_quota(
    p_subject text, p_limit int, p_pool text, p_pool_limit int
) returns jsonb
language plpgsql
set search_path = public
as $$
declare
    d         date := (now() at time zone 'Europe/Istanbul')::date;
    used      int;
    pool_used int;
begin
    insert into usage_daily as u (day, subject, count) values (d, p_subject, 1)
    on conflict (day, subject) do update set count = u.count + 1 where u.count < p_limit
    returning u.count into used;

    if used is null then
        select count into used from usage_daily where day = d and subject = p_subject;
        return jsonb_build_object('status', 'subject_limit', 'used', coalesce(used, p_limit));
    end if;

    insert into usage_daily as u (day, subject, count) values (d, p_pool, 1)
    on conflict (day, subject) do update set count = u.count + 1 where u.count < p_pool_limit
    returning u.count into pool_used;

    if pool_used is null then
        -- The pool is full: give the unit back to the subject.
        update usage_daily set count = count - 1 where day = d and subject = p_subject;
        return jsonb_build_object('status', 'pool_limit', 'used', used - 1);
    end if;

    return jsonb_build_object('status', 'ok', 'used', used);
end;
$$;

-- Give one unit back (used when the model could not produce an answer).
create function public.refund_quota(p_subject text, p_pool text) returns void
language sql
set search_path = public
as $$
    update usage_daily
    set count = greatest(count - 1, 0)
    where day = (now() at time zone 'Europe/Istanbul')::date
      and subject in (p_subject, p_pool);
$$;

revoke execute on function public.consume_quota(text, int, text, int) from public, anon, authenticated;
revoke execute on function public.refund_quota(text, text) from public, anon, authenticated;
grant execute on function public.consume_quota(text, int, text, int) to service_role;
grant execute on function public.refund_quota(text, text) to service_role;
