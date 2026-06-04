#!/usr/bin/env python3
"""Standalone OPSWAT diagram API.

This service turns either structured diagram payloads or plain-text use-case
descriptions into OPSWAT-style SVG diagrams.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


PROJECT = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(PROJECT / ".env")


def project_path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else PROJECT / path


DIAGRAM_OUTPUT_DIR = project_path_from_env("DIAGRAM_OUTPUT_DIR", PROJECT / "outputs" / "diagrams")
DIAGRAM_SCRIPT = PROJECT / "scripts" / "diagram_generator.py"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")


def load_diagram_module() -> Any:
    spec = importlib.util.spec_from_file_location("diagram_generator", DIAGRAM_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {DIAGRAM_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diagram_generator = load_diagram_module()

app = FastAPI(title="OPSWAT Diagram API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DiagramTextRequest(BaseModel):
    description: str = Field(..., min_length=10, max_length=12000)
    account_name: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=180)
    model: Optional[str] = Field(default=None, max_length=120)
    mode: Literal["auto", "claude", "heuristic"] = "auto"
    include_purdue: bool = False
    include_svg: bool = False


def safe_filename(filename: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.(svg|json)", filename))


def read_json_from_model(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Claude response did not contain JSON")
    return json.loads(candidate[start : end + 1])


def detected_products(description: str) -> list[dict[str, str]]:
    text = description.lower()
    product_rules = [
        ("kiosk", "MetaDefender Kiosk"),
        ("managed file transfer", "MetaDefender Managed File Transfer"),
        ("mft", "MetaDefender Managed File Transfer"),
        ("core", "MetaDefender Core"),
        ("data diode", "OPSWAT Data Diode"),
        ("diode", "OPSWAT Data Diode"),
        ("drive", "MetaDefender Drive"),
        ("email", "MetaDefender Email Security"),
        ("media firewall", "MetaDefender Media Firewall"),
    ]
    products: list[dict[str, str]] = []
    seen: set[str] = set()
    for keyword, product in product_rules:
        if keyword in text and product not in seen:
            products.append({"product": product})
            seen.add(product)
    if not products:
        products = [{"product": "MetaDefender Core"}, {"product": "MetaDefender Managed File Transfer"}]
    if "usb" in text and not any("Kiosk" in product["product"] for product in products):
        products.insert(0, {"product": "MetaDefender Kiosk"})
    return products


def infer_title(description: str, supplied_title: str) -> str:
    if supplied_title.strip():
        return supplied_title.strip()
    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
    if 8 <= len(first_line) <= 110:
        return first_line
    text = re.sub(r"\s+", " ", description).strip()
    if "usb" in text.lower() and "kiosk" in text.lower():
        return "Secure removable media transfer into OT"
    if "supplier" in text.lower() or "contractor" in text.lower():
        return "Secure supplier file exchange"
    return "Secure data flow use case"


def wants_quarantine(description: str) -> bool:
    text = description.lower()
    return any(word in text for word in ["quarantine", "blocked", "malicious", "reject", "rejected", "threat", "infected"])


def infer_zones(description: str) -> list[dict[str, Any]]:
    text = description.lower()
    if "two zone" in text or ("it side" in text and "ot side" in text):
        return [
            {"rank": 0, "label": "IT SIDE", "x": 60, "y": 150, "width": 610, "height": 440, "color": "#2563EB"},
            {"rank": 1, "label": "OT SIDE", "x": 700, "y": 150, "width": 500, "height": 440, "color": "#16A34A"},
        ]
    if "low side" in text and ("high side" in text or "high-side" in text):
        return [
            {"rank": 0, "label": "LOW SIDE", "x": 60, "y": 150, "width": 610, "height": 440, "color": "#16A34A"},
            {"rank": 1, "label": "HIGH SIDE", "x": 700, "y": 150, "width": 500, "height": 440, "color": "#E8842A"},
        ]
    return []


def build_simple_flow_payload(request: DiagramTextRequest, products: list[dict[str, str]], title: str) -> dict[str, Any]:
    description = request.description.strip()
    zones = infer_zones(description)
    has_quarantine = wants_quarantine(description)
    destination_label = "High-side\nMFT" if ("high side" in description.lower() or "high-side" in description.lower()) else "Destination\nMFT"
    if "nas" in description.lower():
        destination_label = "NAS /\nStorage"

    nodes: list[dict[str, Any]] = [
        {"id": "source", "label": "USB Device /\nRemovable Media", "kind": "source", "x": 110, "y": 330},
        {"id": "kiosk", "label": "Kiosk", "kind": "product", "product_type": "kiosk", "x": 320, "y": 282, "variant": "active"},
        {"id": "core", "label": "Core", "kind": "product", "product_type": "core", "x": 320, "y": 438, "variant": "active"},
        {"id": "verdict", "label": "Clean\nVerdict", "kind": "verdict", "x": 555, "y": 320},
        {"id": "mft", "label": "MFT", "kind": "product", "product_type": "mft", "x": 800, "y": 292, "variant": "active"},
        {"id": "destination", "label": destination_label, "kind": "entity", "icon": "server-rack", "x": 1015, "y": 318},
    ]
    flows: list[dict[str, Any]] = [
        {"from": "source", "to": "kiosk", "role": "ingress", "label": "scan media", "glyph": "media_red"},
        {"from": "kiosk", "to": "core", "role": "primary", "label": "", "route": "side", "bidirectional": True},
        {"from": "core", "to": "verdict", "role": "egress", "label": "approved", "glyph": "file_green"},
        {"from": "verdict", "to": "mft", "role": "egress", "label": "copy clean file", "glyph": "file_green"},
        {"from": "mft", "to": "destination", "role": "primary", "label": "deliver"},
    ]
    if has_quarantine:
        nodes.append({"id": "quarantine", "label": "Quarantine", "kind": "quarantine", "x": 525, "y": 450})
        flows.append({"from": "core", "to": "quarantine", "role": "ingress", "label": "blocked", "indicator": "quarantine"})

    return {
        "title": title,
        "subtitle": "",
        "title_color": "#2563EB",
        "figure_caption": f"Figure: {title}",
        "account_name": request.account_name,
        "pattern": "custom",
        "include_purdue": request.include_purdue,
        "include_quarantine": has_quarantine,
        "zones": zones,
        "nodes": nodes,
        "flows": flows,
        "use_case": {
            "title": title,
            "account_trigger": description[:900],
            "business_value": description[:900],
            "opswat_products": products,
        },
        "products": products,
    }


def heuristic_payload(request: DiagramTextRequest) -> dict[str, Any]:
    description = request.description.strip()
    products = detected_products(description)
    title = infer_title(description, request.title)
    text = description.lower()
    if ("kiosk" in text and "core" in text and "mft" in text) or infer_zones(description):
        return build_simple_flow_payload(request, products, title)
    return {
        "title": title,
        "subtitle": "SECURING THE FLOW OF DATA",
        "account_name": request.account_name,
        "pattern": "auto",
        "include_purdue": request.include_purdue,
        "include_quarantine": wants_quarantine(description),
        "zones": infer_zones(description),
        "use_case": {
            "title": title,
            "account_trigger": description[:900],
            "business_value": description[:900],
            "opswat_products": products,
        },
        "products": products,
    }


def claude_payload(request: DiagramTextRequest) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError("The anthropic package is not installed") from exc

    client = Anthropic(api_key=api_key)
    model = request.model or DEFAULT_MODEL
    prompt = {
        "account_name": request.account_name,
        "title_hint": request.title,
        "description": request.description,
        "allowed_patterns": ["auto", "custom", "removable_media", "secure_file_exchange", "cross_domain"],
        "preferred_products": [
            "MetaDefender Kiosk",
            "MetaDefender Core",
            "MetaDefender Managed File Transfer",
            "OPSWAT Data Diode",
            "MetaDefender Drive",
            "MetaDefender Email Security",
            "MetaDefender Media Firewall",
        ],
    }
    response = client.messages.create(
        model=model,
        max_tokens=2500,
        system=(
            "You convert plain-English industrial cybersecurity use cases into a compact JSON payload "
            "for an OPSWAT SVG diagram generator. Return only valid JSON. Do not invent unavailable "
            "OPSWAT product names. Prefer products explicitly mentioned in the text, then infer only "
            "obvious OPSWAT products from the use case. Do not force Purdue zones, four-zone models, "
            "or quarantine paths unless the user asks for them or the text clearly discusses malicious "
            "files being blocked. If the user defines two zones, return exactly those two zones. For "
            "simple use cases, prefer pattern=custom with explicit nodes and flows."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    "Create this JSON shape:\n"
                    "{\n"
                    '  "title": "short diagram title",\n'
                    '  "subtitle": "optional subtitle, empty for simple use-case diagrams",\n'
                    '  "title_color": "#2563EB",\n'
                    '  "figure_caption": "Figure: short caption",\n'
                    '  "account_name": "optional account name",\n'
                    '  "pattern": "auto|custom|removable_media|secure_file_exchange|cross_domain",\n'
                    '  "include_purdue": false,\n'
                    '  "include_quarantine": false,\n'
                    '  "zones": [{"label": "IT SIDE", "x": 60, "y": 150, "width": 610, "height": 440, "color": "#2563EB"}],\n'
                    '  "nodes": [{"id": "kiosk", "label": "Kiosk", "kind": "product", "product_type": "kiosk", "x": 310, "y": 315, "variant": "active"}],\n'
                    '  "flows": [{"from": "kiosk", "to": "core", "role": "primary", "label": "scan"}],\n'
                    '  "use_case": {\n'
                    '    "title": "use case title",\n'
                    '    "account_trigger": "why this matters",\n'
                    '    "business_value": "outcome in one sentence",\n'
                    '    "opswat_products": [{"product": "OPSWAT product name"}]\n'
                    "  },\n"
                    '  "products": [{"product": "OPSWAT product name"}]\n'
                    "}\n\n"
                    f"Input:\n{json.dumps(prompt, indent=2)}"
                ),
            }
        ],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    payload = read_json_from_model(text)
    payload.setdefault("subtitle", "SECURING THE FLOW OF DATA")
    payload.setdefault("pattern", "auto")
    payload["include_purdue"] = request.include_purdue
    payload.setdefault("include_quarantine", False)
    if request.account_name and not payload.get("account_name"):
        payload["account_name"] = request.account_name
    return payload


def payload_from_text(request: DiagramTextRequest) -> tuple[dict[str, Any], str]:
    if request.mode == "heuristic":
        return heuristic_payload(request), "heuristic"
    try:
        return claude_payload(request), "claude"
    except Exception:
        if request.mode == "claude":
            raise
        return heuristic_payload(request), "heuristic"


def create_diagram(payload: dict[str, Any], include_svg: bool = False) -> dict[str, Any]:
    artifact = diagram_generator.generate_diagram(payload)
    json_path, svg_path = diagram_generator.write_diagram(artifact, DIAGRAM_OUTPUT_DIR)
    response = {
        "id": artifact.diagram_id,
        "spec": artifact.spec,
        "json_url": f"/api/diagrams/{json_path.name}",
        "svg_url": f"/api/diagrams/{svg_path.name}",
    }
    if include_svg:
        response["svg"] = artifact.svg
    return response


def create_diagram_from_text(request: DiagramTextRequest) -> dict[str, Any]:
    payload, interpreter = payload_from_text(request)
    result = create_diagram(payload, include_svg=request.include_svg)
    result["interpreter"] = interpreter
    result["input_payload"] = payload
    return result


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "OPSWAT Diagram API",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": DEFAULT_MODEL,
        "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "outputs": str(DIAGRAM_OUTPUT_DIR),
    }


@app.post("/api/diagrams")
async def generate_diagram(payload: dict[str, Any] = Body(...), include_svg: bool = False) -> dict[str, Any]:
    try:
        return await run_in_threadpool(create_diagram, payload, include_svg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/diagrams/from-text")
async def generate_diagram_from_text(payload: DiagramTextRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(create_diagram_from_text, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/diagrams/from-file")
async def generate_diagram_from_file(
    file: UploadFile = File(...),
    account_name: str = Form(default=""),
    title: str = Form(default=""),
    mode: Literal["auto", "claude", "heuristic"] = Form(default="auto"),
    include_purdue: bool = Form(default=True),
    include_svg: bool = Form(default=False),
    model: Optional[str] = Form(default=None),
) -> dict[str, Any]:
    if file.content_type and file.content_type not in {"text/plain", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Upload must be a plain text file")
    raw = await file.read()
    try:
        description = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Upload must be UTF-8 text") from exc
    payload = DiagramTextRequest(
        description=description,
        account_name=account_name,
        title=title,
        mode=mode,
        include_purdue=include_purdue,
        include_svg=include_svg,
        model=model,
    )
    try:
        return await run_in_threadpool(create_diagram_from_text, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/diagrams/{filename}")
def get_diagram(filename: str) -> FileResponse:
    if not safe_filename(filename):
        raise HTTPException(status_code=404, detail="Diagram not found")
    path = DIAGRAM_OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Diagram not found")
    if filename.endswith(".svg"):
        return FileResponse(path, media_type="image/svg+xml")
    return FileResponse(path, media_type="application/json")
