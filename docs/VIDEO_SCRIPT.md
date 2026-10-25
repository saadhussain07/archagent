# YouTube demo: suggested script (about 6–8 minutes)

Record with OBS or the Xbox Game Bar (Win+G). Use 1080p and make the font readable.

| Time | Show | Say |
|---|---|---|
| 0:00 | Title slide or the README | "This is ArchAgent, a simplified Claude-Code-style coding agent driven by DeepSeek v4-pro. It turns an architecture document and UML views into a working desktop game." |
| 0:30 | `docs/DESIGN.md` diagram | Explain the pipeline: deterministic preprocessing to JSON, then the agent loop with tools, then the verification gate. Name the Claude Code ideas: tool interface, read-before-edit, unique-match edits, TodoWrite, compaction, prompt caching. |
| 1:30 | Explorer: double-click `ArchAgent.exe` | Point out that it is a single exe with no Python installed. You can also show `where python` failing in cmd. |
| 1:50 | GUI | Paste the API key, press **Test**, then show the inputs (bundled examples), the output folder and the $1.00 budget. Press **Preview structured JSON** and scroll through the digest: entities, the state machine, scenarios. |
| 2:40 | Press **Generate project** | Narrate the Activity tab: reasoning, `todo_write`, `write_files`, `run_tests`. Switch to the **Plan** tab and watch the items tick. Point at the live cost bar. |
| 4:00 | (speed up the recording) | Show a failing test followed by `read_file`, `edit_file` and a re-run that passes. This is the self-correction loop. |
| 4:40 | Completion line | Read the summary: status, turns, cost (for example $0.3x), tests passed, self-test OK. Open `.agent/run_report.md`. |
| 5:10 | **Files** tab and Explorer | Walk through the layout: packages per component, `requirements.txt`, `Dockerfile`, `README.md` (architecture mapping and traceability table), `tests/`, `docs/architecture/`. |
| 5:50 | **Run tests** button | The tests run on the interpreter bundled in the exe. |
| 6:10 | **Launch app** button | Play the generated game: intro, menu, a few fraction questions (right and wrong), pause and resume, game over, score, help. Then admin login, edit a question, restart the app and show that the change persisted (SQLite). |
| 7:30 | Wrap up | Recap: cost under $1, a desktop exe, a tested project. Link the GitHub repository. |

Tips:
* Do the real run once before recording, so you know roughly how long it takes. Then record a second, fresh run into a new folder.
* Off-peak DeepSeek pricing (outside 01:00–04:00 and 06:00–10:00 UTC on weekdays, which is 06:00–09:00 and 11:00–15:00 PKT) halves the cost.
