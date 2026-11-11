-- Chat history used to live in schema public (before the ai_app role, which writes to schema agent).
-- Nothing reads these tables any more and the 7-day retention job no longer reaches them, so old conversations
-- (with customer names) would stay forever. Back up first if you need them: pg_dump -t 'public.checkpoint*'
DROP TABLE IF EXISTS public.checkpoint_writes, public.checkpoint_blobs, public.checkpoints, public.checkpoint_migrations;
