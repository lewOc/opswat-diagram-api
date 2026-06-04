#!/usr/bin/env python3
"""Standalone OPSWAT diagram API.

This service turns either structured diagram payloads or plain-text use-case
descriptions into OPSWAT-style SVG diagrams.
"""

from __future__ import annotations

import importlib.util
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
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
IMAGE_OUTPUT_DIR = project_path_from_env("IMAGE_OUTPUT_DIR", PROJECT / "outputs" / "image_diagrams")
DIAGRAM_SCRIPT = PROJECT / "scripts" / "diagram_generator.py"
STATIC_DIR = PROJECT / "static"
REFERENCE_DIAGRAM_DIR = project_path_from_env("REFERENCE_DIAGRAM_DIR", PROJECT / "assets" / "references" / "diagrams")
PRODUCT_ICON_DIR = project_path_from_env("PRODUCT_ICON_DIR", PROJECT / "assets" / "product_icons")
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
DEFAULT_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1.5")
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


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


class PromptHelperRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=180)
    use_case: str = Field(..., min_length=10, max_length=4000)
    account_name: str = Field(default="", max_length=200)
    lanes: list[str] = Field(default_factory=list, max_length=8)
    flow_steps: list[str] = Field(default_factory=list, max_length=18)
    products: list[str] = Field(default_factory=list, max_length=12)
    show_opswat_scope: bool = True
    show_lanes: bool = True
    show_quarantine: bool = False
    show_air_gap: bool = False
    show_outside_scope_sync: bool = False
    include_purdue: bool = False
    extra_instructions: str = Field(default="", max_length=1500)
    mode: Literal["auto", "claude", "heuristic"] = "auto"
    model: Optional[str] = Field(default=None, max_length=120)
    include_svg: bool = False


class ImageDiagramRequest(BaseModel):
    prompt: str = Field(..., min_length=20, max_length=32000)
    title: str = Field(default="OPSWAT image diagram", max_length=180)
    account_name: str = Field(default="", max_length=200)
    model: Optional[str] = Field(default=None, max_length=120)
    size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1536x1024"
    quality: Literal["low", "medium", "high", "auto"] = "high"
    output_format: Literal["png", "jpeg", "webp"] = "png"
    include_reference_diagrams: bool = True
    include_product_icons: bool = True
    max_reference_diagrams: int = Field(default=8, ge=0, le=16)
    max_product_icons: int = Field(default=8, ge=0, le=16)


def clean_list(values: list[str], limit: int = 80) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        item = re.sub(r"\s+", " ", str(value)).strip()
        if item:
            cleaned.append(item[:limit])
    return cleaned


def build_helper_prompt(request: PromptHelperRequest) -> tuple[str, list[str]]:
    lanes = clean_list(request.lanes, 80)
    flow_steps = clean_list(request.flow_steps, 120)
    products = clean_list(request.products, 120)
    warnings: list[str] = []
    if len(flow_steps) > 10:
        warnings.append("This flow has many steps; the diagram may need multiple lanes or a future multi-slide export.")
    if len(lanes) > 4:
        warnings.append("More than four lanes can become dense on one 1280x720 canvas.")
    if not flow_steps:
        warnings.append("No flow steps were provided, so Claude will infer the flow from the use-case description.")
    if not products:
        warnings.append("No products were selected, so Claude will infer likely OPSWAT products.")

    lane_text = "\n".join(f"- {lane}" for lane in lanes) if lanes else "- No explicit lanes. Use the simplest layout that fits the use case."
    flow_text = " -> ".join(flow_steps) if flow_steps else "Infer the clearest left-to-right flow from the use-case description."
    product_text = "\n".join(f"- {product}" for product in products) if products else "- Infer from the description, using only real OPSWAT product names."
    show_items = []
    if request.show_opswat_scope:
        show_items.append("a dashed rounded OPSWAT scope boundary around OPSWAT-controlled components")
    if request.show_lanes and lanes:
        show_items.append("the requested lanes as horizontal dashed lane containers")
    if request.show_air_gap:
        show_items.append("an air gap where the flow crosses between security domains")
    if request.show_outside_scope_sync:
        show_items.append("outside-scope synchronization as a dashed annotation, not as a heavy normal data-flow line")
    if request.show_quarantine:
        show_items.append("a quarantine or blocked-file path")
    else:
        show_items.append("no quarantine or blocked-file path unless the use case explicitly requires it")
    show_text = "\n".join(f"- {item}" for item in show_items)

    extra = request.extra_instructions.strip()
    extra_text = f"\nAdditional instructions:\n{extra}\n" if extra else ""
    prompt = f"""Create an OPSWAT-style light diagram titled "{request.title.strip()}".

Use case:
{request.use_case.strip()}

Lanes or repeated site rows:
{lane_text}

Main left-to-right flow:
{flow_text}

Products involved:
{product_text}

Show:
{show_text}
{extra_text}
Design rules:
- Use a white canvas, OPSWAT blue title text, thin dark-navy arrows, dashed grey scope boundaries, and restrained grey labels.
- Keep normal left-to-right data flows as straight horizontal arrows wherever possible.
- Avoid unnecessary right-angle routing unless a connector must route around a node.
- Put OPSWAT product labels underneath product icons, not inside large cards.
- Use simple white cards for external sources and destinations.
- Keep connector labels minimal; only show labels that materially clarify the diagram.
- Keep every node and zone inside a 1280x720 canvas.
- Do not add Purdue levels, extra zones, extra products, or quarantine paths unless requested above.
"""
    return prompt.strip(), warnings


def helper_to_text_request(request: PromptHelperRequest) -> tuple[DiagramTextRequest, list[str], str]:
    prompt, warnings = build_helper_prompt(request)
    return (
        DiagramTextRequest(
            description=prompt,
            account_name=request.account_name,
            title=request.title,
            model=request.model,
            mode=request.mode,
            include_purdue=request.include_purdue,
            include_svg=request.include_svg,
        ),
        warnings,
        prompt,
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "diagram"


def image_asset_paths(directory: Path, limit: int) -> list[Path]:
    if limit <= 0 or not directory.exists():
        return []
    paths = [
        path
        for path in sorted(directory.iterdir(), key=lambda item: item.name.lower())
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
    ]
    return paths[:limit]


PRODUCT_ICON_RULES = [
    (("media validation", "validation"), "mobile_validation.png"),
    (("media firewall", "firewall"), "media_firewall.png"),
    (("managed file transfer", "mft"), "managed_file_transfer_mft.png"),
    (("kiosk",), "kiosk_tower.png"),
    (("core", "scanning", "malware scan"), "on_premises.png"),
    (("data diode", "diode"), "transfer_guard.png"),
    (("drive",), "drive.png"),
    (("email",), "email_Security.png"),
    (("storage", "nas"), "secure_storage.png"),
    (("ot device", "plc", "rtu", "hmi"), "ot_Security.png"),
]


def product_icon_reference_paths(prompt: str, limit: int) -> list[Path]:
    if limit <= 0:
        return []
    text = prompt.lower()
    paths: list[Path] = []
    seen: set[Path] = set()
    for keywords, filename in PRODUCT_ICON_RULES:
        if not any(keyword in text for keyword in keywords):
            continue
        path = PRODUCT_ICON_DIR / filename
        if path.exists() and path not in seen:
            paths.append(path)
            seen.add(path)
        if len(paths) >= limit:
            break
    return paths


def build_image_generation_prompt(request: ImageDiagramRequest, reference_paths: list[Path], icon_paths: list[Path]) -> str:
    reference_names = "\n".join(f"- {path.name}" for path in reference_paths) or "- None"
    icon_names = "\n".join(f"- {path.name}" for path in icon_paths) or "- None"
    account = f"\nAccount/project context: {request.account_name.strip()}" if request.account_name.strip() else ""
    return f"""Create a polished OPSWAT-style light technical architecture diagram.

Title: {request.title.strip()}{account}

Use the attached manual diagrams as visual style references. Match their clean white background, dark navy linework, OPSWAT blue product treatments, balanced spacing, readable labels, and sales-engineering architecture style.

Reference diagrams attached:
{reference_names}

Product/icon references attached:
{icon_names}

Diagram brief:
{request.prompt.strip()}

Hard requirements:
- Keep all labels legible and correctly spelled.
- Use real OPSWAT product names exactly as provided in the brief.
- Prefer straight thin navy arrows for left-to-right flow.
- Use restrained right-angle connectors only where split/merge routing is necessary.
- Use white cards for external people, media, and destinations.
- Use the attached OPSWAT product icon references where relevant; do not invent unrelated product icons.
- Do not add threat, quarantine, malware, or blocked-file paths unless explicitly requested.
- Produce one complete presentation-ready diagram, not a collage of the references.
"""


def create_image_diagram(request: ImageDiagramRequest) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The openai package is not installed. Run .venv/bin/pip install -r requirements.txt") from exc

    reference_paths = image_asset_paths(REFERENCE_DIAGRAM_DIR, request.max_reference_diagrams) if request.include_reference_diagrams else []
    remaining_slots = max(0, 16 - len(reference_paths))
    icon_limit = min(request.max_product_icons, remaining_slots)
    icon_paths = product_icon_reference_paths(request.prompt, icon_limit) if request.include_product_icons else []
    image_paths = reference_paths + icon_paths
    if not image_paths:
        raise RuntimeError(f"No reference images found in {REFERENCE_DIAGRAM_DIR}")

    client = OpenAI(api_key=api_key)
    prompt = build_image_generation_prompt(request, reference_paths, icon_paths)
    opened_files = [path.open("rb") for path in image_paths]
    try:
        result = client.images.edit(
            model=request.model or DEFAULT_IMAGE_MODEL,
            image=opened_files,
            prompt=prompt,
            size=request.size,
            quality=request.quality,
            output_format=request.output_format,
            n=1,
        )
    finally:
        for file_handle in opened_files:
            file_handle.close()

    data = result.data[0]
    encoded = getattr(data, "b64_json", None)
    if not encoded:
        raise RuntimeError("OpenAI image response did not include b64_json")
    image_bytes = base64.b64decode(encoded)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    diagram_id = f"{slugify(request.title)}-{stamp}"
    IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = IMAGE_OUTPUT_DIR / f"{diagram_id}.{request.output_format}"
    metadata_path = IMAGE_OUTPUT_DIR / f"{diagram_id}.json"
    image_path.write_bytes(image_bytes)
    metadata = {
        "id": diagram_id,
        "model": request.model or DEFAULT_IMAGE_MODEL,
        "size": request.size,
        "quality": request.quality,
        "output_format": request.output_format,
        "prompt": prompt,
        "reference_diagrams": [path.name for path in reference_paths],
        "product_icons": [path.name for path in icon_paths],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return {
        **metadata,
        "image_url": f"/api/image-diagrams/{image_path.name}",
        "json_url": f"/api/image-diagrams/{metadata_path.name}",
    }


def safe_filename(filename: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.(svg|json)", filename))


def safe_image_filename(filename: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.(png|jpg|jpeg|webp|json)", filename))


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
    if any(
        phrase in text
        for phrase in [
            "no quarantine",
            "without quarantine",
            "do not add quarantine",
            "do not include quarantine",
            "no blocked-file path",
            "no blocked file path",
        ]
    ):
        return False
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
            " Use supported node kinds only: source, product, verdict, entity, actor, zone, quarantine. "
            "Use verdict for document/check nodes and entity for NAS, servers, users, or external systems. "
            "Keep connector labels off unless they are essential; use show_label=true only for labels the "
            "diagram must visibly display. Keep every zone and node fully inside the 1280x720 canvas: "
            "use x <= 1120 for normal nodes, y <= 600 for product nodes, y <= 620 for compact nodes, "
            "and keep zone heights within the bottom margin."
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
                    '  "flows": [{"from": "kiosk", "to": "core", "role": "bidirectional", "label": "", "show_label": false}],\n'
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
        "helper": "/helper",
        "health": "/api/health",
        "image_references": "/api/image-diagrams/references",
    }


@app.get("/helper")
def prompt_helper_page() -> FileResponse:
    path = STATIC_DIR / "prompt_helper.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Prompt helper not found")
    return FileResponse(path, media_type="text/html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": DEFAULT_MODEL,
        "image_model": DEFAULT_IMAGE_MODEL,
        "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "outputs": str(DIAGRAM_OUTPUT_DIR),
        "image_outputs": str(IMAGE_OUTPUT_DIR),
        "reference_diagrams": str(REFERENCE_DIAGRAM_DIR),
    }


@app.post("/api/prompt-helper")
async def prompt_helper(payload: PromptHelperRequest) -> dict[str, Any]:
    prompt, warnings = build_helper_prompt(payload)
    text_payload = DiagramTextRequest(
        description=prompt,
        account_name=payload.account_name,
        title=payload.title,
        model=payload.model,
        mode=payload.mode,
        include_purdue=payload.include_purdue,
        include_svg=payload.include_svg,
    )
    return {
        "prompt": prompt,
        "warnings": warnings,
        "suggested_payload": text_payload.model_dump(),
    }


@app.post("/api/diagrams/from-helper")
async def generate_diagram_from_helper(payload: PromptHelperRequest) -> dict[str, Any]:
    text_request, warnings, prompt = helper_to_text_request(payload)
    try:
        result = await run_in_threadpool(create_diagram_from_text, text_request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    result["helper_prompt"] = prompt
    result["helper_warnings"] = warnings
    return result


@app.get("/api/image-diagrams/references")
def list_image_references() -> dict[str, Any]:
    diagrams = image_asset_paths(REFERENCE_DIAGRAM_DIR, 100)
    return {
        "reference_dir": str(REFERENCE_DIAGRAM_DIR),
        "references": [
            {
                "filename": path.name,
                "size_bytes": path.stat().st_size,
            }
            for path in diagrams
        ],
    }


@app.post("/api/image-diagrams/from-text")
async def generate_image_diagram_from_text(payload: ImageDiagramRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(create_image_diagram, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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


@app.get("/api/image-diagrams/{filename}")
def get_image_diagram(filename: str) -> FileResponse:
    if not safe_image_filename(filename):
        raise HTTPException(status_code=404, detail="Image diagram not found")
    path = IMAGE_OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image diagram not found")
    if filename.endswith(".json"):
        return FileResponse(path, media_type="application/json")
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)
