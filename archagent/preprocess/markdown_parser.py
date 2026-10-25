"""Parse an architecture document written in Markdown into structured JSON.

Output shape::

    {
      "title": str,
      "preamble": [str, ...],            # text before the first heading
      "sections": [Section, ...],         # nested by heading level
      "requirements": [{"id", "text", ...}],
      "components": [{"name", "description"}],
      "tech_stack": {"options": [...], "recommended": [...]},
      "deliverables": [str, ...],
      "stats": {...},
    }

    Section = {"heading", "level", "paragraphs", "bullets", "tables",
               "code_blocks", "subsections"}
"""

from __future__ import annotations

import re
from typing import Any

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
REQ_ID_RE = re.compile(r"\b((?:FR|NFR|ASR|QA|SR|UC)-\d+)\b")


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _new_section(heading: str, level: int) -> dict[str, Any]:
    return {
        "heading": heading,
        "level": level,
        "paragraphs": [],
        "bullets": [],
        "tables": [],
        "code_blocks": [],
        "subsections": [],
    }


def _parse_blocks(lines: list[str]) -> dict[str, Any]:
    """Single pass over lines, building a heading tree."""
    root = _new_section("__root__", 0)
    stack = [root]
    all_code: list[dict[str, str]] = []
    i = 0
    para: list[str] = []

    def current() -> dict[str, Any]:
        return stack[-1]

    def flush_para() -> None:
        if para:
            text = " ".join(s.strip() for s in para).strip()
            if text:
                current()["paragraphs"].append(text)
            para.clear()

    while i < len(lines):
        line = lines[i]

        fence = FENCE_RE.match(line)
        if fence:
            flush_para()
            marker, lang = fence.group(1), fence.group(2) or ""
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            block = {"lang": lang.lower(), "content": "\n".join(body).strip("\n")}
            current()["code_blocks"].append(block)
            all_code.append({**block, "section": current()["heading"]})
            continue

        heading = HEADING_RE.match(line)
        if heading:
            flush_para()
            level = len(heading.group(1))
            sec = _new_section(heading.group(2).strip(), level)
            while stack[-1]["level"] >= level:
                stack.pop()
            stack[-1]["subsections"].append(sec)
            stack.append(sec)
            i += 1
            continue

        # table: header row followed by a separator row
        if "|" in line and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            header = _split_row(line)
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = _split_row(lines[i])
                cells += [""] * (len(header) - len(cells))
                rows.append(dict(zip(header, cells)))
                i += 1
            current()["tables"].append({"columns": header, "rows": rows})
            continue

        bullet = BULLET_RE.match(line)
        if bullet:
            flush_para()
            current()["bullets"].append(bullet.group(1).strip())
            i += 1
            continue

        if not line.strip():
            flush_para()
        else:
            para.append(line)
        i += 1

    flush_para()
    return {"root": root, "code": all_code}


def _walk(section: dict[str, Any]):
    yield section
    for sub in section["subsections"]:
        yield from _walk(sub)


def _extract_requirements(root: dict[str, Any], raw: str) -> list[dict[str, str]]:
    reqs: dict[str, dict[str, str]] = {}
    for sec in _walk(root):
        for table in sec["tables"]:
            for row in table["rows"]:
                for key, value in row.items():
                    m = REQ_ID_RE.search(value)
                    if not m:
                        continue
                    rid = m.group(1)
                    entry = reqs.setdefault(rid, {"id": rid})
                    for k, v in row.items():
                        if k == key or not v:
                            continue
                        entry.setdefault(_snake(k), v)
                    # e.g. "ASR-1 (data durability)"
                    rest = value.replace(rid, "").strip(" ():.-")
                    if rest:
                        entry.setdefault("text", rest)
                    break
    # IDs mentioned in prose only (e.g. "meets ASR-2 (security)")
    for m in re.finditer(r"\b((?:FR|NFR|ASR)-\d+)\s*\(([^)]+)\)", raw):
        entry = reqs.setdefault(m.group(1), {"id": m.group(1)})
        entry.setdefault("text", m.group(2).strip())
    for entry in reqs.values():
        if "short_text" in entry and "text" not in entry:
            entry["text"] = entry["short_text"]
    return sorted(reqs.values(), key=lambda r: (r["id"].split("-")[0], int(r["id"].split("-")[1])))


def _snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().replace("(s)", "s")).strip("_")


def _extract_components(root: dict[str, Any]) -> list[dict[str, str]]:
    comps: dict[str, str] = {}
    pattern = re.compile(r"^\**`?([A-Z][A-Za-z0-9]+(?:Component|Service))`?\**\s*[:\-]\s*(.+)$")
    for sec in _walk(root):
        for bullet in sec["bullets"]:
            m = pattern.match(bullet)
            if m:
                comps.setdefault(m.group(1), m.group(2).strip())
        h = re.match(r"^([A-Z][A-Za-z0-9]+(?:Component|Service))$", sec["heading"])
        if h and h.group(1) not in comps:
            comps[h.group(1)] = " ".join(sec["paragraphs"][:1])
    return [{"name": k, "description": v} for k, v in comps.items()]


def _extract_tech_stack(root: dict[str, Any]) -> dict[str, list[Any]]:
    options, recommended = [], []
    for sec in _walk(root):
        head = sec["heading"].lower()
        if "technology options" in head:
            for b in sec["bullets"]:
                m = re.match(r"^([^:]+):\s*(.+?)(?:\s*\(Justification:\s*(.+)\))?$", b)
                if m:
                    options.append(
                        {"concern": m.group(1).strip(), "choice": m.group(2).strip(),
                         "justification": (m.group(3) or "").strip()}
                    )
        elif "recommended default stack" in head:
            recommended.extend(sec["bullets"])
    return {"options": options, "recommended": recommended}


def _extract_deliverables(root: dict[str, Any]) -> list[str]:
    for sec in _walk(root):
        if "deliverable" in sec["heading"].lower():
            for block in sec["code_blocks"]:
                files = [ln.strip() for ln in block["content"].splitlines() if ln.strip()]
                if files and all(re.match(r"^[\w./-]+\.\w+$", f) for f in files):
                    return files
    return []


def _prune(section: dict[str, Any]) -> dict[str, Any]:
    """Drop empty keys so the JSON sent to the LLM stays compact."""
    out: dict[str, Any] = {"heading": section["heading"]}
    for key in ("paragraphs", "bullets", "tables", "code_blocks"):
        if section[key]:
            out[key] = section[key]
    if section["subsections"]:
        out["subsections"] = [_prune(s) for s in section["subsections"]]
    return out


def parse_markdown_document(text: str) -> dict[str, Any]:
    """Parse a Markdown architecture document into a structured dict."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parsed = _parse_blocks(text.split("\n"))
    root = parsed["root"]

    # A document title is a leading H1 that is not an enumerated chapter ("A. ...").
    first = root["subsections"][0] if root["subsections"] else None
    title = first["heading"] if first and first["level"] == 1 and not re.match(
        r"^[A-Z0-9]{1,2}[.)]\s", first["heading"]) else ""

    preamble = list(root["paragraphs"]) + list(root["bullets"])
    return {
        "title": title,
        "preamble": preamble,
        "sections": [_prune(s) for s in root["subsections"]],
        "requirements": _extract_requirements(root, text),
        "components": _extract_components(root),
        "tech_stack": _extract_tech_stack(root),
        "deliverables": _extract_deliverables(root),
        "stats": {
            "characters": len(text),
            "sections": sum(1 for _ in _walk(root)) - 1,
            "code_blocks": len(parsed["code"]),
        },
    }
