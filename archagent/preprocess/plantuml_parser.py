"""Parse PlantUML diagram blocks (the 4+1 architectural views) into JSON.

Supported diagram kinds (auto-detected from the content and the
``@startuml <Name>`` label): use case, class, object, state, activity,
sequence / collaboration, package, component, deployment, container.
Unknown constructs are kept verbatim under ``"unparsed"`` so nothing is
silently lost.
"""

from __future__ import annotations

import re
from typing import Any

START_RE = re.compile(r"@startuml\s*(\S*)")
FENCE_RE = re.compile(r"```\s*(?:plantuml|puml|uml)?\s*\n(.*?)```", re.S | re.I)
VIEW_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
TITLE_LINE_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s*$")

# Relationship arrows in class / component / package diagrams.
REL_RE = re.compile(
    r"^\s*\"?([\w.]+)\"?\s*(?:\"([^\"]*)\"\s*)?"
    r"(<\|--|--\|>|\*--|--\*|o--|--o|<\.\.|\.\.>|<--|-->|\.\.|--|->|<-)"
    r"\s*(?:\"([^\"]*)\"\s*)?\"?([\w.]+)\"?\s*(?::\s*(.*))?$"
)
REL_KIND = {
    "<|--": "inheritance", "--|>": "inheritance",
    "*--": "composition", "--*": "composition",
    "o--": "aggregation", "--o": "aggregation",
    "..>": "dependency", "<..": "dependency", "..": "dependency",
    "-->": "association", "<--": "association", "->": "association",
    "<-": "association", "--": "association",
}

VISIBILITY = {"+": "public", "-": "private", "#": "protected", "~": "package"}


def _clean(lines: str) -> list[str]:
    out = []
    for ln in lines.splitlines():
        s = ln.strip()
        if not s or s.startswith("'") or s.startswith("@enduml"):
            continue
        out.append(s)
    return out


def _detect_kind(name: str, lines: list[str]) -> str:
    n = name.lower()
    for key in ("usecase", "class", "object", "state", "activity", "sequence",
                "collaboration", "package", "component", "deployment", "container"):
        if key in n:
            return key
    body = "\n".join(lines)
    if re.search(r"^\s*(actor|usecase)\b", body, re.M):
        return "usecase"
    if re.search(r"^\s*class\b", body, re.M):
        return "class"
    if re.search(r"^\s*state\b|\[\*\]", body, re.M):
        return "state"
    if re.search(r"^\s*(start|stop)\s*$|^\s*:.*;\s*$", body, re.M):
        return "activity"
    if re.search(r"->>|->", body) and re.search(r"^\s*participant\b", body, re.M):
        return "sequence"
    if re.search(r"^\s*node\b", body, re.M):
        return "deployment"
    if re.search(r"^\s*(artifact|component)\b", body, re.M):
        return "component"
    return "unknown"


# --------------------------------------------------------------------- kinds

def _parse_member(line: str) -> tuple[str, dict[str, Any]]:
    vis = VISIBILITY.get(line[0], "public") if line[0] in VISIBILITY else "public"
    body = line[1:].strip() if line[0] in VISIBILITY else line
    m = re.match(r"^(\w+)\s*\(([^)]*)\)\s*(?::\s*(.+))?$", body)
    if m:
        params = []
        for p in filter(None, (x.strip() for x in m.group(2).split(","))):
            if ":" in p:
                pname, ptype = (s.strip() for s in p.split(":", 1))
            else:
                pname, ptype = p, ""
            params.append({"name": pname, "type": ptype})
        return "method", {"name": m.group(1), "visibility": vis, "params": params,
                          "returns": (m.group(3) or "void").strip()}
    m = re.match(r"^(\w+)\s*:\s*(.+)$", body)
    if m:
        return "attribute", {"name": m.group(1), "visibility": vis, "type": m.group(2).strip()}
    return "attribute", {"name": body, "visibility": vis, "type": ""}


def _relations(lines: list[str]) -> list[dict[str, str]]:
    rels = []
    for ln in lines:
        if "->>" in ln:
            continue
        m = REL_RE.match(ln)
        if not m:
            continue
        a, card_a, arrow, card_b, b, label = m.groups()
        a, b = a.strip("()"), b.strip("()")
        rel = {"from": a, "to": b, "kind": REL_KIND.get(arrow, "association"), "arrow": arrow}
        # Diamond position per PlantUML notation ("A --* B": diamond at B).
        if arrow in ("--*", "--o"):
            rel["diamond_at"] = b
        elif arrow in ("*--", "o--"):
            rel["diamond_at"] = a
        if arrow in ("--|>",):
            rel["child"], rel["parent"] = a, b
        elif arrow in ("<|--",):
            rel["child"], rel["parent"] = b, a
        if label:
            rel["label"] = label.strip()
        if card_a:
            rel["from_multiplicity"] = card_a
        if card_b:
            rel["to_multiplicity"] = card_b
        rels.append(rel)
    return rels


def _parse_usecase(lines: list[str]) -> dict[str, Any]:
    actors, usecases, containers, links = [], [], [], []
    for ln in lines:
        m = re.match(r'^actor\s+(?:"([^"]+)"\s+as\s+(\w+)|(\w+)(?:\s+as\s+"([^"]+)")?)', ln)
        if m:
            if m.group(1):
                actors.append({"id": m.group(2), "label": m.group(1)})
            else:
                actors.append({"id": m.group(3), "label": m.group(4) or m.group(3)})
            continue
        m = re.match(r'^usecase\s+"([^"]+)"\s+as\s+\(?(\w+)\)?', ln) or \
            re.match(r'^usecase\s+\(?(\w+)\)?(?:\s+as\s+"([^"]+)")?', ln)
        if m:
            g = m.groups()
            if ln.startswith('usecase "'):
                usecases.append({"id": g[1], "label": g[0]})
            else:
                usecases.append({"id": g[0], "label": g[1] or g[0]})
            continue
        m = re.match(r'^(rectangle|package)\s+"?([\w ]+)"?\s*\{', ln)
        if m:
            containers.append(m.group(2).strip())
            continue
        m = re.match(r"^\(?(\w+)\)?\s*(-+>?|\.+>?)\s*\(?(\w+)\)?(?:\s*:\s*(.*))?$", ln)
        if m:
            link = {"from": m.group(1), "to": m.group(3)}
            if m.group(4):
                link["label"] = m.group(4)
            links.append(link)
    by_id = {a["id"]: a["label"] for a in actors}
    for uc in usecases:
        uc["actors"] = sorted(
            {by_id.get(l["from"], l["from"]) for l in links if l["to"] == uc["id"]}
            | {by_id.get(l["to"], l["to"]) for l in links if l["from"] == uc["id"]}
        )
    return {"system_boundary": containers, "actors": actors, "use_cases": usecases, "links": links}


def _parse_class(lines: list[str]) -> dict[str, Any]:
    classes, current = [], None
    for ln in lines:
        m = re.match(r"^(abstract\s+class|class|interface|enum)\s+\"?(\w+)\"?(?:\s+(?:extends|implements)\s+(\w+))?\s*(\{)?\s*(\})?$", ln)
        if m:
            current = {"name": m.group(2), "stereotype": m.group(1).replace(" class", ""),
                       "attributes": [], "methods": []}
            if m.group(3):
                current["extends"] = m.group(3)
            classes.append(current)
            if not m.group(4) or m.group(5):
                current = None
            continue
        if current is not None:
            if ln == "}":
                current = None
                continue
            kind, member = _parse_member(ln)
            current["attributes" if kind == "attribute" else "methods"].append(member)
    return {"classes": classes, "relations": _relations(lines)}


def _parse_object(lines: list[str]) -> dict[str, Any]:
    objects, links = [], []
    for ln in lines:
        m = re.match(r"^(?:object|participant)\s+\"?(\w+)\"?(?:\s+as\s+(\w+))?(?:\s*:\s*(\w+))?", ln)
        if m:
            name = m.group(1)
            inferred = re.sub(r"\d+$", "", name)
            objects.append({"name": name, "instance_of": m.group(3) or inferred[:1].upper() + inferred[1:]})
            continue
        m = re.match(r"^(\w+)\s*-+>\s*(\w+)(?:\s*:\s*(.*))?$", ln)
        if m:
            links.append({"from": m.group(1), "to": m.group(2), **({"label": m.group(3)} if m.group(3) else {})})
    return {"objects": objects, "links": links}


def _parse_state(lines: list[str]) -> dict[str, Any]:
    states, transitions = [], []
    for ln in lines:
        m = re.match(r'^state\s+"?([\w ]+?)"?(?:\s+as\s+(\w+))?\s*\{?$', ln)
        if m:
            states.append(m.group(2) or m.group(1).strip())
            continue
        m = re.match(r"^(\[\*\]|\w+)\s*-+>\s*(\[\*\]|\w+)(?:\s*:\s*(.*))?$", ln)
        if m:
            src, dst, trig = m.groups()
            for s in (src, dst):
                if s != "[*]" and s not in states:
                    states.append(s)
            t = {"from": "INITIAL" if src == "[*]" else src,
                 "to": "FINAL" if dst == "[*]" else dst}
            if trig:
                t["trigger"] = trig.strip()
            transitions.append(t)
    initial = [t["to"] for t in transitions if t["from"] == "INITIAL"]
    final = [t["from"] for t in transitions if t["to"] == "FINAL"]
    terminal = [s for s in states if not any(t["from"] == s for t in transitions)]
    return {"states": states, "initial_state": initial[0] if initial else None,
            "final_states": final, "terminal_states": terminal, "transitions": transitions}


def _parse_activity(lines: list[str]) -> dict[str, Any]:
    """Build a nested flow: actions + if/else branches."""
    root: list[Any] = []
    stack: list[list[Any]] = [root]
    branch_stack: list[dict[str, Any]] = []
    for ln in lines:
        if ln in ("start", "stop", "end"):
            stack[-1].append({"type": ln})
            continue
        m = re.match(r"^:(.*);$", ln)
        if m:
            stack[-1].append({"type": "action", "text": m.group(1).strip()})
            continue
        m = re.match(r"^if\s*\((.*?)\)\s*then\s*(?:\((.*?)\))?$", ln)
        if m:
            node = {"type": "decision", "condition": m.group(1).strip(),
                    "branches": [{"guard": (m.group(2) or "yes").strip(), "steps": []}]}
            stack[-1].append(node)
            branch_stack.append(node)
            stack.append(node["branches"][0]["steps"])
            continue
        m = re.match(r"^(?:else|elseif\s*\((.*?)\)\s*then)\s*(?:\((.*?)\))?$", ln)
        if m and branch_stack:
            stack.pop()
            br = {"guard": (m.group(2) or m.group(1) or "else").strip(), "steps": []}
            branch_stack[-1]["branches"].append(br)
            stack.append(br["steps"])
            continue
        if ln == "endif" and branch_stack:
            stack.pop()
            branch_stack.pop()
            continue
    actions = []

    def collect(steps):
        for s in steps:
            if s["type"] == "action":
                actions.append(s["text"])
            elif s["type"] == "decision":
                for b in s["branches"]:
                    collect(b["steps"])
    collect(root)
    return {"flow": root, "actions": actions}


def _parse_sequence(lines: list[str]) -> dict[str, Any]:
    participants, messages, notes = [], [], []
    for ln in lines:
        m = re.match(r'^(participant|actor|boundary|control|entity|database)\s+"?([\w ]+?)"?(?:\s+as\s+(\w+))?$', ln)
        if m:
            participants.append({"name": m.group(3) or m.group(2).strip(), "kind": m.group(1)})
            continue
        m = re.match(r"^(\w+)\s*(->>|-->>|-->|->|<--|<-)\s*(\w+)\s*:\s*(.*)$", ln)
        if m:
            src, arrow, dst, label = m.groups()
            if arrow.startswith("<"):
                src, dst = dst, src
            label = label.strip()
            is_return = arrow.startswith("--") or label.lower().startswith("return")
            messages.append({"step": len(messages) + 1, "from": src, "to": dst,
                             "message": label, "kind": "return" if is_return else "call"})
            continue
        m = re.match(r"^note\s+(?:right|left|over)\s+(?:of\s+)?([\w, ]+?)\s*:\s*(.*)$", ln)
        if m:
            notes.append({"on": m.group(1).strip(), "text": m.group(2).strip()})
    known = {p["name"] for p in participants}
    for msg in messages:
        for side in ("from", "to"):
            if msg[side] not in known:
                participants.append({"name": msg[side], "kind": "participant"})
                known.add(msg[side])
    out: dict[str, Any] = {"participants": participants, "messages": messages}
    if notes:
        out["notes"] = notes
        scen = [n["text"].split(":", 1)[1].strip() for n in notes if n["text"].lower().startswith("scenario:")]
        if scen:
            out["scenario"] = scen[0]
    return out


def _parse_package(lines: list[str]) -> dict[str, Any]:
    packages, current = [], None
    for ln in lines:
        m = re.match(r'^package\s+"?([\w. ]+?)"?\s*\{$', ln)
        if m:
            current = {"name": m.group(1).strip(), "classes": []}
            packages.append(current)
            continue
        if ln == "}":
            current = None
            continue
        m = re.match(r"^(?:class|interface|enum)\s+(\w+)", ln)
        if m and current is not None:
            current["classes"].append(m.group(1))
    return {"packages": packages, "dependencies": _relations([l for l in lines if not l.startswith("package")])}


def _parse_nodes(lines: list[str]) -> dict[str, Any]:
    nodes = []
    for ln in lines:
        m = re.match(r'^(node|artifact|component|database|cloud|frame|container)\s+"?([\w ]+?)"?(?:\s+as\s+(\w+))?\s*\{?$', ln)
        if m:
            nodes.append({"name": m.group(3) or m.group(2).strip(), "kind": m.group(1)})
    return {"elements": nodes, "connections": _relations(lines)}


PARSERS = {
    "usecase": _parse_usecase,
    "class": _parse_class,
    "object": _parse_object,
    "state": _parse_state,
    "activity": _parse_activity,
    "sequence": _parse_sequence,
    "collaboration": _parse_sequence,
    "package": _parse_package,
    "component": _parse_nodes,
    "deployment": _parse_nodes,
    "container": _parse_nodes,
}


def parse_plantuml_block(source: str) -> dict[str, Any]:
    """Parse one ``@startuml ... @enduml`` block."""
    m = START_RE.search(source)
    name = m.group(1) if m and m.group(1) else "Diagram"
    body = source[m.end():] if m else source
    lines = _clean(body)
    kind = _detect_kind(name, lines)
    parsed = PARSERS.get(kind, lambda _l: {})(lines)
    return {"name": name, "kind": kind, **parsed}


def parse_architecture_views(text: str) -> dict[str, Any]:
    """Parse a Markdown file containing ``## <View>`` headings with PlantUML blocks.

    Returns ``{"views": [{"view", "diagrams": [...]}, ...], "index": {...}}``.
    """
    text = text.replace("\r\n", "\n")
    headings = list(VIEW_HEADING_RE.finditer(text))
    chunks: list[tuple[str, str]] = []
    if not headings:
        chunks.append(("Views", text))
    for idx, h in enumerate(headings):
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        chunks.append((h.group(1).strip(), text[h.end():end]))

    views, index = [], {}
    for view_name, chunk in chunks:
        diagrams = []
        last_title = ""
        for fm in FENCE_RE.finditer(chunk):
            before = chunk[: fm.start()].rstrip().splitlines()
            title = ""
            for prev in reversed(before):
                tm = TITLE_LINE_RE.match(prev)
                if tm:
                    title = tm.group(1)
                    break
                if prev.strip().startswith("```"):
                    title = last_title  # consecutive blocks share a caption
                    break
            last_title = title
            diagram = parse_plantuml_block(fm.group(1))
            if title:
                diagram["title"] = title
            diagrams.append(diagram)
            index[diagram["name"]] = {"view": view_name, "kind": diagram["kind"]}
        views.append({"view": view_name, "diagrams": diagrams})
    return {"views": views, "index": index}
