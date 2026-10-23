-- GitHub's own scheduler fires scheduled workflows only every few hours for this repository, so the hourly
-- job is also started from here: every 20 minutes the database asks GitHub to run the ingest workflow.
-- The workflow's concurrency group keeps two runs from overlapping, and the job is safe to repeat.
--
-- The GitHub token is NOT in this file. It lives in Vault under the name 'github_dispatch_token' (a
-- fine-grained token limited to this repository with Actions: read and write), stored once with
--   select vault.create_secret('<token>', 'github_dispatch_token');
create extension if not exists pg_cron with schema extensions;
create extension if not exists pg_net with schema extensions;

create or replace function public.dispatch_ingest()
returns bigint
language sql
security definer
set search_path to 'public', 'extensions', 'vault'
as $$
    select net.http_post(
        url := 'https://api.github.com/repos/emiryucedag/tr-news_research_rag-agent/actions/workflows/ingest.yml/dispatches',
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token'),
            'Accept', 'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'User-Agent', 'tr-news-agent-cron',
            'Content-Type', 'application/json'),
        body := '{"ref":"main"}'::jsonb);
$$;

revoke execute on function public.dispatch_ingest() from public, anon, authenticated;

select cron.schedule('dispatch-ingest', '*/20 * * * *', $$select public.dispatch_ingest()$$);
