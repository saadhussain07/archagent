import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

EXAMPLES = ROOT / "examples"
DOC = EXAMPLES / "Architecture_Documentation.md"
VIEWS = EXAMPLES / "Architecture_View.md"


@pytest.fixture
def spec():
    from archagent.preprocess.spec_builder import build_spec
    return build_spec(DOC, VIEWS)
