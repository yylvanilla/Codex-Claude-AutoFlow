# Codex-Claude AutoFlow

[简体中文](./README.zh-CN.md)

Codex-Claude AutoFlow is a portable Windows GUI for **fully automated Codex + Claude collaboration** on local code projects.

You provide:

- a project directory
- one or more allowed code directories
- a task label
- the original task description

The tool then runs a structured review-driven workflow automatically:

1. `Codex` reads the original task and writes the implementation plan
2. `Claude` receives only the concrete plan and edits the real project files
3. `Codex` reviews the result
4. `Claude` revises if needed
5. `Codex` performs the final review
6. `Codex` always generates one wrap-up Markdown report (including suggested commit message)
7. If enabled, `Codex` can optionally attempt one real git commit after the wrap-up report

This makes the implementation loop predictable, resumable, and suitable for repeated local engineering tasks.

## Key Behavior

- Fully automated dual-agent loop: `Codex -> Claude -> Codex`
- The original user task is sent only to `Codex`
- `Claude` does not directly read the raw task description
- `Claude` works only from:
  - the `Codex` plan
  - the `Codex` review feedback
  - the selected project files
- Selecting a project automatically loads the current active workflow, if that project already has one

## Features

- Windows GUI launcher
- Multiple allowed code directories
- Codex model selection
- Stage-specific Codex reasoning effort selection (Plan/Review/Final/Wrap-up)
- Claude model selection
- Claude permission mode selection
- Default wrap-up report generation on success (`06_codex_commit.md`)
- Optional real git commit attempt after the wrap-up report
- Clear stage status tracking for the wrap-up report phase
- Task labels and task IDs
- Current task state inside `workflow/`
- Historical task archive inside `workflow_history/`
- Restore old tasks from the GUI
- Retry-after-rejection flow for repeated revise/re-review rounds
- Pause and resume support
- Workflow artifacts stored inside the target project

## Button Logic

When you choose a project directory, the launcher may automatically restore the current active task from that project's `workflow/` directory. That is expected behavior.

The main task buttons are intentionally separated:

- `Start` / `开始运行`: starts a brand-new workflow only when there is no active `workflow/`
- `Continue Last Task` / `继续上次任务`: continues the current active workflow
- `Retry Review Loop` / `再修再审`: used only after a rejected final review
- `New Task Draft` / `新任务草稿`: archives the current `workflow/`, clears the task label and task description, and **does not auto-run**

If you want to start a new task in a project that already contains an active workflow, the correct flow is:

1. Select the project
2. Click `New Task Draft` / `新任务草稿`
3. Enter the new task label and new task description
4. Click `Start` / `开始运行`

## Wrap-up Summary Behavior

After final review is approved, the workflow always generates `06_codex_commit.md`:

- the wrap-up stage runs only after final review is approved
- Codex generates `06_codex_commit.md` with:
  - workflow summary
  - changed-file summary
  - suggested commit title/body
  - suggested git commands for manual execution
- by default this stage is summary-only and does **not** run `git add` / `git commit` / `git push`
- stage success depends on whether Codex successfully produced the report content

This means:

- you still keep a consistent end-of-workflow artifact
- actual git commit is always done manually by you, based on the suggested message/commands

If `optional real git commit` is enabled:

- after generating `06_codex_commit.md`, Codex attempts one real git commit
- the commit result (completed/skipped/failed) is appended to `06_codex_commit.md`

## Do I Need Only The EXE?

**Yes. For normal end-user usage, downloading only `CodexClaudeWorkflow.exe` is enough.**

You do **not** need Python or the source code if all of the following are true:

- You are on Windows
- `codex` CLI is installed
- `codex` CLI is already authenticated
- `claude` CLI is installed
- `claude` CLI is already authenticated
- The EXE can create `launcher_settings.json` next to itself

In short:

- You can use the app with **just the EXE**
- The EXE does **not** bundle the `codex` CLI
- The EXE does **not** bundle the `claude` CLI
- The source repository is needed only for development, debugging, or rebuilding

## Quick Start

### Option A: Use the EXE

1. Download `CodexClaudeWorkflow.exe`
2. Install and log in to both `codex` and `claude`
3. Launch the EXE
4. Select your project directory
5. Add one or more allowed code directories
6. Enter a task label and task description
7. Click `Start`

If the selected project already contains an active workflow:

- continue it with `Continue Last Task`
- or prepare a different task with `New Task Draft`, then edit the form, then click `Start`

### Option B: Run from Source

Run:

```powershell
python .\workflow_launcher.py
```

Or double-click:

```text
open_workflow_launcher.cmd
```

## Repository Layout

Main source files:

- `workflow_launcher.py`: GUI launcher
- `orchestrate_agents.py`: workflow orchestrator
- `run_workflow.cmd`: command-line entry
- `open_workflow_launcher.cmd`: GUI entry
- `build_exe.py`: EXE build script
- `build_exe.cmd`: helper build command
- `clean_local.py`: remove local build leftovers
- `clean_local.cmd`: helper cleanup command
- `selftest/`: fake `codex` / `claude` commands for smoke testing

Common local/generated directories:

- `dist/`: built EXE output
- `build/`: PyInstaller build output
- `_tmp/`: temporary build files
- `__pycache__/`: Python cache
- `.venv_build/`: local build environment leftovers
- `_build_tools/`: optional local PyInstaller dependency cache

## Task History And Recovery

Each target project stores its active workflow inside:

- `workflow/`

When you start a new task in the same project:

- the previous `workflow/` is archived into `workflow_history/`
- the new task becomes the active `workflow/`

The GUI can:

- show the current task
- show historical tasks
- restore an old task back into the active `workflow/`

If final review is rejected:

- the workflow enters a waiting state
- you can close the app
- reopen it later
- load the same project
- trigger another revise/re-review round

## GitHub Publishing Recommendation

Recommended to commit:

- source files
- `README.md`
- `README.zh-CN.md`
- `LICENSE`
- `selftest/`
- `.gitignore`

Recommended not to commit:

- `build/`
- `_tmp/`
- `__pycache__/`
- `.venv_build/`
- `_build_tools/`
- local `launcher_settings.json`

`dist/` is usually better published as a **GitHub Release asset** instead of committing it into repository history.

Recommended publishing style:

1. Push the source repository to GitHub
2. Keep local/generated build folders out of git
3. Attach `CodexClaudeWorkflow.exe` to a GitHub Release

## Cleanup Local Build Artifacts

If you want to tidy the repository before publishing it, run:

```powershell
python .\clean_local.py
```

Or double-click:

```text
clean_local.cmd
```

The cleanup script removes local build leftovers such as:

- `build/`
- `_tmp/`
- `.venv_build/`
- `_build_tools/`
- `__pycache__/`
- `*.pyc`
- `dist/launcher_settings.json`

It keeps the built EXE itself.

## Build From Source

To build the EXE:

```powershell
python .\build_exe.py
```

Or:

```powershell
.\build_exe.cmd
```

`build_exe.py` supports either:

- a local `_build_tools` copy of PyInstaller
- a globally installed `PyInstaller`

If needed:

```powershell
pip install pyinstaller
```

## License

This project is released under the GPL license. See [LICENSE](./LICENSE).
