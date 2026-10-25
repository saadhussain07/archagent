"""Markdown + PlantUML preprocessing -> structured JSON."""

import json

from archagent.preprocess.markdown_parser import parse_markdown_document
from archagent.preprocess.plantuml_parser import parse_architecture_views, parse_plantuml_block
from archagent.preprocess.spec_builder import compact_json
from conftest import DOC, VIEWS


# ----------------------------------------------------------------- markdown

def test_markdown_sections_tables_and_code():
    doc = parse_markdown_document(DOC.read_text(encoding="utf-8"))
    headings = [s["heading"] for s in doc["sections"]]
    assert headings[0] == "A. Executive Summary"
    assert "L. Deliverables" in headings
    summary = doc["sections"][0]
    assert summary["tables"][0]["columns"] == ["Risk", "Mitigation"]
    assert len(summary["tables"][0]["rows"]) == 3
    assert doc["stats"]["code_blocks"] >= 9


def test_markdown_nested_subsections():
    doc = parse_markdown_document(DOC.read_text(encoding="utf-8"))
    detailed = next(s for s in doc["sections"] if s["heading"].startswith("D."))
    game = detailed["subsections"][0]
    assert game["heading"] == "GameComponent"
    sub = [s["heading"] for s in game["subsections"]]
    assert "Technology options" in sub and "Interface design" in sub


def test_markdown_requirements_merge_tables_and_prose():
    doc = parse_markdown_document(DOC.read_text(encoding="utf-8"))
    reqs = {r["id"]: r for r in doc["requirements"]}
    assert set(reqs) == {"FR-1", "NFR-1", "ASR-1", "ASR-2"}
    assert reqs["FR-1"]["components"] == "GameComponent"
    assert reqs["ASR-1"]["text"] == "data durability"
    assert reqs["ASR-2"]["text"] == "security"  # only mentioned in prose / QA table


def test_markdown_components_stack_deliverables():
    doc = parse_markdown_document(DOC.read_text(encoding="utf-8"))
    names = [c["name"] for c in doc["components"]]
    assert names[:3] == ["GameComponent", "QuestionComponent", "UserComponent"]
    concerns = [o["concern"] for o in doc["tech_stack"]["options"]]
    assert "Persistence" in concerns and len(concerns) == 12
    assert "Node.js 18" in doc["tech_stack"]["recommended"]
    assert "openapi.yaml" in doc["deliverables"]


def test_markdown_handles_crlf_and_empty():
    doc = parse_markdown_document("# Title\r\n\r\ntext\r\n- a\r\n")
    assert doc["title"] == "Title"
    assert doc["sections"][0]["bullets"] == ["a"]
    assert parse_markdown_document("")["sections"] == []


# ------------------------------------------------------------------ plantuml

def test_views_cover_all_eleven_view_types():
    views = parse_architecture_views(VIEWS.read_text(encoding="utf-8"))
    kinds = {d["kind"] for v in views["views"] for d in v["diagrams"]}
    assert kinds == {"usecase", "class", "object", "state", "activity", "sequence", "collaboration",
                     "package", "component", "deployment", "container"}
    assert [v["view"] for v in views["views"]] == [
        "ScenarioView", "LogicView", "ProcessView", "DevelopmentView", "PhysicalView"]
    assert len(views["index"]) == 13


def test_usecase_actors_resolved():
    d = parse_plantuml_block("""@startuml UC
actor EndUser as "End User"
usecase "Play Game" as (PlayGame)
EndUser -- (PlayGame)
@enduml""")
    assert d["kind"] == "usecase"
    assert d["use_cases"] == [{"id": "PlayGame", "label": "Play Game", "actors": ["End User"]}]


def test_class_members_and_relations():
    d = parse_plantuml_block("""@startuml ClassDiagram
class User {
  - id: string
  + playGame(game: Game): void
}
class Game
User --* Game
Admin --|> User
@enduml""")
    user = d["classes"][0]
    assert user["attributes"] == [{"name": "id", "visibility": "private", "type": "string"}]
    assert user["methods"][0]["params"] == [{"name": "game", "type": "Game"}]
    assert [c["name"] for c in d["classes"]] == ["User", "Game"]
    comp, inh = d["relations"]
    assert comp["kind"] == "composition" and comp["diamond_at"] == "Game"
    assert inh["kind"] == "inheritance" and inh["parent"] == "User"


def test_state_machine():
    d = parse_plantuml_block("""@startuml StateDiagram
state Playing
[*] --> Playing
Playing --> Paused : pause()
Playing --> GameOver : gameOver()
@enduml""")
    assert d["initial_state"] == "Playing"
    assert d["states"] == ["Playing", "Paused", "GameOver"]
    assert {"from": "Playing", "to": "Paused", "trigger": "pause()"} in d["transitions"]
    assert "GameOver" in d["terminal_states"]


def test_activity_decisions_nested():
    d = parse_plantuml_block("""@startuml ActivityDiagram
start
:play game;
if (game over?) then (yes)
  :game over;
else (no)
  :continue playing;
endif
:display score;
stop
@enduml""")
    decision = d["flow"][2]
    assert decision["type"] == "decision" and decision["condition"] == "game over?"
    assert [b["guard"] for b in decision["branches"]] == ["yes", "no"]
    assert d["actions"] == ["play game", "game over", "continue playing", "display score"]


def test_sequence_messages_and_scenario_note():
    d = parse_plantuml_block("""@startuml CollaborationDiagram1
participant User
User->>Game: play()
Game->>User: return prompt
note right of Game: Scenario: Play Game
@enduml""")
    assert d["kind"] == "collaboration"
    assert [p["name"] for p in d["participants"]] == ["User", "Game"]  # Game inferred
    assert d["messages"][1]["kind"] == "return"
    assert d["scenario"] == "Play Game"


def test_deployment_nodes():
    d = parse_plantuml_block("@startuml DeploymentDiagram\nnode A\nnode B\nA -- B\n@enduml")
    assert [e["name"] for e in d["elements"]] == ["A", "B"]
    assert d["connections"][0]["from"] == "A"


# ---------------------------------------------------------------------- spec

def test_spec_digest(spec):
    d = spec["digest"]
    assert d["system_name"] == "Space Fractions"
    assert [e["name"] for e in d["entities"]] == ["Game", "Question", "User", "Admin"]
    assert "updateQuestions(questions: List<Question>): void" in d["entities"][3]["operations"]
    assert d["state_machines"][0]["transitions"][1] == "Playing -> Paused [pause()]"
    assert {u["name"] for u in d["use_cases"]} == {"Play Game", "View Score", "Update Questions", "View Help"}
    assert len(d["scenarios"]) == 4


def test_spec_is_json_serialisable_and_compact(spec):
    text = compact_json(spec)
    assert json.loads(text)["schema"] == "archagent.spec/v1"
    assert len(text) < 40_000  # ~10k tokens: cheap to send, cached after turn 1
