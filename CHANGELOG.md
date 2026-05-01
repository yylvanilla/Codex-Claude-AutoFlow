# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-05-01

### Added

- Added a Windows GUI launcher for the Codex-Claude automated workflow.
- Added support for multiple allowed code directories in a single task.
- Added Codex model selection, Codex reasoning selection, Claude model selection, and Claude permission mode selection in the GUI.
- Added task labels, task IDs, workflow history, and restore-from-history support.
- Added retry-after-rejection support for repeated revise and re-review loops.
- Added optional auto-commit after final approval.
- Added EXE build scripts and local cleanup scripts.
- Added Chinese and English README documentation.

### Changed

- Clarified the agent handoff so the original user task is sent only to Codex, while Claude works only from Codex-generated plan and review feedback.
- Changed project-loading behavior so selecting a project restores the current active workflow state in the GUI when present.
- Changed the old `Start New Task` behavior into `New Task Draft`, which archives the current workflow and clears the task form without auto-running.
- Changed the commit stage to run non-interactively.
- Limited elevated execution for automated git commit behavior to the Codex commit phase only.

### Fixed

- Fixed a false-success case where the workflow treated the existence of `06_codex_commit.md` as a successful git commit.
- Fixed commit result handling to verify real repository state changes before reporting success.
- Fixed GUI completion messaging so commit-stage success, skip, and failure are distinguished more clearly.
- Fixed task-flow confusion when users wanted to start a new task inside a project that already had an active workflow.
- Fixed repeated review-loop status presentation so multi-round retry states are easier to understand.
