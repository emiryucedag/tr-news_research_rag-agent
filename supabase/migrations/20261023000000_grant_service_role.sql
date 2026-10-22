-- The project was created with "Automatically expose new tables" turned off,
-- so no role has privileges on new objects by default. Give the server-side
-- role (service_role, used by the ingestion job and Edge Functions) access.
-- anon and authenticated stay locked out on purpose.

grant select, insert, update, delete on public.articles to service_role;

grant execute on function public.match_articles(extensions.vector, int, text, timestamptz)
    to service_role;
