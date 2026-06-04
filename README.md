# OPSWAT Diagram API

Standalone FastAPI service for generating OPSWAT-style data-flow diagrams from either structured diagram payloads or plain-text use-case descriptions.

The service returns SVG first because SVG is easy to preview in a browser, embed in apps, or export later into slide decks.

## What It Does

- `POST /api/diagrams` accepts an existing structured diagram payload.
- `POST /api/diagrams/from-text` accepts a plain-text use case and generates a diagram payload.
- `POST /api/prompt-helper` turns lightweight form fields into a strong reusable prompt.
- `POST /api/diagrams/from-helper` builds the prompt and generates either SVG or GPT Image output in one call.
- `POST /api/image-diagrams/from-text` experimentally generates a PNG diagram with OpenAI image generation, always attaching local reference diagrams by default.
- `GET /api/image-diagrams/references` lists the reference diagrams that will be attached to image-generation prompts.
- `POST /api/diagrams/from-file` accepts an uploaded UTF-8 `.txt` use-case file.
- `GET /api/diagrams/{id}.svg` returns the generated SVG.
- `GET /api/diagrams/{id}.json` returns the normalized diagram spec.

When `ANTHROPIC_API_KEY` is configured, the text endpoint uses Claude to interpret the use case. If it is not configured, `mode=auto` falls back to a deterministic heuristic parser so the service still works for testing.

The generator is intentionally flexible:

- It does not force Purdue or four-zone models by default.
- If a prompt defines two zones, such as `IT Side` and `OT Side`, it returns those two zones.
- If no zones are requested, the spec can contain no zone guides.
- Quarantine paths are included only when the prompt mentions blocking, malicious files, rejected files, threats, or quarantine.
- Claude can return explicit `nodes`, `flows`, and `zones` for bespoke layouts.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Set `ANTHROPIC_API_KEY` in `.env` for Claude-supported text interpretation.

For GPT Image output, either set `OPENAI_API_KEY` in `.env` for server-side testing or send `openai_api_key` in the request. The helper UI has an API-key field for per-request use.

## Run

```bash
.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8020 --reload
```

Open:

```text
http://127.0.0.1:8020/docs
```

For the lightweight prompt helper UI, open:

```text
http://127.0.0.1:8020/helper
```

The helper UI includes an **Output method** selector:

- `SVG architecture` for deterministic, editable SVG output.
- `GPT Image` for a polished PNG render using the local reference diagrams and product icons.

## Plain Text Example

```bash
curl -s -X POST http://127.0.0.1:8020/api/diagrams/from-text \
  -H "Content-Type: application/json" \
  -d @examples/from_text.json
```

## Prompt Helper Example

```bash
curl -s -X POST http://127.0.0.1:8020/api/prompt-helper \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Use Case 1 - Secure USB Ingest to NAS",
    "use_case": "Engineers bring USB removable media into two sites. Files must be scanned by MetaDefender Kiosk and MetaDefender Core before clean files are moved through MetaDefender Managed File Transfer to each site NAS.",
    "lanes": ["London MSOC", "IBC"],
    "flow_steps": [
      "USB Device / Removable Media",
      "MetaDefender Kiosk",
      "MetaDefender Core",
      "Clean verdict",
      "MetaDefender Managed File Transfer",
      "Site NAS"
    ],
    "products": [
      "MetaDefender Kiosk",
      "MetaDefender Core",
      "MetaDefender Managed File Transfer"
    ],
    "show_opswat_scope": true,
    "show_lanes": true,
    "show_outside_scope_sync": true
  }'
```

Generate directly from the helper as a GPT Image PNG:

```bash
curl -s -X POST http://127.0.0.1:8020/api/diagrams/from-helper \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Vendor / OEM Media Release to OT",
    "use_case": "A Vendor / OEM Engineer brings USB / CD / Peripheral Media containing firmware updates, configuration tools, diagnostic packages, and patches. The media passes through MetaDefender Kiosk and MetaDefender Core for scanning, CDR, and policy checks. A clean verdict crosses an IT / OT airgap, then either MetaDefender Media Firewall or MetaDefender Media Validation enforces clean release to OT devices.",
    "flow_steps": [
      "Vendor / OEM Engineer",
      "USB / CD / Peripheral Media",
      "MetaDefender Kiosk",
      "MetaDefender Core",
      "Clean Verdict",
      "IT / OT AIRGAP",
      "MetaDefender Media Firewall OR MetaDefender Media Validation",
      "OT Devices"
    ],
    "products": [
      "MetaDefender Kiosk",
      "MetaDefender Core",
      "MetaDefender Media Firewall",
      "MetaDefender Media Validation"
    ],
    "show_air_gap": true,
    "output_method": "gpt_image",
    "image_quality": "high",
    "openai_api_key": "sk-..."
  }'
```

## Experimental Image Renderer

Reference diagrams are loaded from:

```text
assets/references/diagrams
```

The image renderer automatically attaches those reference diagrams to every image-generation request, then adds relevant product icon references when the prompt mentions products such as Kiosk, Core, MFT, Media Firewall, or Media Validation.

List available references:

```bash
curl -s http://127.0.0.1:8020/api/image-diagrams/references
```

Generate a PNG concept render:

```bash
curl -s -X POST http://127.0.0.1:8020/api/image-diagrams/from-text \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Vendor / OEM Media Release to OT",
    "prompt": "Create an OPSWAT-style light technical architecture diagram showing a Vendor / OEM Engineer bringing USB / CD / Peripheral Media through MetaDefender Kiosk, MetaDefender Core, a Clean Verdict, an IT / OT AIRGAP, then either MetaDefender Media Firewall OR MetaDefender Media Validation before release to OT Devices (PLCs, RTUs, etc.). Match the attached reference diagram style closely.",
    "size": "1536x1024",
    "quality": "high",
    "openai_api_key": "sk-..."
  }'
```

Generated PNGs and metadata are written to:

```text
outputs/image_diagrams
```

Two-zone example:

```bash
curl -s -X POST http://127.0.0.1:8020/api/diagrams/from-text \
  -H "Content-Type: application/json" \
  -d '{
    "account_name": "Example Account",
    "title": "Low-side media scanning to high-side MFT",
    "mode": "auto",
    "description": "Generate a diagram showing a Low Side MDKIOSK that scans files with MDCORE and if approved, copies them to a high-side MFT. The diagram shows two zones, IT side and OT side."
  }'
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
