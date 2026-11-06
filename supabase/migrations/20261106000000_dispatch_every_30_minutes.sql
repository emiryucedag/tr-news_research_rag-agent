-- The free Gemini quota ran out within the day at one run every 20 minutes: refresh every 30 minutes instead.
select cron.unschedule('dispatch-ingest') where exists (select 1 from cron.job where jobname = 'dispatch-ingest');
select cron.schedule('dispatch-ingest', '*/30 * * * *', $$select public.dispatch_ingest()$$);
