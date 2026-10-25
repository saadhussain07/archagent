"""Deterministic preprocessing: Markdown + PlantUML  ->  structured JSON spec.

No LLM tokens are spent here; the agent receives a compact, already
structured view of the inputs, which keeps cost low and grounding high.
"""

from .markdown_parser import parse_markdown_document
from .plantuml_parser import parse_architecture_views, parse_plantuml_block
from .spec_builder import build_spec

__all__ = [
    "parse_markdown_document",
    "parse_architecture_views",
    "parse_plantuml_block",
    "build_spec",
]
