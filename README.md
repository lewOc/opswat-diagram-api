# OPSWAT Diagram API

Standalone FastAPI service for generating OPSWAT-style data-flow diagrams from either structured diagram payloads or plain-text use-case descriptions.

The service returns SVG first because SVG is easy to preview in a browser, embed in apps, or export later into slide decks.

## What It Does

- `POST /api/diagrams` accepts an existing structured diagram payload.
- `POST /api/diagrams/from-text` accepts a plain-text use case and generates a diagram payload.
- `POST /api/diagrams/from-file` accepts an uploaded UTF-8 `.txt` use-case file.
- `GET /api/diagrams/{id}.svg` returns the generated SVG.
- `GET /api/diagrams/{id}.json` returns the normalized diagram spec.

When `ANTHROPIC_API_KEY` is configured, the text endpoint uses Claude to interpret the use case. If it is not configured, `mode=auto` falls back to a deterministic heuristic parser so the service still works for testing.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Set `ANTHROPIC_API_KEY` in `.env` for Claude-supported text interpretation.

## Run

```bash
.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8020 --reload
```

Open:

```text
http://127.0.0.1:8020/docs
```

## Plain Text Example

```bash
curl -s -X POST http://127.0.0.1:8020/api/diagrams/from-text \
  -H "Content-Type: application/json" \
  -d @examples/from_text.json
```

## File Upload Example

```bash
curl -s -X POST http://127.0.0.1:8020/api/diagrams/from-file \
  -F "file=@examples/removable_media.txt" \
  -F "account_name=SSE" \
  -F "mode=auto"
```

## Response Shape

```json
{
  "id": "secure-removable-media-transfer-20260604T120000Z",
  "interpreter": "claude",
  "spec": {},
  "svg_url": "/api/diagrams/secure-removable-media-transfer-20260604T120000Z.svg",
  "json_url": "/api/diagrams/secure-removable-media-transfer-20260604T120000Z.json"
}
```

Set `include_svg=true` if a caller needs the full SVG returned inline. The default response keeps the payload small and returns reusable URLs.

## Notes

- Generated files are written to `outputs/diagrams`.
- Icon assets are stored in `assets/product_icons` and `assets/other_icons`.
- For production, run behind a reverse proxy, protect the API, and persist or back up the output directory.
