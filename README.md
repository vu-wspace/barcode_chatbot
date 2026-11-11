# Barcode chatbot

The packing station scans a barcode. The page shows the product from the database and checks the box against the
order. **The BE decides whether the box may be sealed** (`POST /orders/{id}/verify` → `complete`); the AI assistant
only explains that result and answers questions about products and orders.

```
FE (scan) -> gate (nginx 127.0.0.1:8080) -> /api/ -> be (FastAPI, role be_app) -> Postgres (products, orders)
                                         -> /ai/  -> ai (LangChain agent, role ai_app) -> tools call be's API
```

| Folder | Service |
|---|---|
| `src/barcode_chatbot/fe` | scanner page + nginx gate (`default.conf.template`) |
| `src/barcode_chatbot/be` | BE: `/products`, `/products/{barcode or sku}`, `/orders/{id}`, `POST /orders/{id}/verify` |
| `src/barcode_chatbot/agent` | AI agent `POST /chat` (SSE); tools `get_product`, `search_products`, `get_order`, `verify_order` |

## Run

```
cp .env.example .env    # set POSTGRES_PASSWORD, BE_DB_PASSWORD, AI_DB_PASSWORD, OPENROUTER_API_KEY, GOOGLE_API_KEY
docker compose up -d --build
```

Open http://localhost:8080, load order `SO-104587` and scan or type `8935000100011`, `8935000200018`, `8935000300015`.

Start order: `db` → `migrate` (one-shot, as the DB owner: migrations, demo data from `be/app/seed.sql` while
`SEED_DEMO=true`, and the `be_app` / `ai_app` roles) → `be`, `ai` → `gate`.

- `be_app` can only SELECT `products`, `orders`, `order_items`. `ai_app` owns schema `agent` (chat history) and cannot
  read shop data. The `postgres` superuser is used only by `migrate`.
- Only the variables listed under `ai` in `docker-compose.yml` reach the agent; other keys in `.env` do not.
- **The gate listens on 127.0.0.1 only.** There is no login: put authentication (and HTTPS, needed for the camera on a
  phone) in front before publishing it on a network.

### Gate protections (nginx)
- `/ai/` is rate-limited to 30 turns/min with bursts of 20 → `429` with a JSON `detail`.
- Paths go to `be`/`ai` exactly as sent (no decoding of `%2F` / `..` on the way); the BE only accepts codes and order
  ids made of letters, digits and `-`.
- Security headers (CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`), `/api/docs` and `/ai/docs` blocked, and
  requests with an unknown `Host` dropped. Docs stay reachable inside the Docker network.

## AI chat API: `POST /ai/chat`

Body: `{"message": "...", "thread_id": "<uuid>", "request_id": "<uuid>"}`. Both ids are optional UUIDs; the page
creates `thread_id` when it opens and a new `request_id` per question. Sending the same `request_id` again (retry)
returns the stored answer instead of running the agent twice.

The response is `text/event-stream`:

| event | data | |
|---|---|---|
| `meta` | `{thread_id, request_id}` | always first |
| `tool` | `{name}` | the agent starts a tool call |
| `token` | `{text}` | a piece of the answer, for display only |
| `done` | `{thread_id, answer, tools}` | final answer; replaces the streamed text |
| `error` | `{status, detail}` | `status` as in HTTP: 409 thread busy, 429 rate limit, 502 model error, 503 busy/DB, 504 too slow |

A `: ping` comment comes every 15 s. Closing the connection cancels the turn. One turn has 120 s; the page gives up
after 170 s, nginx after 180 s without data.

## Tests

```
docker compose run --rm migrate python -m app.server check   # BE + migrations, against the real DB (as owner)
cd src/barcode_chatbot/agent && uv run pytest                 # agent (no LLM, BE or DB needed)
node src/barcode_chatbot/fe/core.test.js                      # scanner logic
```

Page E2E (Playwright, Chromium): the real `index.html` against `fe/e2e/server.js`, a fake BE plus a scripted SSE agent,
so no Docker, DB or LLM is needed. Covers the BE-decided seal verdict, QR text kept out of the AI, SSE events split
across reads, error → "Thử lại" with the same `request_id`, a stream cut before `done`, merging of waiting checks,
and the order-loading lock.

```
cd src/barcode_chatbot/fe
npm ci && npx playwright install chromium   # once
npm test                                    # core.test.js + E2E
```

Postgres-only agent paths (advisory lock, checkpoint pruning, 7-day retention) run when a DB is given; they use a
scratch schema and drop it:

```
cd src/barcode_chatbot/agent
TEST_DATABASE_URL=postgresql://postgres:<POSTGRES_PASSWORD>@127.0.0.1:5433/barcode_chatbot uv run pytest
```

Use `127.0.0.1`, not `localhost`: on Windows `localhost` tries IPv6 first and each connection waits ~8 s.

## Database changes (migrations)

The `migrate` service applies `src/barcode_chatbot/be/app/migrations/NNN_name.sql` in name order, each file once
(recorded in the `schema_migrations` table). One run is one transaction, so a failing file leaves the DB unchanged
and `be`/`ai` do not start.

- Add a new file with the next number, e.g. `003_add_product_weight.sql`. Never edit a file that has been applied.
- Rename/retype columns or move data in the new file; there is no down migration, so write the reverse by hand if needed.
- A new table the BE reads needs a `GRANT SELECT ... TO be_app` in the same file.
- Demo data stays in `seed.sql` (loaded while `SEED_DEMO=true`), not in migrations.
