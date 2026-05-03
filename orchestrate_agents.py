#!/usr/bin/env python3
"""Coordinate a Codex -> Claude -> Codex -> Claude workflow in one workspace."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CODEX_CMD = "codex.cmd"
CLAUDE_CMD = "claude.cmd"

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

        if self.args.commit_on_success:
            self.run_commit_stage(plan_text, final_review_text)

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

        if self.args.commit_on_success:
            self.run_commit_stage(plan_text, final_review_text)

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

    def build_codex_exec_args(self, output_path: Path) -> list[str]:
        args = [str(self.codex_cmd), "exec", "--cd", str(self.primary_code_dir), "--output-last-message", str(output_path)]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        if self.args.codex_reasoning_effort:
            args.extend(["--config", f'model_reasoning_effort="{self.args.codex_reasoning_effort}"'])
        for code_dir in self.code_dirs[1:]:
            args.extend(["--add-dir", str(code_dir)])
        if self.args.codex_extra_args:
            args.extend(self.split_extra_args(self.args.codex_extra_args))
        return args

    def build_codex_review_args(self, output_path: Path) -> list[str]:
        args = [str(self.codex_cmd), "exec", "--cd", str(self.primary_code_dir), "--output-last-message", str(output_path)]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        if self.args.codex_reasoning_effort:
            args.extend(["--config", f'model_reasoning_effort="{self.args.codex_reasoning_effort}"'])
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
            "--ask-for-approval",
            "never",
            "--sandbox",
            "danger-full-access",
        ]
        if self.args.codex_model:
            args.extend(["--model", self.args.codex_model])
        if self.args.codex_reasoning_effort:
            args.extend(["--config", f'model_reasoning_effort="{self.args.codex_reasoning_effort}"'])
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
        if result.returncode != 0 or not result.stdout.strip():
            self.set_stage("implement", "failed", returncode=result.returncode)
            raise WorkflowError("Claude implementation stage failed")

        implementation_text = result.stdout.strip()
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
        result = self.run_command(self.build_codex_review_args(output_path), cwd=self.project_dir, stdin_text=prompt)
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
        if result.returncode != 0 or not result.stdout.strip():
            self.set_stage("revise", "failed", returncode=result.returncode)
            raise WorkflowError("Claude revision stage failed")

        revision_text = result.stdout.strip()
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
        result = self.run_command(self.build_codex_review_args(output_path), cwd=self.project_dir, stdin_text=prompt)
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
        if result.returncode != 0 or not result.stdout.strip():
            self.set_stage("revise", "failed", returncode=result.returncode, retry_round=retry_round)
            raise WorkflowError("Claude retry revision stage failed")

        revision_text = result.stdout.strip()
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
        result = self.run_command(self.build_codex_review_args(output_path), cwd=self.project_dir, stdin_text=prompt)
        review_text = read_text_if_exists(output_path) or result.stdout
        if result.returncode != 0 or not review_text.strip():
            self.set_stage("final_review", "failed", returncode=result.returncode, retry_round=retry_round)
            raise WorkflowError("Codex retry final review stage failed")
        self.write_artifact(FINAL_REVIEW_FILE, review_text)
        self.write_artifact(self.retry_history_file(retry_round, FINAL_REVIEW_FILE), review_text)
        self.set_stage("final_review", "completed", returncode=result.returncode, retry_round=retry_round)
        return review_text

    def run_commit_stage(self, plan_text: str, final_review_text: str) -> str:
        if self.should_skip("commit", COMMIT_FILE):
            return read_text_if_exists(self.artifact_path(COMMIT_FILE))

        self.ensure_git_repository_for_commit()

        if not self.has_git_changes():
            message = "No repository changes were detected, so the commit stage was skipped."
            self.write_artifact(COMMIT_FILE, message)
            self.set_stage("commit", "skipped", reason="no changes")
            return message

        head_before = self.git_head_commit()
        prompt = self.build_commit_prompt(plan_text, final_review_text)
        self.write_artifact(PROMPT_FILES["commit"], prompt)
        output_path = self.artifact_path(COMMIT_FILE)
        self.set_stage("commit", "running", artifact=str(output_path))
        result = self.run_command(self.build_codex_commit_args(output_path), cwd=self.project_dir, stdin_text=prompt)
        commit_text = read_text_if_exists(output_path) or result.stdout
        head_after = self.git_head_commit()

        if head_after and head_after != head_before:
            if not commit_text.strip():
                commit_text = (
                    "Codex created a git commit, but did not return a commit summary.\n\n"
                    f"Commit hash: {head_after}\n"
                )
            self.write_artifact(COMMIT_FILE, commit_text)
            self.set_stage(
                "commit",
                "completed",
                returncode=result.returncode,
                previous_head=head_before or None,
                commit_hash=head_after,
            )
            return commit_text

        failure_report = self.build_commit_failure_report(commit_text, result, head_before, head_after)
        self.write_artifact(COMMIT_FILE, failure_report)
        self.set_stage(
            "commit",
            "failed",
            returncode=result.returncode,
            reason="no_real_commit_created",
            previous_head=head_before or None,
            current_head=head_after or None,
        )
        raise WorkflowError("Codex commit stage did not create a real git commit. Approval may have been denied.")

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
            "Inspect the actual files on disk, not just a proposed patch, and produce a Markdown review report with at least:\n"
            "1. Whether the code satisfies the task\n"
            "2. Potential bugs or risks\n"
            "3. Style or architecture concerns\n"
            "4. Required fixes vs optional improvements\n"
            "5. Whether Claude should revise the code before final approval\n\n"
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
            "Inspect the actual files and output a Markdown final review containing:\n"
            "1. Final verdict\n"
            "2. Remaining risks, if any\n"
            "3. Test or manual verification recommendations\n"
            "4. A clear line stating either 'APPROVED' or 'REJECTED'\n\n"
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
            "You are Codex. The coding task has passed final review and now you need to create exactly one git commit.\n"
            "Operate in the current repository and commit only the real source changes related to this task.\n"
            "Do not commit files under the workflow directory.\n"
            "If you cannot actually create the commit because of permissions, approvals, or repository restrictions, say that clearly and do not claim success.\n"
            "Review the current git status, stage only relevant project files, create one concise commit, and then output:\n"
            "1. Commit message\n"
            "2. Commit hash\n"
            "3. Short summary of committed files\n\n"
            f"Task:\n{self.args.task}\n\n"
            f"Allowed code directories:\n{self.render_code_dir_list()}\n\n"
            f"Codex plan:\n{plan_text}\n\n"
            f"Final review:\n{final_review_text}\n"
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
            "# Commit Stage Failed\n\n"
            "Codex returned from the commit stage, but no real git commit was created.\n\n"
            f"- Return code: {result.returncode}\n"
            f"- HEAD before: {head_before or '(none)'}\n"
            f"- HEAD after: {head_after or '(none)'}\n\n"
            "## Codex output\n\n"
            f"{commit_text.strip() or '(empty)'}\n\n"
            "## STDERR\n\n"
            f"{result.stderr.strip() or '(empty)'}\n\n"
            "## Remaining git status in allowed code directories\n\n"
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
        lowered = review_text.lower()
        if "rejected" in lowered:
            return False
        if "approved" in lowered:
            return True
        return False

    @staticmethod
    def review_requests_revision(review_text: str) -> bool:
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
    parser.add_argument("--codex-reasoning-effort", default="xhigh", help="Reasoning effort for codex exec/review")
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
    parser.add_argument("--commit-on-success", action="store_true", help="Ask Codex to create one git commit after final review passes")
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
