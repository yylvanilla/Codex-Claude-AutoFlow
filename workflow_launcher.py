#!/usr/bin/env python3
"""Windows GUI launcher for the Codex-Claude workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import orchestrate_agents


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SCRIPT_DIR = Path(__file__).resolve().parent
ORCHESTRATOR = SCRIPT_DIR / "orchestrate_agents.py"
SETTINGS_FILE = APP_DIR / "launcher_settings.json"
INTERNAL_RUN_FLAG = "--run-orchestrator-internal"

DEFAULT_PROJECT_DIR = ""
DEFAULT_CODE_DIRS: list[str] = []
DEFAULT_TASK = (
    "描述这次需要修改的功能、范围和约束。"
    "建议写清楚目标文件区域、预期行为、禁止改动项，以及验收标准。"
)

CODEX_MODELS = ["gpt-5.3-codex", "gpt-5.4", "gpt-5.2"]
CODEX_REASONING_EFFORTS = ["xhigh", "high", "medium", "low"]
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]
CLAUDE_PERMISSION_MODES = ["acceptEdits", "dontAsk", "default", "delegate", "plan", "bypassPermissions"]

PERMISSION_MODE_HELP = {
    "acceptEdits": "允许 Claude 在获得文件编辑许可的前提下直接改代码，适合当前工作流。",
    "dontAsk": "尽量少询问，直接继续执行，适合你非常确定工作区允许自动修改时。",
    "default": "使用默认权限行为，通常比 acceptEdits 更保守。",
    "delegate": "偏代理式权限策略，通常不适合这里的直接改代码流程。",
    "plan": "只偏向做计划和建议，不适合这里的直接修改流程。",
    "bypassPermissions": "尽量绕过权限检查，风险最高，只建议在受控环境下使用。",
}

ARTIFACT_BUTTONS = [
    ("计划", "01_codex_plan.md"),
    ("初审", "03_codex_review.md"),
    ("终审", "05_codex_final_review.md"),
    ("提交记录", "06_codex_commit.md"),
    ("日志", "run.log"),
]

STAGE_LABELS = {
    "plan": "Codex 规划",
    "implement": "Claude 修改",
    "review": "Codex 初审",
    "revise": "Claude 修订",
    "final_review": "Codex 终审",
    "commit": "Codex 提交",
}

WORKFLOW_STATUS_LABELS = {
    "initialized": "未开始",
    "running": "运行中",
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "paused_by_user": "已暂停，可稍后继续",
    "needs_user_decision": "终审未通过，等待你决定是否再修再审",
}

STAGE_STATUS_LABELS = {
    "running": "进行中",
    "completed": "已完成",
    "failed": "失败",
    "skipped": "已跳过",
    "pending_retry": "等待本轮再审",
}


def creationflags_no_window() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def build_hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = 0
    return startupinfo


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class WorkflowLauncher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Codex-Claude Workflow Launcher")
        self.root.geometry("1220x860")

        settings = load_settings()
        self.project_var = tk.StringVar(value=settings.get("project_dir", DEFAULT_PROJECT_DIR))
        self.task_label_var = tk.StringVar(value=settings.get("task_label", ""))
        self.codex_model_var = tk.StringVar(value=settings.get("codex_model", "gpt-5.3-codex"))
        self.codex_effort_var = tk.StringVar(value=settings.get("codex_reasoning_effort", "xhigh"))
        self.claude_model_var = tk.StringVar(value=settings.get("claude_model", "haiku"))
        self.claude_permission_var = tk.StringVar(value=settings.get("claude_permission_mode", "acceptEdits"))
        self.commit_on_success_var = tk.BooleanVar(value=settings.get("commit_on_success", False))
        self.status_var = tk.StringVar(value="就绪")
        self.stage_var = tk.StringVar(value="当前阶段: 未开始")
        self.permission_help_var = tk.StringVar(value=PERMISSION_MODE_HELP["acceptEdits"])
        self.process: subprocess.Popen[str] | None = None
        self.process_active = False
        self.monitor_job: str | None = None
        self.hidden_startupinfo = build_hidden_startupinfo()
        self.history_items: list[dict] = []

        self._build_ui()
        self.set_code_dirs(settings.get("code_dirs", DEFAULT_CODE_DIRS))
        self.task_text.insert("1.0", settings.get("task", DEFAULT_TASK))
        self.update_permission_help()
        self.restore_workflow_context(force=True)
        self.update_artifact_buttons()

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, padx=12, pady=12)
        outer.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(outer)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right = tk.Frame(outer, padx=12)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        self._build_config_panel(left)
        self._build_log_panel(left)
        self._build_side_panel(right)

        status_bar = tk.Label(self.root, textvariable=self.status_var, anchor="w", relief=tk.SUNKEN)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _build_config_panel(self, parent: tk.Frame) -> None:
        frame = tk.LabelFrame(parent, text="运行配置", padx=10, pady=10)
        frame.pack(fill=tk.X)

        tk.Label(frame, text="工程目录").grid(row=0, column=0, sticky="w")
        tk.Entry(frame, textvariable=self.project_var, width=82).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        tk.Button(frame, text="选择...", command=self.choose_project_dir).grid(row=0, column=2, sticky="ew")

        tk.Label(frame, text="代码目录").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        list_frame = tk.Frame(frame)
        list_frame.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(10, 0))

        self.code_dir_listbox = tk.Listbox(list_frame, height=5, exportselection=False)
        self.code_dir_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)

        list_buttons = tk.Frame(list_frame, padx=8)
        list_buttons.pack(side=tk.RIGHT, fill=tk.Y)
        tk.Button(list_buttons, text="添加目录...", width=12, command=self.add_code_dir).pack(fill=tk.X)
        tk.Button(list_buttons, text="删除选中", width=12, command=self.remove_selected_code_dir).pack(fill=tk.X, pady=(6, 0))
        tk.Button(list_buttons, text="上移", width=12, command=self.move_code_dir_up).pack(fill=tk.X, pady=(6, 0))
        tk.Button(list_buttons, text="下移", width=12, command=self.move_code_dir_down).pack(fill=tk.X, pady=(6, 0))
        tk.Button(list_buttons, text="清空", width=12, command=self.clear_code_dirs).pack(fill=tk.X, pady=(6, 0))

        tk.Label(frame, text="任务标识").grid(row=2, column=0, sticky="w", pady=(12, 0))
        tk.Entry(frame, textvariable=self.task_label_var, width=82).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(12, 0))

        tk.Label(frame, text="Codex 模型选择").grid(row=3, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(frame, textvariable=self.codex_model_var, values=CODEX_MODELS, state="readonly", width=28).grid(
            row=3, column=1, sticky="w", padx=(8, 8), pady=(12, 0)
        )

        tk.Label(frame, text="Claude 模型选择").grid(row=3, column=2, sticky="w", pady=(12, 0))
        ttk.Combobox(frame, textvariable=self.claude_model_var, values=CLAUDE_MODELS, state="readonly", width=18).grid(
            row=3, column=2, sticky="e", pady=(12, 0)
        )

        tk.Label(frame, text="Codex 推理强度").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(frame, textvariable=self.codex_effort_var, values=CODEX_REASONING_EFFORTS, state="readonly", width=28).grid(
            row=4, column=1, sticky="w", padx=(8, 8), pady=(10, 0)
        )

        tk.Label(frame, text="Claude 权限模式").grid(row=4, column=2, sticky="w", pady=(10, 0))
        permission_combo = ttk.Combobox(
            frame,
            textvariable=self.claude_permission_var,
            values=CLAUDE_PERMISSION_MODES,
            state="readonly",
            width=18,
        )
        permission_combo.grid(row=4, column=2, sticky="e", pady=(10, 0))
        permission_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_permission_help())

        tk.Label(frame, text="权限模式说明").grid(row=5, column=0, sticky="nw", pady=(8, 0))
        tk.Label(frame, textvariable=self.permission_help_var, justify=tk.LEFT, wraplength=760, anchor="w").grid(
            row=5, column=1, columnspan=2, sticky="w", pady=(8, 0)
        )

        tk.Checkbutton(
            frame,
            text="这次任务成功后由 Codex 自动提交一次 commit（不提交 workflow 目录）",
            variable=self.commit_on_success_var,
        ).grid(row=6, column=1, columnspan=2, sticky="w", pady=(10, 0))

        tk.Label(frame, text="任务描述").grid(row=7, column=0, sticky="nw", pady=(12, 0))
        self.task_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=10)
        self.task_text.grid(row=7, column=1, columnspan=2, sticky="nsew", pady=(12, 0))

        button_frame = tk.Frame(frame)
        button_frame.grid(row=8, column=1, columnspan=2, sticky="w", pady=(12, 0))
        self.run_button = tk.Button(button_frame, text="开始运行", width=14, command=self.start_workflow)
        self.run_button.pack(side=tk.LEFT)
        self.resume_button = tk.Button(button_frame, text="继续上次任务", width=14, command=self.resume_workflow)
        self.resume_button.pack(side=tk.LEFT, padx=(8, 0))
        self.retry_button = tk.Button(button_frame, text="再修再审", width=14, command=self.retry_after_reject_workflow)
        self.retry_button.pack(side=tk.LEFT, padx=(8, 0))
        self.restart_button = tk.Button(button_frame, text="新任务草稿", width=14, command=self.restart_workflow)
        self.restart_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = tk.Button(button_frame, text="停止当前任务", width=14, command=self.stop_workflow)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

    def _build_log_panel(self, parent: tk.Frame) -> None:
        frame = tk.LabelFrame(parent, text="运行日志", padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.log_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=24, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _build_side_panel(self, parent: tk.Frame) -> None:
        status_frame = tk.LabelFrame(parent, text="阶段状态", padx=10, pady=10)
        status_frame.pack(fill=tk.X)
        tk.Label(status_frame, textvariable=self.stage_var, justify=tk.LEFT, anchor="w").pack(fill=tk.X)

        artifact_frame = tk.LabelFrame(parent, text="快捷产物", padx=10, pady=10)
        artifact_frame.pack(fill=tk.X, pady=(12, 0))
        self.artifact_buttons: dict[str, tk.Button] = {}
        for label, filename in ARTIFACT_BUTTONS:
            button = tk.Button(artifact_frame, text=label, width=18, command=lambda f=filename: self.open_artifact(f))
            button.pack(fill=tk.X, pady=2)
            self.artifact_buttons[filename] = button
        tk.Button(artifact_frame, text="打开 workflow 文件夹", width=18, command=self.open_workflow_dir).pack(fill=tk.X, pady=(8, 2))

        history_frame = tk.LabelFrame(parent, text="任务历史", padx=10, pady=10)
        history_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.history_listbox = tk.Listbox(history_frame, height=14, exportselection=False)
        self.history_listbox.pack(fill=tk.BOTH, expand=True)
        self.history_listbox.bind("<<ListboxSelect>>", lambda _event: self.update_history_action_buttons())

        history_button_frame = tk.Frame(history_frame)
        history_button_frame.pack(fill=tk.X, pady=(8, 0))
        tk.Button(history_button_frame, text="刷新列表", width=10, command=self.refresh_task_history_list).pack(side=tk.LEFT)
        self.restore_history_button = tk.Button(
            history_button_frame,
            text="恢复选中",
            width=10,
            command=self.restore_selected_history_workflow,
        )
        self.restore_history_button.pack(side=tk.LEFT, padx=(6, 0))
        self.open_history_button = tk.Button(
            history_button_frame,
            text="打开目录",
            width=10,
            command=self.open_selected_history_workflow,
        )
        self.open_history_button.pack(side=tk.LEFT, padx=(6, 0))

    def update_permission_help(self) -> None:
        mode = self.claude_permission_var.get().strip() or "acceptEdits"
        self.permission_help_var.set(PERMISSION_MODE_HELP.get(mode, "未提供说明。"))

    def choose_project_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.project_var.get() or str(SCRIPT_DIR))
        if selected:
            self.project_var.set(selected)
            self.restore_workflow_context(force=True)
            self.update_artifact_buttons()
            if self.workflow_dir().exists():
                self.status_var.set("已加载当前任务；要继续请点“继续上次任务”，要新开任务请点“新任务草稿”")
            else:
                self.status_var.set("就绪")

    def get_code_dirs(self) -> list[str]:
        return [self.code_dir_listbox.get(i) for i in range(self.code_dir_listbox.size())]

    def set_code_dirs(self, code_dirs: list[str]) -> None:
        self.code_dir_listbox.delete(0, tk.END)
        for code_dir in code_dirs:
            self.code_dir_listbox.insert(tk.END, code_dir)

    def add_code_dir(self) -> None:
        project_dir = Path(self.project_var.get().strip())
        initialdir = str(project_dir) if project_dir.exists() else str(SCRIPT_DIR)
        selected = filedialog.askdirectory(initialdir=initialdir)
        if not selected:
            return

        selected_path = Path(selected).resolve()
        display_value = str(selected_path)
        if project_dir.exists():
            try:
                display_value = str(selected_path.relative_to(project_dir.resolve())).replace("\\", "/")
            except ValueError:
                display_value = str(selected_path)

        code_dirs = self.get_code_dirs()
        if display_value not in code_dirs:
            self.code_dir_listbox.insert(tk.END, display_value)

    def remove_selected_code_dir(self) -> None:
        selection = self.code_dir_listbox.curselection()
        if not selection:
            return
        self.code_dir_listbox.delete(selection[0])

    def move_code_dir_up(self) -> None:
        selection = self.code_dir_listbox.curselection()
        if not selection or selection[0] == 0:
            return
        index = selection[0]
        value = self.code_dir_listbox.get(index)
        self.code_dir_listbox.delete(index)
        self.code_dir_listbox.insert(index - 1, value)
        self.code_dir_listbox.selection_set(index - 1)

    def move_code_dir_down(self) -> None:
        selection = self.code_dir_listbox.curselection()
        if not selection or selection[0] >= self.code_dir_listbox.size() - 1:
            return
        index = selection[0]
        value = self.code_dir_listbox.get(index)
        self.code_dir_listbox.delete(index)
        self.code_dir_listbox.insert(index + 1, value)
        self.code_dir_listbox.selection_set(index + 1)

    def clear_code_dirs(self) -> None:
        self.code_dir_listbox.delete(0, tk.END)

    def append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def set_task_text(self, text: str) -> None:
        self.task_text.delete("1.0", tk.END)
        self.task_text.insert("1.0", text)

    def save_current_settings(self) -> None:
        save_settings(
            {
                "project_dir": self.project_var.get().strip(),
                "task_label": self.task_label_var.get().strip(),
                "code_dirs": self.get_code_dirs(),
                "task": self.task_text.get("1.0", tk.END).strip(),
                "codex_model": self.codex_model_var.get().strip(),
                "codex_reasoning_effort": self.codex_effort_var.get().strip(),
                "claude_model": self.claude_model_var.get().strip(),
                "claude_permission_mode": self.claude_permission_var.get().strip(),
                "commit_on_success": self.commit_on_success_var.get(),
            }
        )

    def workflow_dir(self) -> Path:
        return Path(self.project_var.get().strip()) / "workflow"

    def workflow_history_dir(self) -> Path:
        return Path(self.project_var.get().strip()) / "workflow_history"

    def read_manifest_from_dir(self, workflow_dir: Path) -> dict | None:
        path = workflow_dir / "run_manifest.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def saved_task_label_from_workflow(self) -> str:
        manifest = self.read_manifest()
        if not manifest:
            return ""
        return str(manifest.get("task_label") or "").strip()

    def saved_task_id_from_workflow(self) -> str:
        manifest = self.read_manifest()
        if not manifest:
            return ""
        return str(manifest.get("task_id") or "").strip()

    @staticmethod
    def safe_name_part(text: str, max_length: int = 48) -> str:
        invalid = '<>:"/\\|?*'
        cleaned = "".join("_" if (ch in invalid or ord(ch) < 32) else ch for ch in text.strip())
        cleaned = cleaned.replace("\n", " ").replace("\r", " ")
        cleaned = cleaned.strip(" ._")
        if not cleaned:
            return ""
        return cleaned[:max_length].strip(" ._")

    @staticmethod
    def display_timestamp(raw: str) -> str:
        value = str(raw or "").strip()
        if not value:
            return ""
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return value

    def summarize_workflow_dir(self, workflow_dir: Path, source: str) -> dict | None:
        if not workflow_dir.exists():
            return None

        manifest = self.read_manifest_from_dir(workflow_dir) or {}
        task_file = workflow_dir / "00_task.txt"
        if not manifest and not task_file.exists():
            return None
        task_text = str(manifest.get("task") or "").strip()
        if not task_text and task_file.exists():
            task_text = task_file.read_text(encoding="utf-8", errors="replace").strip()

        task_id = str(manifest.get("task_id") or "").strip() or workflow_dir.name
        task_label = str(manifest.get("task_label") or "").strip()
        status_key = str(manifest.get("status") or "").strip()
        updated_raw = str(manifest.get("updated_at") or "").strip()
        if not updated_raw:
            updated_raw = datetime.fromtimestamp(workflow_dir.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")

        preview = task_label or (task_text.splitlines()[0].strip() if task_text else workflow_dir.name)
        preview = preview[:40]

        return {
            "source": source,
            "dir": str(workflow_dir),
            "task_id": task_id,
            "task_label": task_label,
            "task_text": task_text,
            "preview": preview,
            "status_key": status_key,
            "status_label": WORKFLOW_STATUS_LABELS.get(status_key, status_key or "未知"),
            "updated_at": updated_raw,
            "updated_label": self.display_timestamp(updated_raw),
        }

    def format_history_item_text(self, item: dict) -> str:
        source_text = "当前" if item.get("source") == "current" else "历史"
        label = item.get("task_label") or item.get("preview") or "(未命名任务)"
        task_id = item.get("task_id") or "unknown-id"
        status = item.get("status_label") or "未知"
        updated = item.get("updated_label") or "未知时间"
        return f"[{source_text}] {label} | {task_id} | {status} | {updated}"

    def refresh_task_history_list(self) -> None:
        project_dir_text = self.project_var.get().strip()
        if not project_dir_text:
            self.history_items = []
            self.history_listbox.delete(0, tk.END)
            self.update_history_action_buttons()
            return

        selected_item = self.selected_history_item()
        selected_dir = selected_item.get("dir") if selected_item else ""

        items: list[dict] = []
        current_summary = self.summarize_workflow_dir(self.workflow_dir(), "current")
        if current_summary:
            items.append(current_summary)

        history_root = self.workflow_history_dir()
        if history_root.exists():
            history_dirs = [path for path in history_root.iterdir() if path.is_dir()]
            history_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            for history_dir in history_dirs:
                summary = self.summarize_workflow_dir(history_dir, "history")
                if summary:
                    items.append(summary)

        self.history_items = items
        self.history_listbox.delete(0, tk.END)
        selected_index = None
        for index, item in enumerate(items):
            self.history_listbox.insert(tk.END, self.format_history_item_text(item))
            if selected_dir and item.get("dir") == selected_dir:
                selected_index = index

        if selected_index is None and items:
            selected_index = 0
        if selected_index is not None:
            self.history_listbox.selection_set(selected_index)

        self.update_history_action_buttons()

    def selected_history_item(self) -> dict | None:
        selection = getattr(self, "history_listbox", None)
        if selection is None:
            return None
        selected = self.history_listbox.curselection()
        if not selected:
            return None
        index = selected[0]
        if index < 0 or index >= len(self.history_items):
            return None
        return self.history_items[index]

    def update_history_action_buttons(self) -> None:
        selected_item = self.selected_history_item()
        is_busy = self.process_active or self.process is not None
        if selected_item is None or is_busy:
            restore_state = tk.DISABLED
            open_state = tk.DISABLED
        else:
            open_state = tk.NORMAL
            restore_state = tk.DISABLED if selected_item.get("source") == "current" else tk.NORMAL

        self.restore_history_button.configure(state=restore_state)
        self.open_history_button.configure(state=open_state)

    def restore_selected_history_workflow(self) -> None:
        item = self.selected_history_item()
        if item is None:
            messagebox.showinfo("提示", "请先在任务历史里选择一个任务。")
            return
        if item.get("source") == "current":
            messagebox.showinfo("提示", "当前选中的已经是活动任务。")
            return

        source_dir = Path(str(item.get("dir")))
        if not source_dir.exists():
            messagebox.showerror("恢复失败", "选中的历史任务目录不存在，列表将自动刷新。")
            self.refresh_task_history_list()
            return

        answer = messagebox.askyesno(
            "确认恢复",
            "恢复这个历史任务后，它会变成当前 workflow。\n如果当前已经有活动任务，会先自动归档到 workflow_history。继续吗？",
        )
        if not answer:
            return

        workflow_dir = self.workflow_dir()
        try:
            if workflow_dir.exists():
                self.archive_existing_workflow()
            shutil.move(str(source_dir), str(workflow_dir))
        except OSError as exc:
            messagebox.showerror("恢复失败", f"无法恢复历史任务：{exc}")
            return

        self.clear_log()
        self.restore_workflow_context(force=True)
        self.update_stage_status()
        self.update_artifact_buttons()
        self.append_log(f"已恢复历史任务为当前 workflow: {item.get('task_id', workflow_dir.name)}\n")
        messagebox.showinfo("恢复成功", "已把选中的历史任务恢复为当前任务。")

    def open_selected_history_workflow(self) -> None:
        item = self.selected_history_item()
        if item is None:
            messagebox.showinfo("提示", "请先在任务历史里选择一个任务。")
            return
        target_dir = Path(str(item.get("dir")))
        if not target_dir.exists():
            messagebox.showerror("打开失败", "选中的任务目录不存在，列表将自动刷新。")
            self.refresh_task_history_list()
            return
        os.startfile(str(target_dir))

    def saved_task_from_workflow(self) -> str:
        manifest = self.read_manifest()
        if manifest:
            task = str(manifest.get("task") or "").strip()
            if task:
                return task
        task_path = self.workflow_dir() / "00_task.txt"
        if task_path.exists():
            return task_path.read_text(encoding="utf-8", errors="replace").strip()
        return ""

    def saved_code_dirs_from_workflow(self) -> list[str]:
        manifest = self.read_manifest()
        if not manifest:
            return []
        raw_dirs = manifest.get("code_dirs", [])
        if not isinstance(raw_dirs, list):
            return []
        return [str(item) for item in raw_dirs if str(item).strip()]

    def normalize_code_dir_display(self, code_dir: str) -> str:
        project_dir = Path(self.project_var.get().strip())
        path = Path(code_dir)
        if project_dir.exists():
            try:
                return str(path.resolve().relative_to(project_dir.resolve())).replace("\\", "/")
            except Exception:
                return str(path)
        return str(path)

    def restore_workflow_context(self, *, force: bool = False) -> None:
        manifest = self.read_manifest()
        saved_code_dirs = self.saved_code_dirs_from_workflow()
        if saved_code_dirs:
            self.set_code_dirs([self.normalize_code_dir_display(code_dir) for code_dir in saved_code_dirs])
        elif self.project_var.get().strip():
            self.set_code_dirs([])

        saved_task = self.saved_task_from_workflow()
        current_task = self.task_text.get("1.0", tk.END).strip()
        if saved_task and (force or not current_task or current_task == DEFAULT_TASK.strip()):
            self.task_text.delete("1.0", tk.END)
            self.task_text.insert("1.0", saved_task)

        if manifest is not None:
            self.task_label_var.set(str(manifest.get("task_label") or "").strip())

        self.update_resume_related_buttons()
        self.refresh_task_history_list()

    def manifest_path(self) -> Path:
        return self.workflow_dir() / "run_manifest.json"

    def read_manifest(self) -> dict | None:
        path = self.manifest_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def stage_retry_round(stage: dict | None) -> int:
        if not stage:
            return 0
        try:
            return int(stage.get("retry_round", 0) or 0)
        except Exception:
            return 0

    def format_stage_status(self, stage_key: str, stage: dict | None, manifest: dict) -> str:
        if not stage:
            return "未开始"

        raw_status = str(stage.get("status", "unknown") or "unknown")
        active_retry_round = int(manifest.get("active_retry_round", 0) or 0)
        stage_retry_round = self.stage_retry_round(stage)
        manifest_status = str(manifest.get("status", "") or "")

        if (
            stage_key == "final_review"
            and raw_status == "completed"
            and active_retry_round > stage_retry_round
            and manifest_status in {"running", "paused_by_user", "needs_user_decision"}
        ):
            return f"等待第 {active_retry_round} 轮终审"

        if (
            stage_key == "final_review"
            and raw_status == "completed"
            and manifest_status == "needs_user_decision"
        ):
            round_text = f"第 {stage_retry_round} 轮" if stage_retry_round > 0 else "当前轮"
            return f"{round_text}未通过"

        label = STAGE_STATUS_LABELS.get(raw_status, raw_status)
        if raw_status == "running" and stage_retry_round > 0:
            return f"第 {stage_retry_round} 轮{label}"
        if raw_status == "completed" and stage_retry_round > 0:
            return f"第 {stage_retry_round} 轮{label}"
        if raw_status == "pending_retry" and stage_retry_round > 0:
            return f"等待第 {stage_retry_round} 轮终审"
        return label

    @staticmethod
    def format_round_line(manifest: dict) -> str | None:
        active_retry_round = int(manifest.get("active_retry_round", 0) or 0)
        retry_round = int(manifest.get("retry_round", 0) or 0)
        manifest_status = str(manifest.get("status", "") or "")

        if active_retry_round > 0 and manifest_status in {"running", "paused_by_user", "needs_user_decision"}:
            return f"当前轮次: 再修再审第 {active_retry_round} 轮"
        if retry_round > 0 and manifest_status == "completed":
            return f"完成轮次: 再修再审第 {retry_round} 轮"
        return None

    def update_stage_status(self) -> None:
        manifest = self.read_manifest()
        if not manifest:
            self.stage_var.set("当前阶段: 未开始")
            return

        raw_status = str(manifest.get("status", "unknown"))
        status = WORKFLOW_STATUS_LABELS.get(raw_status, raw_status)
        stages = manifest.get("stages", {})
        lines = [f"工作流状态: {status}"]
        round_line = self.format_round_line(manifest)
        if round_line:
            lines.append(round_line)
        for key, label in STAGE_LABELS.items():
            stage = stages.get(key)
            lines.append(f"{label}: {self.format_stage_status(key, stage, manifest)}")
        self.stage_var.set("\n".join(lines))

    def set_pending_stage_status(self, action_label: str) -> None:
        lines = [f"工作流状态: {action_label}"]
        first = True
        for _key, label in STAGE_LABELS.items():
            lines.append(f"{label}: {'等待启动' if first else '未开始'}")
            first = False
        self.stage_var.set("\n".join(lines))

    def update_artifact_buttons(self) -> None:
        workflow_dir = self.workflow_dir()
        for filename, button in self.artifact_buttons.items():
            button.configure(state=tk.NORMAL if (workflow_dir / filename).exists() else tk.DISABLED)
        self.refresh_task_history_list()
        self.update_resume_related_buttons()

    def update_resume_related_buttons(self) -> None:
        has_current_workflow = self.workflow_dir().exists()
        is_busy = self.process_active or self.process is not None
        self.resume_button.configure(state=tk.DISABLED if (is_busy or not has_current_workflow) else tk.NORMAL)
        can_retry = self.can_retry_after_reject()
        button_state = tk.DISABLED if is_busy else (tk.NORMAL if can_retry else tk.DISABLED)
        self.retry_button.configure(state=button_state)
        self.update_history_action_buttons()

    def can_retry_after_reject(self) -> bool:
        manifest = self.read_manifest()
        if manifest and str(manifest.get("status", "")) == "needs_user_decision":
            return True
        final_review_path = self.workflow_dir() / "05_codex_final_review.md"
        if not final_review_path.exists():
            return False
        final_review = final_review_path.read_text(encoding="utf-8", errors="replace")
        return "REJECTED" in final_review.upper()

    def current_task_text(self) -> str:
        return self.task_text.get("1.0", tk.END).strip()

    def current_task_label_text(self) -> str:
        return self.task_label_var.get().strip()

    def build_command(self, *, resume: bool = False, retry_final_reject: bool = False) -> list[str] | None:
        project_dir = Path(self.project_var.get().strip())
        code_dirs = self.get_code_dirs()
        if (resume or retry_final_reject) and not code_dirs:
            code_dirs = [self.normalize_code_dir_display(code_dir) for code_dir in self.saved_code_dirs_from_workflow()]

        task = self.current_task_text()
        saved_task = self.saved_task_from_workflow()
        if (resume or retry_final_reject) and saved_task:
            task = saved_task
        task_label = self.current_task_label_text()
        saved_task_label = self.saved_task_label_from_workflow()
        if (resume or retry_final_reject) and saved_task_label:
            task_label = saved_task_label

        codex_model = self.codex_model_var.get().strip()
        codex_effort = self.codex_effort_var.get().strip()
        claude_model = self.claude_model_var.get().strip()
        claude_permission = self.claude_permission_var.get().strip()

        if not project_dir.exists():
            messagebox.showerror("错误", "工程目录不存在。")
            return None
        if not code_dirs:
            messagebox.showerror("错误", "至少需要填写一个代码目录。")
            return None
        if not task and not (resume or retry_final_reject):
            messagebox.showerror("错误", "任务描述不能为空。")
            return None
        if not task and (resume or retry_final_reject):
            messagebox.showerror("错误", "没有找到可恢复任务的历史任务描述。")
            return None
        if not codex_model or not codex_effort or not claude_model or not claude_permission:
            messagebox.showerror("错误", "请完整选择 Codex 和 Claude 的配置。")
            return None

        orchestrator_args = [
            "--task",
            task,
            "--task-label",
            task_label,
            "--project-dir",
            str(project_dir),
            "--codex-model",
            codex_model,
            "--codex-reasoning-effort",
            codex_effort,
            "--claude-model",
            claude_model,
            "--claude-permission-mode",
            claude_permission,
        ]
        if self.commit_on_success_var.get():
            orchestrator_args.append("--commit-on-success")
        for code_dir in code_dirs:
            orchestrator_args.extend(["--code-dir", code_dir])
        if resume:
            orchestrator_args.append("--resume")
        if retry_final_reject:
            orchestrator_args.append("--retry-final-reject")

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, INTERNAL_RUN_FLAG, *orchestrator_args]
        else:
            cmd = [sys.executable, str(ORCHESTRATOR), *orchestrator_args]
        return cmd

    def start_process(self, cmd: list[str], action_label: str) -> None:
        if self.process is not None:
            messagebox.showinfo("提示", "当前已有任务在运行，请等待结束。")
            return

        self.save_current_settings()
        self.run_button.configure(state=tk.DISABLED)
        self.resume_button.configure(state=tk.DISABLED)
        self.retry_button.configure(state=tk.DISABLED)
        self.restart_button.configure(state=tk.DISABLED)
        self.process_active = True
        self.update_history_action_buttons()
        self.status_var.set(f"{action_label}...")
        self.set_pending_stage_status(action_label)
        self.append_log(f"\n=== {action_label} ===\n")
        self.append_log("命令: " + subprocess.list2cmdline(cmd) + "\n\n")

        worker = threading.Thread(target=self._run_process, args=(cmd,), daemon=True)
        worker.start()
        self.monitor_job = self.root.after(300, self.schedule_monitor)

    def start_workflow(self) -> None:
        if self.workflow_dir().exists():
            messagebox.showinfo(
                "提示",
                "当前工程目录下已经存在 workflow。\n\n如果你要继续原任务，请点击“继续上次任务”或“再修再审”；如果你要开始一个新的任务，请先点击“新任务草稿”，修改任务描述后再点击“开始运行”。",
            )
            return
        cmd = self.build_command(resume=False, retry_final_reject=False)
        if cmd is not None:
            self.start_process(cmd, "启动任务")

    def resume_workflow(self) -> None:
        if self.can_retry_after_reject():
            answer = messagebox.askyesno("提示", "当前任务处于终审未通过状态。是否直接进入“再修再审”？")
            if answer:
                self.retry_after_reject_workflow()
            return

        cmd = self.build_command(resume=True, retry_final_reject=False)
        if cmd is not None:
            self.start_process(cmd, "继续任务")

    def retry_after_reject_workflow(self) -> None:
        cmd = self.build_command(resume=False, retry_final_reject=True)
        if cmd is not None:
            self.start_process(cmd, "再修再审")

    def restart_workflow(self) -> None:
        workflow_dir = self.workflow_dir()
        if workflow_dir.exists():
            answer = messagebox.askyesno(
                "确认",
                "准备新任务会先把当前 workflow 归档到 workflow_history，然后清空当前任务名称和任务描述。\n\n这个操作不会立即运行。继续吗？",
            )
            if not answer:
                return
            try:
                archived_to = self.archive_existing_workflow()
            except OSError as exc:
                messagebox.showerror("归档失败", f"无法归档旧 workflow：{exc}")
                return
            self.clear_log()
            if archived_to is not None:
                self.append_log(f"已归档旧 workflow 到: {archived_to}\n")

        self.task_label_var.set("")
        self.set_task_text(DEFAULT_TASK)
        self.update_artifact_buttons()
        self.set_pending_stage_status("已准备新任务草稿")
        self.status_var.set("已切换到新任务草稿，请先修改任务描述，再点击“开始运行”")
        self.append_log("\n=== 已切换到新任务草稿 ===\n")
        self.append_log("当前不会自动运行，请修改任务描述后，再点击“开始运行”。\n")
        self.task_text.focus_set()
        self.task_text.mark_set(tk.INSERT, "1.0")
        self.task_text.see(tk.INSERT)
        messagebox.showinfo("已准备新任务", "已经切换到新任务草稿。\n\n请修改任务描述后，再点击“开始运行”。")

    def archive_existing_workflow(self) -> Path | None:
        workflow_dir = self.workflow_dir()
        if not workflow_dir.exists():
            return None

        history_root = self.workflow_history_dir()
        history_root.mkdir(parents=True, exist_ok=True)

        summary = self.summarize_workflow_dir(workflow_dir, "current")
        task_id = str(summary.get("task_id") or "") if summary else ""
        task_name = ""
        if summary:
            task_name = str(summary.get("task_label") or summary.get("preview") or "")
        if not task_id:
            task_id = "workflow_" + datetime.now().strftime("%Y%m%d_%H%M%S")

        base_name = task_id
        safe_name = self.safe_name_part(task_name)
        if safe_name:
            base_name = f"{base_name}__{safe_name}"

        archive_dir = history_root / base_name
        counter = 1
        while archive_dir.exists():
            counter += 1
            archive_dir = history_root / f"{base_name}_{counter}"

        shutil.move(str(workflow_dir), str(archive_dir))
        return archive_dir

    def stop_workflow(self) -> None:
        if self.process is None:
            if self.process_active:
                messagebox.showinfo("提示", "任务正在启动，请稍等片刻再停止。")
                return
            messagebox.showinfo("提示", "当前没有正在运行的任务。")
            return
        answer = messagebox.askyesno("确认", "要停止当前任务吗？")
        if not answer:
            return
        subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, text=True)
        self.mark_workflow_paused()
        self.status_var.set("正在停止任务...")

    def mark_workflow_paused(self) -> None:
        path = self.manifest_path()
        if not path.exists():
            return
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        manifest["status"] = "paused_by_user"
        manifest["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    def _run_process(self, cmd: list[str]) -> None:
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags_no_window(),
                startupinfo=self.hidden_startupinfo,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.root.after(0, self.append_log, line)
            returncode = self.process.wait()
            self.root.after(0, self.on_process_finished, returncode)
        except Exception as exc:
            self.root.after(0, self.status_var.set, "启动失败")
            self.root.after(0, self.append_log, f"\n启动失败: {exc}\n")
            self.root.after(0, messagebox.showerror, "启动失败", str(exc))
        finally:
            self.process_active = False
            self.process = None

    def on_process_finished(self, returncode: int) -> None:
        self.run_button.configure(state=tk.NORMAL)
        self.resume_button.configure(state=tk.NORMAL)
        self.restart_button.configure(state=tk.NORMAL)
        self.update_stage_status()
        self.update_artifact_buttons()

        final_review_path = self.workflow_dir() / "05_codex_final_review.md"
        commit_path = self.workflow_dir() / "06_codex_commit.md"

        manifest = self.read_manifest() or {}
        manifest_status = str(manifest.get("status", ""))
        commit_stage = manifest.get("stages", {}).get("commit", {})
        commit_stage_status = str(commit_stage.get("status", "") or "")

        if manifest_status == "needs_user_decision" or returncode == 2:
            self.status_var.set("等待你决定是否再修再审")
            self.append_log("\n=== 终审未通过，等待你决定是否再修再审 ===\n")
            answer = messagebox.askyesno(
                "终审未通过",
                "Codex 终审没有通过。是否现在立刻让 Claude 再改一次，并让 Codex 再审？\n\n选择“否”后，你以后重新打开 exe，选择同一个工程目录，点击“再修再审”也可以继续。",
            )
            if answer:
                self.retry_after_reject_workflow()
            return

        if manifest_status == "paused_by_user":
            self.status_var.set("任务已暂停")
            self.append_log("\n=== 任务已暂停，可稍后继续 ===\n")
            messagebox.showinfo(
                "任务已暂停",
                "当前任务已经暂停。\n\n以后重新打开 exe，选择同一个工程目录后，可以点击“继续上次任务”；如果当时停在终审未通过后的等待阶段，也可以点击“再修再审”。",
            )
            return

        if returncode == 0:
            self.status_var.set("运行完成")
            self.append_log("\n=== 任务完成 ===\n")
            final_review = final_review_path.read_text(encoding="utf-8", errors="replace") if final_review_path.exists() else ""
            if "APPROVED" in final_review.upper():
                if self.commit_on_success_var.get() and commit_stage_status == "completed":
                    messagebox.showinfo("任务完成", "最终审查通过，并且已确认由 Codex 真正创建了 git commit。")
                elif self.commit_on_success_var.get() and commit_stage_status == "skipped":
                    messagebox.showinfo("任务完成", "最终审查通过，但没有检测到可提交的代码改动，所以跳过了 commit 阶段。")
                elif self.commit_on_success_var.get() and commit_path.exists():
                    messagebox.showwarning("任务完成", "最终审查通过，但 commit 阶段没有被标记为成功，请打开 06_codex_commit.md 检查原因。")
                else:
                    messagebox.showinfo("任务完成", "最终审查通过，任务已完成。")
            else:
                messagebox.showwarning("任务完成", "任务已结束，但没有检测到明确的 APPROVED。")
        else:
            self.status_var.set(f"运行失败，退出码 {returncode}")
            self.append_log(f"\n=== 任务失败，退出码 {returncode} ===\n")
            if commit_stage_status == "failed":
                messagebox.showerror("任务失败", "最终审查虽然通过了，但 Codex 没有真正完成 git commit。请打开 06_codex_commit.md 查看原因。")
            elif final_review_path.exists() and "REJECTED" in final_review_path.read_text(encoding="utf-8", errors="replace").upper():
                messagebox.showerror("任务失败", "最终审查未通过，结果为 REJECTED。")
            else:
                messagebox.showerror("任务失败", f"任务失败，退出码 {returncode}。")

    def schedule_monitor(self) -> None:
        self.update_stage_status()
        self.update_artifact_buttons()
        if self.process_active or self.process is not None:
            self.monitor_job = self.root.after(1500, self.schedule_monitor)
        else:
            self.monitor_job = None

    def open_workflow_dir(self) -> None:
        workflow_dir = self.workflow_dir()
        if not workflow_dir.exists():
            messagebox.showinfo("提示", "当前工程下还没有 workflow 目录。")
            return
        os.startfile(str(workflow_dir))

    def open_artifact(self, filename: str) -> None:
        path = self.workflow_dir() / filename
        if not path.exists():
            messagebox.showinfo("提示", f"{filename} 尚未生成。")
            return
        os.startfile(str(path))


def main() -> int:
    if INTERNAL_RUN_FLAG in sys.argv[1:]:
        remaining = [arg for arg in sys.argv[1:] if arg != INTERNAL_RUN_FLAG]
        sys.argv = [sys.argv[0], *remaining]
        return orchestrate_agents.main()

    root = tk.Tk()
    WorkflowLauncher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
