"""Combine the parsed document and views into one agent-ready spec.json."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any

from .markdown_parser import parse_markdown_document
from .plantuml_parser import parse_architecture_views


def _diagrams(views: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [d for v in views["views"] for d in v["diagrams"] if d["kind"] == kind]


def _derive_digest(doc: dict[str, Any], views: dict[str, Any]) -> dict[str, Any]:
    """A short cross-view summary: the facts the code must honour."""
    classes = {c["name"]: c for d in _diagrams(views, "class") for c in d["classes"]}
    relations = [r for d in _diagrams(views, "class") for r in d["relations"]]
    use_cases = [
        {"name": uc["label"], "actors": uc.get("actors", [])}
        for d in _diagrams(views, "usecase") for uc in d["use_cases"]
    ]
    state_machines = [
        {"diagram": d["name"], "states": d["states"], "initial": d["initial_state"],
         "transitions": [f'{t["from"]} -> {t["to"]}' + (f' [{t["trigger"]}]' if t.get("trigger") else "")
                         for t in d["transitions"]]}
        for d in _diagrams(views, "state")
    ]
    scenarios = []
    for kind in ("sequence", "collaboration"):
        for d in _diagrams(views, kind):
            scenarios.append({
                "diagram": d["name"],
                "scenario": d.get("scenario", ""),
                "steps": [f'{m["from"]} -> {m["to"]}: {m["message"]}' for m in d["messages"]],
            })
    packages = [
        {"package": p["name"], "classes": p["classes"]}
        for d in _diagrams(views, "package") for p in d["packages"]
    ]
    deployment = [
        {"diagram": d["name"], "elements": [e["name"] for e in d["elements"]],
         "links": [f'{c["from"]} -- {c["to"]}' for c in d["connections"]]}
        for kind in ("deployment", "container", "component") for d in _diagrams(views, kind)
    ]
    activities = [{"diagram": d["name"], "actions": d["actions"]} for d in _diagrams(views, "activity")]
    return {
        "system_name": _guess_system_name(doc),
        "entities": [
            {"name": c["name"],
             "attributes": [f'{a["name"]}: {a["type"]}' for a in c["attributes"]],
             "operations": [
                 f'{m["name"]}({", ".join(p["name"] + (": " + p["type"] if p["type"] else "") for p in m["params"])}): {m["returns"]}'
                 for m in c["methods"]]}
            for c in classes.values()
        ],
        "relations": [
            {k: r[k] for k in ("from", "to", "kind", "arrow", "diamond_at", "label") if k in r}
            for r in relations
        ],
        "use_cases": use_cases,
        "state_machines": state_machines,
        "activities": activities,
        "scenarios": scenarios,
        "packages": packages,
        "components_and_deployment": deployment,
        "components_from_document": doc["components"],
        "requirements": doc["requirements"],
    }


def _guess_system_name(doc: dict[str, Any]) -> str:
    import re
    blob = " ".join(doc["preamble"]) + " " + json.dumps(doc["sections"][:2])
    m = re.search(r"\b[Tt]he\s+([A-Z][\w]*(?:\s+[A-Z][\w]*)*)\s+system\b", blob)
    if m:
        return m.group(1)
    m = re.search(r"for the\s+([A-Z][\w]*(?:\s+[A-Z][\w]*)*)\s+system", blob)
    return m.group(1) if m else (doc.get("title") or "Application")


def build_spec(doc_path: str | Path, views_path: str | Path | None = None) -> dict[str, Any]:
    """Read both input files and return the combined structured spec."""
    doc_path = Path(doc_path)
    doc_text = doc_path.read_text(encoding="utf-8", errors="replace")
    doc = parse_markdown_document(doc_text)

    views: dict[str, Any] = {"views": [], "index": {}}
    views_text = ""
    if views_path:
        views_path = Path(views_path)
        views_text = views_path.read_text(encoding="utf-8", errors="replace")
        views = parse_architecture_views(views_text)

    digest = _derive_digest(doc, views)
    return {
        "schema": "archagent.spec/v1",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "architecture_document": {"file": doc_path.name,
                                      "sha256": hashlib.sha256(doc_text.encode()).hexdigest()[:16]},
            "architecture_views": {"file": Path(views_path).name if views_path else None,
                                   "sha256": hashlib.sha256(views_text.encode()).hexdigest()[:16]},
        },
        "digest": digest,
        "architecture_document": doc,
        "architecture_views": views,
    }


def compact_json(data: Any) -> str:
    """Token-friendly serialisation used when embedding the spec in a prompt."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
