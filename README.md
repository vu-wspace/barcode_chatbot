# Barcode chatbot

The packing station scans a barcode. The page shows the product from the database, and the AI assistant says what the product is and whether the box matches the order.

```
FE (scan) -> gate (nginx :8080) -> /api/ -> be (FastAPI) -> Postgres (products, orders)
                                -> /ai/  -> ai (LangChain agent) -> tools call be's API -> Postgres
```

| Folder | Service |
|---|---|
| `src/barcode_chatbot/fe` | scanner page + nginx gate (`default.conf.template`) |
| `src/barcode_chatbot/be` | BE: `/products`, `/products/{barcode or sku}`, `/orders/{id}`, `POST /orders/{id}/verify` |
| `src/barcode_chatbot/agent` | AI agent `POST /chat`; tools `get_product`, `search_products`, `get_order`, `verify_order` |

## Run

```
cp .env.example .env    # set POSTGRES_PASSWORD, OPENROUTER_API_KEY, GOOGLE_API_KEY, GROQ_API_KEY
docker compose up -d --build
```

Open http://localhost:8080, load order `SO-104587` and scan or type `8935000100011`, `8935000200018`, `8935000300015`.
Demo data: `be/app/seed.sql` (loaded while `SEED_DEMO=true`).

## Tests

```
docker compose exec be python -m app.server check     # BE against the real DB
cd src/barcode_chatbot/agent && uv run pytest         # agent (no LLM or BE needed)
node src/barcode_chatbot/fe/core.test.js              # scanner logic
```
