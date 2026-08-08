# Weather pipeline for Lakebase

## Data source
- National Weather Service (NWS) API, free public US government data
- No API key required
- This app resolves requested locations, then pulls alerts and forecasts for each location

## Why NWS
- Real-time and authoritative
- Covers the full US
- Good fit for a public demo and location-based risk search

## Schema choices
- `weather_documents.id` is `TEXT` because upstream weather records are best handled as stable string IDs
- `weather_embeddings.embedding` uses `vector(384)` to match `all-MiniLM-L6-v2`
- `payload JSONB` stores raw provenance from the source API
- `source_type` distinguishes `alert` vs `forecast`

## Chunking
- Sliding windows of 800 characters with 100 characters overlap
- That fits typical NWS narrative sizes, which are often around 500 to 2000 characters

## How to run
1. `POST /weather/sync` with a body like `{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}`
2. `POST /weather/embed`
3. `POST /weather/search` with a body like `{"query": "risk of flooding near rivers", "top_k": 5}`

If no locations are provided to `/weather/sync`, the app defaults to a few major cities.

## Limitations
- The app environment must include `sentence-transformers` and `torch`
- NWS APIs can rate limit aggressive polling, so batch requests thoughtfully
- City/state inputs require geocoding before the NWS `/points/{lat},{lon}` lookup
