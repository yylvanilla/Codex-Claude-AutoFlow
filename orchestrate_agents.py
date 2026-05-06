#!/usr/bin/env python3
"""Coordinate a Codex -> Claude -> Codex -> Claude workflow in one workspace."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CODEX_CMD = "codex.cmd"
CLAUDE_CMD = "claude-work.cmd"

TASK_FILE = "00_task.txt"
PLAN_FILE = "01_codex_plan.md"
IMPLEMENTATION_FILE = "02_claude_implementation.md"
IMPLEMENTATION_DIFF_FILE = "02_workspace_after_claude.diff"
REVIEW_FILE = "03_codex_review.md"
RETRY_GUIDANCE_FILE = "03_retry_codex_guidance.md"
REVISION_FILE = "04_claude_revision.md"
REVISION_DIFF_FILE = "04_workspace_after_revision.diff"
FINAL_REVIEW_FILE = "05_codex_final_review.md"
COMMIT_FILE = "06_codex_commit.md"
MANIFEST_FILE = "run_manifest.json"
LOG_FILE = "run.log"

PROMPT_FILES = {
    "plan": "prompts/plan_prompt.txt",
    "implement": "prompts/implement_prompt.txt",
    "review": "prompts/review_prompt.txt",
    "revise": "prompts/revise_prompt.txt",
    "final_review": "prompts/final_review_prompt.txt",
    "commit": "prompts/commit_prompt.txt",
    "git_commit": "prompts/git_commit_prompt.txt",
}


class WorkflowError(RuntimeError):
    """Raised when a workflow stage fails."""


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_task_id() -> str:
    return "task-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def creationflags_no_window() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def build_hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = 0
    return startupinfo


class WorkflowRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project_dir = args.project_dir.resolve()
        self.code_dirs = self.resolve_code_dirs(args.code_dir)
        self.primary_code_dir = self.code_dirs[0]
        self.output_dir = (self.project_dir / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir.resolve()
        self.prompts_dir = self.output_dir / "prompts"
        self.manifest_path = self.output_dir / MANIFEST_FILE
        self.log_path = self.output_dir / LOG_FILE
        self.manifest = self._load_manifest() if (args.resume or args.retry_final_reject) else self._new_manifest()
        self.codex_cmd = self.resolve_command(args.codex_cmd)
        self.claude_cmd = self.resolve_command(args.claude_cmd)
        self.hidden_startupinfo = build_hidden_startupinfo()

    def resolve_code_dirs(self, raw_code_dirs: list[Path]) -> list[Path]:
        resolved: list[Path] = []
        for raw_path in raw_code_dirs:
            path = (self.project_dir / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
            resolved.append(path)
        return resolved

    def _new_manifest(self) -> dict[str, Any]:
        return {
            "created_at": utc_timestamp(),
            "updated_at": utc_timestamp(),
            "status": "initialized",
            "task_id": None,
            "task_label": "",
            "task": None,
            "project_dir": str(self.project_dir),
            "code_dirs": [str(path) for path in self.code_dirs],
            "output_dir": str(self.output_dir),
            "stages": {},
            "commands": [],
            "artifacts": {},
        }

    def _load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return self._new_manifest()

    def save_manifest(self) -> None:
        self.manifest["updated_at"] = utc_timestamp()
        ensure_parent(self.manifest_path)
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    def log(self, message: str) -> None:
        ensure_parent(self.log_path)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now_stamp()}] {message}\n")

    def set_stage(self, name: str, status: str, **extra: Any) -> None:
        stage = self.manifest.setdefault("stages", {}).setdefault(name, {})
        stage.update({"status": status, "updated_at": utc_timestamp(), **extra})
        self.save_manifest()

    def artifact_path(self, filename: str) -> Path:
        return self.output_dir / filename

    def write_artifact(self, filename: str, content: str) -> Path:
        path = self.artifact_path(filename)
        ensure_parent(path)
        path.write_text(content, encoding="utf-8")
        self.manifest.setdefault("artifacts", {})[filename] = str(path)
        self.save_manifest()
        return path

    def resolve_task_text(self) -> str:
        task = str(self.args.task or "").strip()
        if task:
            return task
        manifest_task = str(self.manifest.get("task") or "").strip()
        if manifest_task:
            return manifest_task
        return read_text_if_exists(self.artifact_path(TASK_FILE)).strip()

    def resolve_task_label(self) -> str:
        task_label = str(getattr(self.args, "task_label", "") or "").strip()
        if task_label:
            return task_label
        return str(self.manifest.get("task_label") or "").strip()

    def ensure_task_identity(self) -> None:
        task_id = str(self.manifest.get("task_id") or "").strip()
        if not task_id:
            task_id = new_task_id()
            self.manifest["task_id"] = task_id
        self.manifest["task_label"] = self.resolve_task_label()

    def load_required_artifact(self, filename: str, description: str) -> str:
        content = read_text_if_exists(self.artifact_path(filename)).strip()
        if not content:
            raise WorkflowError(f"Missing required workflow artifact: {description} ({filename})")
        return content

    @staticmethod
    def retry_history_prefix(retry_round: int) -> str:
        return f"retry_history/retry_{retry_round:02d}"

    def retry_history_file(self, retry_round: int, filename: str) -> str:
        return f"{self.retry_history_prefix(retry_round)}/{filename}"

    def next_retry_round(self) -> int:
        return int(self.manifest.get("retry_round", 0)) + 1

    def clear_pending_decision(self) -> None:
        self.manifest.pop("pending_action", None)
        self.manifest.pop("last_rejected_at", None)
        self.manifest.pop("last_rejection_stage", None)
        self.manifest.pop("last_final_review_verdict", None)

    def mark_waiting_for_retry(self, source_stage: str) -> None:
        self.manifest["status"] = "needs_user_decision"
        self.manifest["pending_action"] = "retry_final_reject"
        self.manifest["last_final_review_verdict"] = "rejected"
        self.manifest["last_rejection_stage"] = source_stage
        self.manifest["last_rejected_at"] = utc_timestamp()
        self.save_manifest()

    def finalize_completed_workflow(self) -> None:
        self.manifest["status"] = "completed"
        self.manifest.pop("active_retry_round", None)
        self.clear_pending_decision()
        self.save_manifest()

    def run(self) -> str:
        self.validate_environment()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        if not self.args.resume and not self.args.retry_final_reject:
            self.manifest = self._new_manifest()

        task_text = self.resolve_task_text()
        if not task_text:
            raise WorkflowError("Task description is required for a new workflow.")
        self.args.task = task_text
        self.args.task_label = self.resolve_task_label()
        self.ensure_task_identity()
        self.manifest["task"] = self.args.task
        self.manifest["status"] = "running"
        if not self.args.retry_final_reject:
            self.manifest.pop("active_retry_round", None)
        self.manifest.pop("error", None)
        self.clear_pending_decision()
        self.save_manifest()

        self.write_artifact(TASK_FILE, self.args.task)

        if self.args.retry_final_reject:
            return self.run_retry_after_rejection()

        plan_text = self.run_plan_stage()
        implementation_text = self.run_implementation_stage(plan_text)
        review_text = self.run_review_stage(plan_text, implementation_text)

        if self.review_requests_revision(review_text):
            revision_text = self.run_revision_stage(plan_text, implementation_text, review_text)
        else:
            revision_text = "No Claude revision was required because the Codex review already approved the workspace."
            self.write_artifact(REVISION_FILE, revision_text)
            self.write_artifact(REVISION_DIFF_FILE, self.snapshot_git_diff_text())
            self.set_stage("revise", "skipped", reason="initial review approved")

        final_review_text = self.run_final_review_stage(plan_text, implementation_text, revision_text)

        if not self.review_allows_completion(final_review_text):
            self.mark_waiting_for_retry("initial_final_review")
            return "needs_user_decision"

        self.run_commit_stage(plan_text, final_review_text, perform_git_commit=self.args.commit_on_success)

        self.finalize_completed_workflow()
        return "completed"

    def run_retry_after_rejection(self) -> str:
        rejected_final_review = self.load_required_artifact(FINAL_REVIEW_FILE, "rejected final review")
        if self.review_allows_completion(rejected_final_review):
            raise WorkflowError("The current final review is already APPROVED; retry-after-reject is not needed.")

        plan_text = self.load_required_artifact(PLAN_FILE, "Codex plan")
        implementation_text = self.load_required_artifact(IMPLEMENTATION_FILE, "Claude implementation summary")
        latest_revision_text = read_text_if_exists(self.artifact_path(REVISION_FILE)).strip()
        if not latest_revision_text:
            latest_revision_text = implementation_text

        retry_round = self.next_retry_round()
        self.manifest["retry_round"] = retry_round
        self.manifest["active_retry_round"] = retry_round
        self.save_manifest()
        self.set_stage("final_review", "pending_retry", retry_round=retry_round, pending_after="review")

        retry_guidance_text = self.run_retry_codex_guidance_stage(
            plan_text,
            implementation_text,
            latest_revision_text,
            rejected_final_review,
            retry_round,
        )
        revision_text = self.run_retry_revision_stage(
            plan_text,
            implementation_text,
            latest_revision_text,
            rejected_final_review,
            retry_guidance_text,
            retry_round,
        )
        final_review_text = self.run_retry_final_review_stage(
            plan_text,
            implementation_text,
            revision_text,
            rejected_final_review,
            retry_round,
        )

        if not self.review_allows_completion(final_review_text):
            self.mark_waiting_for_retry(f"retry_final_review_{retry_round:02d}")
            return "needs_user_decision"

        self.run_commit_stage(plan_text, final_review_text, perform_git_commit=self.args.commit_on_success)

        self.finalize_completed_workflow()
        return "completed"

    def validate_environment(self) -> None:
        missing: list[str] = []
        if not self.codex_cmd:
            missing.append(str(self.args.codex_cmd))
        if not self.claude_cmd:
            missing.append(str(self.args.claude_cmd))
        if not self.project_dir.exists():
            missing.append(str(self.project_dir))
        for code_dir in self.code_dirs:
            if not code_dir.exists():
                missing.append(str(code_dir))
        if missing:
            raise WorkflowError("Missing required paths: " + ", ".join(missing))

    def should_skip(self, stage_name: str, artifact_file: str) -> bool:
        if not self.args.resume:
            return False
        stage = self.manifest.get("stages", {}).get(stage_name, {})
        return stage.get("status") == "completed" and self.artifact_path(artifact_file).exists()

    def run_command(self, args: list[str], *, cwd: Path, stdin_text: str | None = None) -> CommandResult:
        quoted = subprocess.list2cmdline(args)
        self.log(f"RUN cwd={cwd} cmd={quoted}")
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=creationflags_no_window(),
            startupinfo=self.hidden_startupinfo,
        )
        self.log(f"EXIT code={completed.returncode}")
        if completed.stdout:
            self.log("STDOUT\n" + completed.stdout)
        if completed.stderr:
            self.log("STDERR\n" + completed.stderr)
        self.manifest.setdefault("commands", []).append(
            {
                "timestamp": utc_timestamp(),
                "cwd": str(cwd),
                "args": args,
                "returncode": completed.returncode,
            }
        )
        self.save_manifest()
        return CommandResult(args=args, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)

    @staticmethod
    def detect_claude_error_text(stdout_text: str, stderr_text: str) -> str:
        combined = "\n".join(part for part in (stdout_text, stderr_text) if part).strip()
        if not combined:
            return ""
        lowered = combined.lower()
        first_line = combined.splitlines()[0].strip().lower()
        prefix_markers = (
            "api error:",
            "anthropic api error",
            "error:",
            "request failed",
            "authentication error",
            "invalid api key",
        )
        token_markers = (
            '"type":"new_api_error"',
            '"type": "new_api_error"',
            '"code":"model_not_found"',
            '"code": "model_not_found"',
            "model_not_found",
        )
        if any(first_line.startswith(marker) for marker in prefix_markers):
            return combined.splitlines()[0].strip()
        if any(marker in lowered for marker in token_markers):
            return "detected_api_error_payload"
        return ""

    def ensure_claude_stage_success(self, result: CommandResult, stage_name: str, stage_label: str, **extra: Any) -> str:
        output = result.stdout.strip()
        error_hint = self.detect_claude_error_text(result.stdout, result.stderr)
        if result.returncode != 0 or not output or error_hint:
            stage_payload: dict[str, Any] = {"returncode": result.returncode, **extra}
            if error_hint:
                stage_payload["error"] = error_hint
            self.set_stage(stage_name, "failed", **stage_payload)
            raise WorkflowError(f"Claude {stage_label} stage failed")
        return output

    def stage_reasoning_effort(self, stage: str) -> str:
        stage_map = {
            "plan": str(getattr(self.args, "codex_plan_reasoning_effort", "") or "").strip(),
            "review": str(getattr(self.args, "codex_review_reasoning_effort", "") or "").strip(),
            "final_review": str(getattr(self.args, "codex_final_review_reasoning_effort", "") or "").strip(),
            "wrapup": str(getattr(self.args, "codex_wrapup_reasoning_effort", "") or "").strip(),
        }
        stage_value = stage_map.get(stage, "")
        if stage_value:
            return stage_value
        return str(getattr(self.args, "codex_reasoning_effort", "") or "").strip()

    def build_codex_exec_args(self, output_path: Path) -> list[str]:
        args = [str(self.codex_cmd), "exec", "--cd", str(self.primary_code_dir), "--output-last-message", str(output_path)]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        effort = self.stage_reasoning_effort("plan")
        if effort:
            args.extend(["--config", f'model_reasoning_effort="{effort}"'])
        for code_dir in self.code_dirs[1:]:
            args.extend(["--add-dir", str(code_dir)])
        if self.args.codex_extra_args:
            args.extend(self.split_extra_args(self.args.codex_extra_args))
        return args

    def build_codex_review_args(self, output_path: Path, *, final_review: bool = False) -> list[str]:
        args = [str(self.codex_cmd), "exec", "--cd", str(self.primary_code_dir), "--output-last-message", str(output_path)]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        effort = self.stage_reasoning_effort("final_review" if final_review else "review")
        if effort:
            args.extend(["--config", f'model_reasoning_effort="{effort}"'])
        for code_dir in self.code_dirs[1:]:
            args.extend(["--add-dir", str(code_dir)])
        extra = self.args.codex_review_extra_args or self.args.codex_extra_args
        if extra:
            args.extend(self.split_extra_args(extra))
        return args

    def build_codex_commit_args(self, output_path: Path) -> list[str]:
        args = [
            str(self.codex_cmd),
            "exec",
            "--cd",
            str(self.project_dir),
            "--output-last-message",
            str(output_path),
        ]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        effort = self.stage_reasoning_effort("wrapup")
        if effort:
            args.extend(["--config", f'model_reasoning_effort="{effort}"'])
        for code_dir in self.code_dirs:
            args.extend(["--add-dir", str(code_dir)])
        extra = self.args.codex_review_extra_args or self.args.codex_extra_args
        if extra:
            args.extend(self.split_extra_args(extra))
        return args

    def build_codex_git_commit_args(self, output_path: Path) -> list[str]:
        args = [
            str(self.codex_cmd),
            "-a",
            "never",
            "exec",
            "--cd",
            str(self.project_dir),
            "--output-last-message",
            str(output_path),
            "--sandbox",
            "danger-full-access",
        ]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        effort = self.stage_reasoning_effort("wrapup")
        if effort:
            args.extend(["--config", f'model_reasoning_effort="{effort}"'])
        for code_dir in self.code_dirs:
            args.extend(["--add-dir", str(code_dir)])
        extra = self.args.codex_review_extra_args or self.args.codex_extra_args
        if extra:
            args.extend(self.split_extra_args(extra))
        return args

    def build_claude_args(self, extra_args: str) -> list[str]:
        args = [
            str(self.claude_cmd),
            "-p",
            "--permission-mode",
            self.args.claude_permission_mode,
            "--add-dir",
            str(self.project_dir),
        ]
        if self.args.claude_model:
            args.extend(["--model", self.args.claude_model])
        if extra_args:
            args.extend(self.split_extra_args(extra_args))
        return args

    def build_claude_implement_args(self) -> list[str]:
        return self.build_claude_args(self.args.claude_extra_args)

    def build_claude_revise_args(self) -> list[str]:
        extra = self.args.claude_revise_extra_args or self.args.claude_extra_args
        return self.build_claude_args(extra)

    @staticmethod
    def split_extra_args(extra_args: str) -> list[str]:
        return shlex.split(extra_args, posix=False)

    def snapshot_git_diff(self, filename: str) -> str:
        diff_text = self.snapshot_git_diff_text()
        self.write_artifact(filename, diff_text)
        return diff_text

    def snapshot_git_diff_text(self) -> str:
        args = ["git", "diff", "--"]
        args.extend(self.relative_code_dir_args())
        result = self.run_command(args, cwd=self.project_dir)
        return result.stdout

    def relative_code_dir_args(self) -> list[str]:
        return [str(path.relative_to(self.project_dir)) for path in self.code_dirs]

    def git_status_porcelain(self, *paths: str) -> CommandResult:
        args = ["git", "status", "--porcelain"]
        if paths:
            args.extend(["--", *paths])
        return self.run_command(args, cwd=self.project_dir)

    def ensure_git_repository_for_commit(self) -> None:
        result = self.run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.project_dir)
        if result.returncode != 0 or result.stdout.strip().lower() != "true":
            raise WorkflowError("Commit stage requires a valid git repository in the selected project directory.")

    def git_head_commit(self) -> str:
        result = self.run_command(["git", "rev-parse", "HEAD"], cwd=self.project_dir)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def run_plan_stage(self) -> str:
        if self.should_skip("plan", PLAN_FILE):
            return read_text_if_exists(self.artifact_path(PLAN_FILE))

        prompt = self.build_plan_prompt()
        self.write_artifact(PROMPT_FILES["plan"], prompt)
        output_path = self.artifact_path(PLAN_FILE)
        self.set_stage("plan", "running", artifact=str(output_path))
        result = self.run_command(self.build_codex_exec_args(output_path), cwd=self.project_dir, stdin_text=prompt)
        plan_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not plan_text.strip():
            self.set_stage("plan", "failed", returncode=result.returncode)
            raise WorkflowError("Codex planning stage failed")
        self.write_artifact(PLAN_FILE, plan_text)
        self.set_stage("plan", "completed", returncode=result.returncode)
        return plan_text

    def run_implementation_stage(self, plan_text: str) -> str:
        if self.should_skip("implement", IMPLEMENTATION_FILE):
            return read_text_if_exists(self.artifact_path(IMPLEMENTATION_FILE))

        prompt = self.build_implement_prompt(plan_text)
        self.write_artifact(PROMPT_FILES["implement"], prompt)
        artifact = self.artifact_path(IMPLEMENTATION_FILE)
        self.set_stage("implement", "running", artifact=str(artifact))
        result = self.run_command(self.build_claude_implement_args(), cwd=self.project_dir, stdin_text=prompt)
        implementation_text = self.ensure_claude_stage_success(result, "implement", "implementation")
        self.write_artifact(IMPLEMENTATION_FILE, implementation_text)
        diff_text = self.snapshot_git_diff(IMPLEMENTATION_DIFF_FILE)
        self.set_stage(
            "implement",
            "completed",
            returncode=result.returncode,
            changed=bool(diff_text.strip()),
        )
        return implementation_text

    def run_review_stage(self, plan_text: str, implementation_text: str) -> str:
        if self.should_skip("review", REVIEW_FILE):
            return read_text_if_exists(self.artifact_path(REVIEW_FILE))

        prompt = self.build_review_prompt(plan_text, implementation_text)
        self.write_artifact(PROMPT_FILES["review"], prompt)
        output_path = self.artifact_path(REVIEW_FILE)
        self.set_stage("review", "running", artifact=str(output_path))
        result = self.run_command(
            self.build_codex_review_args(output_path, final_review=False),
            cwd=self.project_dir,
            stdin_text=prompt,
        )
        review_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not review_text.strip():
            self.set_stage("review", "failed", returncode=result.returncode)
            raise WorkflowError("Codex review stage failed")
        self.write_artifact(REVIEW_FILE, review_text)
        self.set_stage("review", "completed", returncode=result.returncode)
        return review_text

    def run_revision_stage(self, plan_text: str, implementation_text: str, review_text: str) -> str:
        if self.should_skip("revise", REVISION_FILE):
            return read_text_if_exists(self.artifact_path(REVISION_FILE))

        prompt = self.build_revise_prompt(plan_text, implementation_text, review_text)
        self.write_artifact(PROMPT_FILES["revise"], prompt)
        artifact = self.artifact_path(REVISION_FILE)
        self.set_stage("revise", "running", artifact=str(artifact))
        result = self.run_command(self.build_claude_revise_args(), cwd=self.project_dir, stdin_text=prompt)
        revision_text = self.ensure_claude_stage_success(result, "revise", "revision")
        self.write_artifact(REVISION_FILE, revision_text)
        diff_text = self.snapshot_git_diff(REVISION_DIFF_FILE)
        self.set_stage(
            "revise",
            "completed",
            returncode=result.returncode,
            changed=bool(diff_text.strip()),
        )
        return revision_text

    def run_final_review_stage(self, plan_text: str, implementation_text: str, revision_text: str) -> str:
        if self.should_skip("final_review", FINAL_REVIEW_FILE):
            return read_text_if_exists(self.artifact_path(FINAL_REVIEW_FILE))

        prompt = self.build_final_review_prompt(plan_text, implementation_text, revision_text)
        self.write_artifact(PROMPT_FILES["final_review"], prompt)
        output_path = self.artifact_path(FINAL_REVIEW_FILE)
        self.set_stage("final_review", "running", artifact=str(output_path))
        result = self.run_command(
            self.build_codex_review_args(output_path, final_review=True),
            cwd=self.project_dir,
            stdin_text=prompt,
        )
        review_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not review_text.strip():
            self.set_stage("final_review", "failed", returncode=result.returncode)
            raise WorkflowError("Codex final review stage failed")
        self.write_artifact(FINAL_REVIEW_FILE, review_text)
        self.set_stage("final_review", "completed", returncode=result.returncode)
        return review_text

    def run_retry_codex_guidance_stage(
        self,
        plan_text: str,
        implementation_text: str,
        latest_revision_text: str,
        rejected_final_review: str,
        retry_round: int,
    ) -> str:
        prompt = self.build_retry_guidance_prompt(
            plan_text,
            implementation_text,
            latest_revision_text,
            rejected_final_review,
            retry_round,
        )
        self.write_artifact(self.retry_history_file(retry_round, "prompts/retry_guidance_prompt.txt"), prompt)
        output_path = self.artifact_path(RETRY_GUIDANCE_FILE)
        self.set_stage("retry_guidance", "running", artifact=str(output_path), retry_round=retry_round)
        result = self.run_command(self.build_codex_review_args(output_path), cwd=self.project_dir, stdin_text=prompt)
        guidance_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not guidance_text.strip():
            self.set_stage("retry_guidance", "failed", returncode=result.returncode, retry_round=retry_round)
            raise WorkflowError("Codex retry guidance stage failed")
        self.write_artifact(RETRY_GUIDANCE_FILE, guidance_text)
        self.write_artifact(self.retry_history_file(retry_round, RETRY_GUIDANCE_FILE), guidance_text)
        self.set_stage("retry_guidance", "completed", returncode=result.returncode, retry_round=retry_round)
        return guidance_text

    def run_retry_revision_stage(
        self,
        plan_text: str,
        implementation_text: str,
        latest_revision_text: str,
        rejected_final_review: str,
        retry_guidance_text: str,
        retry_round: int,
    ) -> str:
        prompt = self.build_retry_revise_prompt(
            plan_text,
            implementation_text,
            latest_revision_text,
            rejected_final_review,
            retry_guidance_text,
            retry_round,
        )
        self.write_artifact(self.retry_history_file(retry_round, "prompts/retry_revise_prompt.txt"), prompt)
        self.write_artifact(self.retry_history_file(retry_round, "input_rejected_final_review.md"), rejected_final_review)
        artifact = self.artifact_path(REVISION_FILE)
        self.set_stage("revise", "running", artifact=str(artifact), retry_round=retry_round)
        result = self.run_command(self.build_claude_revise_args(), cwd=self.project_dir, stdin_text=prompt)
        revision_text = self.ensure_claude_stage_success(
            result,
            "revise",
            "retry revision",
            retry_round=retry_round,
        )
        self.write_artifact(REVISION_FILE, revision_text)
        self.write_artifact(self.retry_history_file(retry_round, REVISION_FILE), revision_text)
        diff_text = self.snapshot_git_diff(REVISION_DIFF_FILE)
        self.write_artifact(self.retry_history_file(retry_round, REVISION_DIFF_FILE), diff_text)
        self.set_stage(
            "revise",
            "completed",
            returncode=result.returncode,
            changed=bool(diff_text.strip()),
            retry_round=retry_round,
        )
        return revision_text

    def run_retry_final_review_stage(
        self,
        plan_text: str,
        implementation_text: str,
        revision_text: str,
        rejected_final_review: str,
        retry_round: int,
    ) -> str:
        prompt = self.build_retry_final_review_prompt(
            plan_text,
            implementation_text,
            revision_text,
            rejected_final_review,
            retry_round,
        )
        self.write_artifact(self.retry_history_file(retry_round, "prompts/retry_final_review_prompt.txt"), prompt)
        output_path = self.artifact_path(FINAL_REVIEW_FILE)
        self.set_stage("final_review", "running", artifact=str(output_path), retry_round=retry_round)
        result = self.run_command(
            self.build_codex_review_args(output_path, final_review=True),
            cwd=self.project_dir,
            stdin_text=prompt,
        )
        review_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not review_text.strip():
            self.set_stage("final_review", "failed", returncode=result.returncode, retry_round=retry_round)
            raise WorkflowError("Codex retry final review stage failed")
        self.write_artifact(FINAL_REVIEW_FILE, review_text)
        self.write_artifact(self.retry_history_file(retry_round, FINAL_REVIEW_FILE), review_text)
        self.set_stage("final_review", "completed", returncode=result.returncode, retry_round=retry_round)
        return review_text

    def run_commit_stage(self, plan_text: str, final_review_text: str, *, perform_git_commit: bool) -> str:
        if self.should_skip("commit", COMMIT_FILE):
            return read_text_if_exists(self.artifact_path(COMMIT_FILE))
        prompt = self.build_commit_prompt(plan_text, final_review_text)
        self.write_artifact(PROMPT_FILES["commit"], prompt)
        output_path = self.artifact_path(COMMIT_FILE)
        self.set_stage("commit", "running", artifact=str(output_path), commit_requested=perform_git_commit)
        result = self.run_command(self.build_codex_commit_args(output_path), cwd=self.project_dir, stdin_text=prompt)
        summary_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not summary_text.strip():
            self.set_stage("commit", "failed", returncode=result.returncode)
            raise WorkflowError("Codex wrap-up stage failed to produce the workflow summary artifact.")

        final_text = summary_text.rstrip() + "\n"
        if not perform_git_commit:
            self.write_artifact(COMMIT_FILE, final_text)
            self.set_stage("commit", "completed", returncode=result.returncode, mode="summary_only", commit_requested=False)
            return final_text

        git_commit_result = self.try_optional_git_commit(plan_text, final_review_text, summary_text)
        final_text = self.merge_summary_and_git_commit_result(summary_text, git_commit_result)
        self.write_artifact(COMMIT_FILE, final_text)

        if git_commit_result["status"] == "completed":
            self.set_stage(
                "commit",
                "completed",
                returncode=result.returncode,
                mode="summary_plus_git_commit",
                commit_requested=True,
                git_commit_status="completed",
                commit_hash=git_commit_result.get("commit_hash"),
            )
            return final_text

        if git_commit_result["status"] == "skipped":
            self.set_stage(
                "commit",
                "completed",
                returncode=result.returncode,
                mode="summary_plus_git_commit",
                commit_requested=True,
                git_commit_status="skipped",
                reason=git_commit_result.get("reason", "no_changes"),
            )
            return final_text

        self.set_stage(
            "commit",
            "failed",
            returncode=int(git_commit_result.get("returncode", 1) or 1),
            mode="summary_plus_git_commit",
            commit_requested=True,
            git_commit_status="failed",
            reason=git_commit_result.get("reason", "git_commit_failed"),
            previous_head=git_commit_result.get("head_before"),
            current_head=git_commit_result.get("head_after"),
        )
        raise WorkflowError("Codex generated 06 summary, but the optional real git commit step failed.")

    def try_optional_git_commit(self, plan_text: str, final_review_text: str, summary_text: str) -> dict[str, Any]:
        try:
            self.ensure_git_repository_for_commit()
        except WorkflowError as exc:
            return {
                "status": "failed",
                "reason": "not_git_repository",
                "returncode": 1,
                "report": f"Commit stage requires a valid git repository.\n\n{exc}",
            }

        if not self.has_git_changes():
            return {
                "status": "skipped",
                "reason": "no_changes",
                "report": "允许代码目录下未检测到代码改动，因此跳过真实 git 提交。",
            }

        head_before = self.git_head_commit()
        prompt = self.build_git_commit_prompt(plan_text, final_review_text, summary_text)
        self.write_artifact(PROMPT_FILES["git_commit"], prompt)
        output_path = self.artifact_path("06a_codex_git_commit.md")
        result = self.run_command(self.build_codex_git_commit_args(output_path), cwd=self.project_dir, stdin_text=prompt)
        commit_text = read_text_if_exists(output_path) or result.stdout
        head_after = self.git_head_commit()
        remaining_status = self.git_status_porcelain(*self.relative_code_dir_args()).stdout.strip()

        if head_after and head_after != head_before:
            commit_message = self.run_command(["git", "log", "-1", "--pretty=%s%n%b"], cwd=self.project_dir).stdout.strip()
            if not self.contains_cjk(commit_message):
                return {
                    "status": "failed",
                    "reason": "non_chinese_commit_message",
                    "returncode": result.returncode or 1,
                    "head_before": head_before,
                    "head_after": head_after,
                    "report": (
                        "已创建真实提交，但提交标题/正文未检测到中文，不符合当前工作流规范。\n\n"
                        f"提交哈希：{head_after}\n\n"
                        "当前提交信息：\n"
                        f"{commit_message or '(empty)'}\n\n"
                        "建议手动修正：\n"
                        "git commit --amend\n"
                    ),
                }
            if remaining_status:
                return {
                    "status": "failed",
                    "reason": "remaining_changes_after_commit",
                    "returncode": result.returncode or 1,
                    "head_before": head_before,
                    "head_after": head_after,
                    "report": (
                        "已创建真实提交，但允许代码目录下仍有未提交改动。当前规则要求一次提交覆盖允许目录下全部改动。\n\n"
                        f"提交哈希：{head_after}\n\n"
                        "允许代码目录下剩余 git 状态：\n"
                        f"{remaining_status}\n\n"
                        "Codex 输出：\n"
                        f"{(commit_text or '').strip() or '(empty)'}\n"
                    ),
                }
            return {
                "status": "completed",
                "returncode": result.returncode,
                "head_before": head_before,
                "head_after": head_after,
                "commit_hash": head_after,
                "report": (commit_text or "").strip() or f"真实 git 提交已成功创建。\n\n提交哈希：{head_after}",
            }

        failure_report = self.build_commit_failure_report(commit_text, result, head_before, head_after)
        return {
            "status": "failed",
            "reason": "no_real_commit_created",
            "returncode": result.returncode,
            "head_before": head_before,
            "head_after": head_after,
            "report": failure_report,
        }

    @staticmethod
    def merge_summary_and_git_commit_result(summary_text: str, git_commit_result: dict[str, Any]) -> str:
        summary = summary_text.rstrip()
        status = str(git_commit_result.get("status", "") or "")
        if status == "completed":
            suffix = (
                "## 可选真实 Git 提交结果\n\n"
                "状态：完成\n\n"
                f"提交哈希：{git_commit_result.get('commit_hash', '(unknown)')}\n\n"
                f"{str(git_commit_result.get('report', '') or '').strip()}\n"
            )
            return f"{summary}\n\n{suffix}"
        if status == "skipped":
            suffix = (
                "## 可选真实 Git 提交结果\n\n"
                "状态：跳过\n\n"
                f"原因：{git_commit_result.get('reason', 'no_changes')}\n\n"
                f"{str(git_commit_result.get('report', '') or '').strip()}\n"
            )
            return f"{summary}\n\n{suffix}"
        suffix = (
            "## 可选真实 Git 提交结果\n\n"
            "状态：失败\n\n"
            f"{str(git_commit_result.get('report', '') or '').strip()}\n"
        )
        return f"{summary}\n\n{suffix}"

    def build_plan_prompt(self) -> str:
        return (
            "You are Codex. Your job is to produce a concrete implementation plan only; do not modify code.\n"
            "Inspect the current code under the working directory and output a structured Markdown plan with at least:\n"
            "1. Goal\n"
            "2. Change points\n"
            "3. Affected files\n"
            "4. Test suggestions\n"
            "5. Acceptance criteria\n\n"
            f"Task:\n{self.args.task}\n"
        )

    def build_implement_prompt(self, plan_text: str) -> str:
        return (
            "You are Claude. Execute the Codex plan by directly modifying the project files in the workspace.\n"
            "You must rely only on the concrete Codex plan below and the code you can inspect in the workspace.\n"
            "Do not assume access to any separate original user request beyond the Codex instructions.\n"
            "Do not stop at a proposal or patch-only answer. Make the code changes now.\n"
            "Keep edits within the allowed code directory list and keep the scope limited to the task.\n"
            "Allowed code directories:\n"
            f"{self.render_code_dir_list()}\n\n"
            "After finishing, output a concise Markdown summary of what you changed, which files changed, and any validation you performed.\n\n"
            f"Codex plan:\n{plan_text}\n\n"
            f"CLAUDE.md:\n{self.load_claude_md()}\n"
        )

    def build_review_prompt(self, plan_text: str, implementation_text: str) -> str:
        return (
            "You are Codex. Review the current workspace code after Claude's implementation.\n"
            "This review report is an internal machine-to-machine artifact for the workflow and Claude.\n"
            "Do NOT address the user directly. Do NOT ask questions. Do NOT offer optional next steps.\n"
            "Inspect the actual files on disk, not just a proposed patch, and produce a Markdown review report with at least:\n"
            "1. Whether the code satisfies the task\n"
            "2. Potential bugs or risks\n"
            "3. Style or architecture concerns\n"
            "4. Required fixes vs optional improvements\n"
            "5. Whether Claude should revise the code before final approval\n"
            "6. A final single-line decision marker exactly in one of these forms:\n"
            "   REVIEW_DECISION: REVISE_REQUIRED\n"
            "   REVIEW_DECISION: APPROVED\n\n"
            f"Task:\n{self.args.task}\n\n"
            f"Allowed code directories:\n{self.render_code_dir_list()}\n\n"
            f"Original plan:\n{plan_text}\n\n"
            f"Claude implementation summary:\n{implementation_text}\n"
        )

    def build_revise_prompt(self, plan_text: str, implementation_text: str, review_text: str) -> str:
        return (
            "You are Claude. Codex reviewed the current workspace and requested revisions.\n"
            "You must rely only on the Codex plan and Codex review below, plus the code you can inspect in the workspace.\n"
            "Do not assume access to any separate original user request beyond the Codex instructions.\n"
            "Directly modify the existing project files to address the review findings. Do not return only a patch.\n"
            "Fix the required issues while keeping the scope constrained to the task.\n"
            "Allowed code directories:\n"
            f"{self.render_code_dir_list()}\n\n"
            "After finishing, output a concise Markdown summary of what you corrected and any remaining caveats.\n\n"
            f"Codex plan:\n{plan_text}\n\n"
            f"Claude implementation summary:\n{implementation_text}\n\n"
            f"Codex review:\n{review_text}\n"
        )

    def build_final_review_prompt(self, plan_text: str, implementation_text: str, revision_text: str) -> str:
        return (
            "You are Codex. Perform a final review of the current workspace code after Claude's revision.\n"
            "This final review is an internal machine-to-machine artifact for workflow control.\n"
            "Do NOT address the user directly. Do NOT ask questions. Do NOT offer optional follow-up actions.\n"
            "Inspect the actual files and output a Markdown final review containing:\n"
            "1. Final verdict\n"
            "2. Remaining risks, if any\n"
            "3. Test or manual verification recommendations\n"
            "4. A final single-line decision marker exactly in one of these forms:\n"
            "   FINAL_REVIEW_DECISION: APPROVED\n"
            "   FINAL_REVIEW_DECISION: REJECTED\n\n"
            f"Task:\n{self.args.task}\n\n"
            f"Allowed code directories:\n{self.render_code_dir_list()}\n\n"
            f"Original plan:\n{plan_text}\n\n"
            f"First implementation summary:\n{implementation_text}\n\n"
            f"Revision summary:\n{revision_text}\n"
        )

    def build_retry_guidance_prompt(
        self,
        plan_text: str,
        implementation_text: str,
        latest_revision_text: str,
        rejected_final_review: str,
        retry_round: int,
    ) -> str:
        return (
            "You are Codex. The previous final review was REJECTED and the user requested another retry cycle.\n"
            "Before Claude edits code again, produce a fresh retry guidance that combines the current task description and the rejected final review.\n"
            "Inspect the current workspace code and provide a concrete Markdown instruction set with at least:\n"
            "1. Retry objective (what changed or was clarified in the task)\n"
            "2. Rejected issues mapped to required fixes\n"
            "3. File-level change guidance\n"
            "4. Scope boundaries and things Claude must not change\n"
            "5. Validation checklist Claude should run before handing back\n"
            "6. A final section titled 'Instruction for Claude' with direct actionable bullets\n\n"
            f"Retry round: {retry_round}\n\n"
            f"Task:\n{self.args.task}\n\n"
            f"Allowed code directories:\n{self.render_code_dir_list()}\n\n"
            f"Original plan:\n{plan_text}\n\n"
            f"First implementation summary:\n{implementation_text}\n\n"
            f"Latest revision summary:\n{latest_revision_text}\n\n"
            f"Rejected final review from Codex:\n{rejected_final_review}\n"
        )

    def build_retry_revise_prompt(
        self,
        plan_text: str,
        implementation_text: str,
        latest_revision_text: str,
        rejected_final_review: str,
        retry_guidance_text: str,
        retry_round: int,
    ) -> str:
        return (
            "You are Claude. Codex already performed a final review and rejected the current workspace.\n"
            "This is a user-approved retry cycle. Directly modify the project files to address the rejected final review.\n"
            "You must rely on the Codex retry guidance below as the latest instruction baseline.\n"
            "Use the Codex plan and rejected final review as supporting context only.\n"
            "Do not assume access to any separate original user request beyond the Codex plan.\n"
            "Do not stop at a proposal or patch-only answer. Make the code changes now.\n"
            "Keep edits within the allowed code directory list and keep the scope limited to the task.\n"
            "Allowed code directories:\n"
            f"{self.render_code_dir_list()}\n\n"
            "After finishing, output a concise Markdown summary of what you corrected, which files changed, and any validation you performed.\n\n"
            f"Retry round: {retry_round}\n\n"
            f"Codex retry guidance:\n{retry_guidance_text}\n\n"
            f"Codex plan:\n{plan_text}\n\n"
            f"First implementation summary:\n{implementation_text}\n\n"
            f"Latest revision summary:\n{latest_revision_text}\n\n"
            f"Rejected final review from Codex:\n{rejected_final_review}\n"
        )

    def build_retry_final_review_prompt(
        self,
        plan_text: str,
        implementation_text: str,
        revision_text: str,
        rejected_final_review: str,
        retry_round: int,
    ) -> str:
        return (
            "You are Codex. This is a follow-up final review after a previously rejected final review.\n"
            "Inspect the current workspace code after Claude's latest retry revision and produce a Markdown final review containing:\n"
            "1. Final verdict\n"
            "2. Which rejected issues are now fixed vs still unresolved\n"
            "3. Remaining risks, if any\n"
            "4. Test or manual verification recommendations\n"
            "5. A clear line stating either 'APPROVED' or 'REJECTED'\n\n"
            f"Retry round: {retry_round}\n\n"
            f"Task:\n{self.args.task}\n\n"
            f"Allowed code directories:\n{self.render_code_dir_list()}\n\n"
            f"Original plan:\n{plan_text}\n\n"
            f"First implementation summary:\n{implementation_text}\n\n"
            f"Latest Claude retry revision summary:\n{revision_text}\n\n"
            f"Previously rejected final review:\n{rejected_final_review}\n"
        )

    def build_commit_prompt(self, plan_text: str, final_review_text: str) -> str:
        return (
            "你是 Codex。当前编码任务已通过最终审查。\n"
            "本阶段必须生成 workflow/06_codex_commit.md。\n"
            "本阶段严禁执行 git add / git commit / git push。\n"
            "请检查当前工作区与 git 状态，输出一份 Markdown 报告，且全文必须为简体中文，至少包含：\n"
            "1. 工作流总结（目标、关键实现点、终审结论）\n"
            "2. 改动文件清单及每个文件改动说明\n"
            "3. 验证总结（已验证内容与剩余风险）\n"
            "4. 建议的提交标题（简体中文）\n"
            "5. 建议的提交正文（简体中文）\n"
            "6. 建议执行的 git 命令（供用户手动执行）\n\n"
            f"任务描述：\n{self.args.task}\n\n"
            f"允许代码目录：\n{self.render_code_dir_list()}\n\n"
            "上下文参考：\n"
            "- 可按需引用 workflow/01_codex_plan.md\n"
            "- 可按需引用 workflow/05_codex_final_review.md\n"
            "- 以当前 git diff/status 为最终事实来源\n"
        )

    def build_git_commit_prompt(self, plan_text: str, final_review_text: str, summary_text: str) -> str:
        return (
            "你是 Codex。现在执行可选的真实 git 提交步骤。\n"
            "在当前仓库中操作。\n"
            "必须创建且仅创建 1 个提交，并包含允许代码目录下当前全部改动。\n"
            "提交后，允许代码目录下不得残留 staged/unstaged/untracked 改动。\n"
            "禁止提交 workflow/* 产物以及允许代码目录外的文件。\n"
            "提交标题与提交正文必须使用简体中文，不得使用英文提交信息。\n"
            "若仓库策略或权限导致无法提交，必须如实报告失败，不得宣称成功。\n"
            "执行后请输出（全文简体中文）：\n"
            "1. 提交状态（COMPLETED/SKIPPED/FAILED）\n"
            "2. 提交标题（简体中文）\n"
            "3. 提交正文（简体中文）\n"
            "4. 提交哈希（若成功）\n"
            "5. 已暂存/已提交文件摘要\n"
            "6. 失败原因与下一步手动命令建议（若失败）\n\n"
            f"任务描述：\n{self.args.task}\n\n"
            f"允许代码目录：\n{self.render_code_dir_list()}\n\n"
            f"Codex 计划：\n{plan_text}\n\n"
            f"最终审查：\n{final_review_text}\n\n"
            f"06 汇总草稿：\n{summary_text}\n"
        )

    def render_code_dir_list(self) -> str:
        return "\n".join(f"- {path}" for path in self.code_dirs)

    def has_git_changes(self) -> bool:
        result = self.git_status_porcelain(*self.relative_code_dir_args())
        return result.returncode == 0 and bool(result.stdout.strip())

    def build_commit_failure_report(
        self,
        commit_text: str,
        result: CommandResult,
        head_before: str,
        head_after: str,
    ) -> str:
        remaining_status = self.git_status_porcelain(*self.relative_code_dir_args()).stdout.strip()
        return (
            "# Commit 阶段失败\n\n"
            "Codex 已返回，但未创建真实 git 提交。\n\n"
            f"- 返回码：{result.returncode}\n"
            f"- 提交前 HEAD：{head_before or '(none)'}\n"
            f"- 提交后 HEAD：{head_after or '(none)'}\n\n"
            "## Codex 输出\n\n"
            f"{commit_text.strip() or '(empty)'}\n\n"
            "## STDERR\n\n"
            f"{result.stderr.strip() or '(empty)'}\n\n"
            "## 允许代码目录下剩余 git 状态\n\n"
            f"{remaining_status or '(clean or unavailable)'}\n"
        )

    def load_claude_md(self) -> str:
        for code_dir in self.code_dirs:
            claude_md = code_dir / "CLAUDE.md"
            if claude_md.exists():
                return read_text_if_exists(claude_md)
        return "(none)"

    @staticmethod
    def review_allows_completion(review_text: str) -> bool:
        decision = WorkflowRunner.parse_decision_marker(
            review_text,
            "REVIEW_DECISION",
            ("approved", "rejected", "revise_required"),
        )
        if decision:
            return decision == "approved"

        final_decision = WorkflowRunner.parse_decision_marker(
            review_text,
            "FINAL_REVIEW_DECISION",
            ("approved", "rejected"),
        )
        if final_decision:
            return final_decision == "approved"

        lowered = review_text.lower()
        if "rejected" in lowered:
            return False
        if "approved" in lowered:
            return True
        return False

    @staticmethod
    def review_requests_revision(review_text: str) -> bool:
        decision = WorkflowRunner.parse_decision_marker(
            review_text,
            "REVIEW_DECISION",
            ("approved", "rejected", "revise_required"),
        )
        if decision == "approved":
            return False
        if decision in ("rejected", "revise_required"):
            return True

        lowered = review_text.lower()
        if "approved" in lowered and "should revise" not in lowered and "needs revision" not in lowered:
            return False
        revision_markers = (
            "should revise",
            "needs revision",
            "required fixes",
            "must fix",
            "before final approval",
            "enter revision",
            "requires revision",
        )
        return any(marker in lowered for marker in revision_markers) or not WorkflowRunner.review_allows_completion(review_text)

    @staticmethod
    def parse_decision_marker(review_text: str, marker_name: str, allowed_values: tuple[str, ...]) -> str:
        prefix = marker_name.lower() + ":"
        allowed = {value.lower() for value in allowed_values}
        for raw_line in review_text.splitlines():
            line = raw_line.strip()
            lowered = line.lower()
            if not lowered.startswith(prefix):
                continue
            value_part = line[len(marker_name) + 1 :].strip().lower()
            if not value_part:
                return ""
            token = value_part.split()[0].strip("`*_.,;:()[]{}")
            if token in allowed:
                return token
            return ""
        return ""

    @staticmethod
    def contains_cjk(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    @staticmethod
    def resolve_command(raw: str) -> str:
        candidate = Path(raw)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)
        if candidate.exists():
            return str(candidate.resolve())
        found = shutil.which(raw)
        if found:
            return found

        appdata = Path.home() / "AppData" / "Roaming" / "npm"
        fallback = appdata / raw
        if fallback.exists():
            return str(fallback)
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coordinate Codex and Claude in the same project directory.")
    parser.add_argument("--task", default="", help="Task description. Optional when resuming an existing workflow.")
    parser.add_argument("--task-label", default="", help="Optional human-readable task label shown in workflow history.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project root directory")
    parser.add_argument(
        "--code-dir",
        type=Path,
        action="append",
        default=[],
        help="Code directory inside the project. Repeat this flag to allow multiple directories.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("workflow"), help="Directory for workflow artifacts")
    parser.add_argument("--codex-model", default="gpt-5.3-codex", help="Model name for codex exec/review")
    parser.add_argument(
        "--codex-reasoning-effort",
        default="",
        help="Fallback reasoning effort for Codex stages when a stage-specific effort is not provided.",
    )
    parser.add_argument("--codex-plan-reasoning-effort", default="xhigh", help="Reasoning effort for the Codex plan stage")
    parser.add_argument("--codex-review-reasoning-effort", default="high", help="Reasoning effort for the Codex review stage")
    parser.add_argument(
        "--codex-final-review-reasoning-effort",
        default="high",
        help="Reasoning effort for the Codex final review stage",
    )
    parser.add_argument(
        "--codex-wrapup-reasoning-effort",
        default="medium",
        help="Reasoning effort for the final summary/commit-note stage",
    )
    parser.add_argument("--claude-model", default="haiku", help="Model name for claude")
    parser.add_argument("--codex-cmd", default=CODEX_CMD, help="Codex CLI path or command name")
    parser.add_argument("--claude-cmd", default=CLAUDE_CMD, help="Claude CLI path or command name")
    parser.add_argument("--codex-extra-args", default="", help="Extra arguments passed to codex")
    parser.add_argument("--codex-review-extra-args", default="", help="Extra arguments passed to codex review stages")
    parser.add_argument("--claude-extra-args", default="", help="Extra arguments passed to claude implementation stage")
    parser.add_argument("--claude-revise-extra-args", default="", help="Extra arguments passed to claude revision stage")
    parser.add_argument(
        "--claude-permission-mode",
        default="acceptEdits",
        choices=["acceptEdits", "bypassPermissions", "default", "delegate", "dontAsk", "plan"],
        help="Permission mode for Claude stages",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from completed stages when artifacts already exist")
    parser.add_argument(
        "--retry-final-reject",
        action="store_true",
        help="After a rejected final review, ask Claude to revise again and let Codex perform another final review.",
    )
    parser.add_argument(
        "--commit-on-success",
        action="store_true",
        help="After 06 summary is generated, ask Codex to attempt one real git commit.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.code_dir:
        args.code_dir = [Path(".")]
    runner = WorkflowRunner(args)
    try:
        outcome = runner.run()
    except WorkflowError as exc:
        runner.manifest["status"] = "failed"
        runner.manifest["error"] = str(exc)
        runner.save_manifest()
        runner.log(f"FAILED {exc}")
        print(f"Workflow failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        runner.manifest["status"] = "interrupted"
        runner.save_manifest()
        runner.log("INTERRUPTED by user")
        print("Workflow interrupted by user.", file=sys.stderr)
        return 130

    if outcome == "needs_user_decision":
        print(
            "Workflow paused after a rejected final review. Decide later whether to trigger another Claude revision and Codex re-review.",
            file=sys.stderr,
        )
        return 2

    print(f"Workflow completed. Artifacts directory: {runner.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
