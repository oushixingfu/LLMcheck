from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any
import json
import threading

from llmcheck.batch import run_batch
from llmcheck.pipeline import DEFAULT_LLM_CHUNK_CHARS, LlmCheckSettings, process_documents
from llmcheck.preprocess import (
    DEFAULT_MINERU_BATCH_SIZE,
    DEFAULT_MINERU_MAX_RETRIES,
    DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MINERU_RETRY_BACKOFF_SECONDS,
    DEFAULT_PDF_PAGE_CHUNK_SIZE,
    DEFAULT_PPX_COMMAND,
    DEFAULT_PPX_CWD,
)
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles


DESKTOP_MENU_LABELS = ("文件", "运行", "Profile", "报告", "帮助")
DESKTOP_SECTION_LABELS = ("任务", "Profile", "LLM", "高级设置", "批处理", "运行与报告")


@dataclass(frozen=True)
class DesktopFormValues:
    input_path: Path
    output_dir: Path
    profile_id: str = DEFAULT_PROFILE_ID
    llm_api_url: str = "http://127.0.0.1:3022"
    llm_api_key: str = "123"
    llm_model: str = "deepseek-v4-pro"
    concurrency: int = 10
    llm_chunk_chars: int = DEFAULT_LLM_CHUNK_CHARS
    acceptance_repair_rounds: int = 1
    timeout_seconds: int = 600
    mineru_api_url: str = "https://mineru.net"
    mineru_api_key: str = ""
    mineru_concurrency: int = 12
    mineru_batch_size: int = DEFAULT_MINERU_BATCH_SIZE
    mineru_timeout_seconds: int = 3600
    mineru_request_timeout_seconds: int = DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS
    mineru_max_retries: int = DEFAULT_MINERU_MAX_RETRIES
    mineru_retry_backoff_seconds: float = DEFAULT_MINERU_RETRY_BACKOFF_SECONDS
    pdf_page_chunk_size: int = DEFAULT_PDF_PAGE_CHUNK_SIZE
    enable_ppx: bool = False
    mineru_fallback: str = "none"
    ppx_command: str = DEFAULT_PPX_COMMAND
    ppx_cwd: str = DEFAULT_PPX_CWD
    ppx_timeout_seconds: int = 3600
    ppx_backend: str = "default"
    ppx_ocr: str = "auto"
    ppx_formula: str = "no"
    book_concurrency: int = 1
    start_index: int = 1
    limit: int = 0
    force: bool = False


def desktop_profile_options() -> list[tuple[str, str]]:
    return [(str(profile["id"]), str(profile.get("label") or profile["id"])) for profile in list_profiles()]


def build_settings_from_form(values: DesktopFormValues) -> LlmCheckSettings:
    return LlmCheckSettings(
        llm_api_url=values.llm_api_url.strip(),
        llm_api_key=values.llm_api_key.strip(),
        llm_model=values.llm_model.strip(),
        profile_id=get_profile(values.profile_id).id,
        concurrency=max(1, int(values.concurrency)),
        timeout_seconds=max(10, int(values.timeout_seconds)),
        llm_chunk_chars=max(1000, int(values.llm_chunk_chars)),
        acceptance_repair_rounds=max(0, int(values.acceptance_repair_rounds)),
        mineru_api_url=values.mineru_api_url.strip(),
        mineru_api_key=values.mineru_api_key.strip(),
        mineru_model="vlm",
        mineru_concurrency=max(1, int(values.mineru_concurrency)),
        mineru_batch_size=max(1, int(values.mineru_batch_size)),
        mineru_timeout_seconds=max(10, int(values.mineru_timeout_seconds)),
        mineru_request_timeout_seconds=max(10, int(values.mineru_request_timeout_seconds)),
        mineru_max_retries=max(1, int(values.mineru_max_retries)),
        mineru_retry_backoff_seconds=max(0.0, float(values.mineru_retry_backoff_seconds)),
        pdf_page_chunk_size=max(1, int(values.pdf_page_chunk_size)),
        enable_ppx=bool(values.enable_ppx),
        mineru_fallback=("ppx" if values.enable_ppx and str(values.mineru_fallback or "none").strip().lower() == "ppx" else "none"),
        ppx_command=values.ppx_command.strip(),
        ppx_cwd=values.ppx_cwd.strip(),
        ppx_timeout_seconds=max(10, int(values.ppx_timeout_seconds)),
        ppx_backend=values.ppx_backend.strip() or "default",
        ppx_ocr=values.ppx_ocr.strip() or "auto",
        ppx_formula=values.ppx_formula.strip() or "no",
    )


def should_run_batch(input_path: Path) -> bool:
    return input_path.expanduser().is_dir()


def run_desktop_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as error:
        raise RuntimeError("当前 Python 环境缺少 tkinter，无法启动原生桌面 GUI。") from error

    root = tk.Tk()
    app = DesktopGuiApp(root, tk_module=tk, ttk_module=ttk, messagebox_module=messagebox)
    app.show()
    root.mainloop()
    return 0


class DesktopGuiApp:
    def __init__(self, root: Any, *, tk_module: Any, ttk_module: Any, messagebox_module: Any) -> None:
        self.root = root
        self.tk = tk_module
        self.ttk = ttk_module
        self.messagebox = messagebox_module
        self.log_queue: Queue[tuple[str, Any]] = Queue()
        self.running = False
        self.profile_by_label = {f"{label} ({profile_id})": profile_id for profile_id, label in desktop_profile_options()}
        self.label_by_profile = {profile_id: f"{label} ({profile_id})" for profile_id, label in desktop_profile_options()}
        self.vars: dict[str, Any] = {}
        self.log_text: Any | None = None
        self.progress_var: Any | None = None
        self.status_var: Any | None = None

    def show(self) -> None:
        self.root.title("LLMcheck 桌面版")
        self.root.geometry("1040x760")
        self.root.minsize(900, 680)
        self._configure_style()
        self._create_variables()
        self._build_menu()
        self._build_layout()
        self._poll_log_queue()

    def _configure_style(self) -> None:
        style = self.ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#f5f7fb")
        style.configure("TLabelframe", background="#f5f7fb")
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabel", background="#f5f7fb")
        style.configure("Primary.TButton", padding=(12, 6))

    def _create_variables(self) -> None:
        tk = self.tk
        self.vars = {
            "input_path": tk.StringVar(value=""),
            "output_dir": tk.StringVar(value=""),
            "profile_label": tk.StringVar(value=self.label_by_profile.get(DEFAULT_PROFILE_ID, DEFAULT_PROFILE_ID)),
            "llm_api_url": tk.StringVar(value="http://127.0.0.1:3022"),
            "llm_api_key": tk.StringVar(value="123"),
            "llm_model": tk.StringVar(value="deepseek-v4-pro"),
            "concurrency": tk.StringVar(value="10"),
            "llm_chunk_chars": tk.StringVar(value=str(DEFAULT_LLM_CHUNK_CHARS)),
            "acceptance_repair_rounds": tk.StringVar(value="1"),
            "timeout_seconds": tk.StringVar(value="600"),
            "mineru_api_url": tk.StringVar(value="https://mineru.net"),
            "mineru_api_key": tk.StringVar(value=""),
            "mineru_concurrency": tk.StringVar(value="12"),
            "mineru_batch_size": tk.StringVar(value=str(DEFAULT_MINERU_BATCH_SIZE)),
            "mineru_timeout_seconds": tk.StringVar(value="3600"),
            "mineru_request_timeout_seconds": tk.StringVar(value=str(DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS)),
            "mineru_max_retries": tk.StringVar(value=str(DEFAULT_MINERU_MAX_RETRIES)),
            "mineru_retry_backoff_seconds": tk.StringVar(value=str(DEFAULT_MINERU_RETRY_BACKOFF_SECONDS)),
            "pdf_page_chunk_size": tk.StringVar(value=str(DEFAULT_PDF_PAGE_CHUNK_SIZE)),
            "enable_ppx": tk.BooleanVar(value=False),
            "mineru_fallback": tk.StringVar(value="none"),
            "ppx_command": tk.StringVar(value=DEFAULT_PPX_COMMAND),
            "ppx_cwd": tk.StringVar(value=DEFAULT_PPX_CWD),
            "ppx_timeout_seconds": tk.StringVar(value="3600"),
            "ppx_backend": tk.StringVar(value="default"),
            "ppx_ocr": tk.StringVar(value="auto"),
            "ppx_formula": tk.StringVar(value="no"),
            "book_concurrency": tk.StringVar(value="1"),
            "start_index": tk.StringVar(value="1"),
            "limit": tk.StringVar(value="0"),
            "force": tk.BooleanVar(value=False),
        }
        self.status_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0.0)

    def _build_menu(self) -> None:
        tk = self.tk
        menu_bar = tk.Menu(self.root)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="选择输入文件", command=self._choose_input_file)
        file_menu.add_command(label="选择输入文件夹", command=self._choose_input_dir)
        file_menu.add_command(label="选择输出目录", command=self._choose_output_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.root.destroy)
        menu_bar.add_cascade(label="文件", menu=file_menu)

        run_menu = tk.Menu(menu_bar, tearoff=False)
        run_menu.add_command(label="开始运行", command=self.start)
        run_menu.add_command(label="清空日志", command=self._clear_log)
        menu_bar.add_cascade(label="运行", menu=run_menu)

        profile_menu = tk.Menu(menu_bar, tearoff=False)
        for label, profile_id in self.profile_by_label.items():
            profile_menu.add_command(label=label, command=lambda value=profile_id: self._set_profile(value))
        menu_bar.add_cascade(label="Profile", menu=profile_menu)

        report_menu = tk.Menu(menu_bar, tearoff=False)
        report_menu.add_command(label="打开输出目录", command=lambda: self._open_path(self.vars["output_dir"].get()))
        report_menu.add_command(label="打开过程报告目录", command=self._open_reports_dir)
        menu_bar.add_cascade(label="报告", menu=report_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="关于 LLMcheck", command=self._show_about)
        menu_bar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menu_bar)

    def _build_layout(self) -> None:
        outer = self.ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = self.tk.Canvas(outer, borderwidth=0, highlightthickness=0, background="#f5f7fb")
        scrollbar = self.ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = self.ttk.Frame(canvas)
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        self._build_task_section(content).grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        self._build_profile_section(content).grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self._build_llm_section(content).grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self._build_advanced_section(content).grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        self._build_batch_section(content).grid(row=3, column=0, sticky="nsew", padx=4, pady=4)
        self._build_run_section(content).grid(row=3, column=1, sticky="nsew", padx=4, pady=4)

    def _section(self, parent: Any, title: str) -> Any:
        frame = self.ttk.LabelFrame(parent, text=title, padding=12)
        frame.columnconfigure(1, weight=1)
        return frame

    def _build_task_section(self, parent: Any) -> Any:
        frame = self._section(parent, "任务")
        self._entry_row(frame, 0, "输入文件/文件夹", "input_path", browse=self._choose_input_file)
        self.ttk.Button(frame, text="选文件夹", command=self._choose_input_dir).grid(row=0, column=3, sticky="ew", padx=(6, 0))
        self._entry_row(frame, 1, "输出目录", "output_dir", browse=self._choose_output_dir)
        return frame

    def _build_profile_section(self, parent: Any) -> Any:
        frame = self._section(parent, "Profile")
        self.ttk.Label(frame, text="文档类型").grid(row=0, column=0, sticky="w", pady=4)
        combo = self.ttk.Combobox(
            frame,
            textvariable=self.vars["profile_label"],
            values=list(self.profile_by_label),
            state="readonly",
        )
        combo.grid(row=0, column=1, sticky="ew", pady=4)
        self.ttk.Label(frame, text="默认是通用标准文档，中医只是可选 profile。").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        return frame

    def _build_llm_section(self, parent: Any) -> Any:
        frame = self._section(parent, "LLM")
        self._entry_row(frame, 0, "API URL", "llm_api_url")
        self._entry_row(frame, 1, "API Key", "llm_api_key", show="*")
        self._entry_row(frame, 2, "Model", "llm_model")
        self.ttk.Label(frame, text="默认只保留主入口参数；分片、返修、MinerU、PPX 等调参放到高级设置。\n").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        return frame

    def _build_advanced_section(self, parent: Any) -> Any:
        frame = self._section(parent, "高级设置")
        for column in range(4):
            frame.columnconfigure(column, weight=1 if column in {1, 3} else 0)
        self._entry_row(frame, 0, "片段并发", "concurrency")
        self._entry_row(frame, 1, "片段字数", "llm_chunk_chars")
        self._entry_row(frame, 2, "返修轮数", "acceptance_repair_rounds")
        self._entry_row(frame, 3, "请求超时秒", "timeout_seconds")
        self._entry_row(frame, 4, "MinerU URL", "mineru_api_url")
        self._entry_row(frame, 5, "MinerU Key", "mineru_api_key", show="*")
        self._entry_row(frame, 6, "MinerU 并发", "mineru_concurrency")
        self._entry_row(frame, 7, "批量文件数", "mineru_batch_size")
        self._entry_row(frame, 8, "总等待秒", "mineru_timeout_seconds")
        self._entry_row(frame, 9, "网络超时秒", "mineru_request_timeout_seconds")
        self._entry_row(frame, 10, "重试次数", "mineru_max_retries")
        self._entry_row(frame, 11, "退避秒数", "mineru_retry_backoff_seconds")
        self._entry_row(frame, 12, "PDF 切片页数", "pdf_page_chunk_size")
        self.ttk.Checkbutton(frame, text="启用本地 PPX（默认关，易卡死机器）", variable=self.vars["enable_ppx"]).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=4
        )
        self._combo_row(frame, 14, "MinerU 失败回退", "mineru_fallback", ("none", "ppx"))
        self._entry_row(frame, 15, "PPX 命令", "ppx_command")
        self._entry_row(frame, 16, "PPX 工作目录", "ppx_cwd")
        self._entry_row(frame, 17, "PPX 超时秒", "ppx_timeout_seconds")
        self._combo_row(frame, 18, "PPX 后端", "ppx_backend", ("default",))
        self._combo_row(frame, 19, "PPX OCR", "ppx_ocr", ("auto", "yes", "no"))
        self._combo_row(frame, 20, "PPX 公式", "ppx_formula", ("no", "auto", "yes"))
        return frame

    def _build_batch_section(self, parent: Any) -> Any:
        frame = self._section(parent, "批处理")
        self._entry_row(frame, 0, "逐本并发", "book_concurrency")
        self._entry_row(frame, 1, "起始书号", "start_index")
        self._entry_row(frame, 2, "最多本数", "limit")
        self.ttk.Checkbutton(frame, text="强制重跑已通过书目", variable=self.vars["force"]).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=6
        )
        return frame

    def _build_run_section(self, parent: Any) -> Any:
        frame = self._section(parent, "运行与报告")
        frame.columnconfigure(0, weight=1)
        self.ttk.Button(frame, text="开始转换 / 清洗 / 收口", style="Primary.TButton", command=self.start).grid(row=0, column=0, sticky="ew")
        self.ttk.Progressbar(frame, variable=self.progress_var, maximum=100).grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.ttk.Label(frame, textvariable=self.status_var).grid(row=2, column=0, sticky="w")
        self.log_text = self.tk.Text(frame, height=14, wrap="word", borderwidth=1, relief="solid")
        self.log_text.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(3, weight=1)
        return frame

    def _entry_row(self, parent: Any, row: int, label: str, key: str, *, browse: Callable[[], None] | None = None, show: str | None = None) -> None:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        entry_kwargs = {"textvariable": self.vars[key]}
        if show is not None:
            entry_kwargs["show"] = show
        self.ttk.Entry(parent, **entry_kwargs).grid(row=row, column=1, sticky="ew", pady=4)
        if browse is not None:
            self.ttk.Button(parent, text="浏览", command=browse).grid(row=row, column=2, sticky="ew", padx=(6, 0), pady=4)

    def _combo_row(self, parent: Any, row: int, label: str, key: str, values: tuple[str, ...]) -> None:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.ttk.Combobox(parent, textvariable=self.vars[key], values=values, state="readonly").grid(row=row, column=1, sticky="ew", pady=4)

    def start(self) -> None:
        if self.running:
            self.messagebox.showinfo("LLMcheck", "当前已有任务在运行。")
            return
        try:
            values = self._form_values()
            settings = build_settings_from_form(values)
            self._validate_required(values, settings)
        except Exception as error:
            self.messagebox.showerror("参数错误", str(error))
            return
        self.running = True
        self.progress_var.set(5)
        self.status_var.set("运行中")
        self._append_log("任务已启动。")
        thread = threading.Thread(target=self._run_job, args=(values, settings), daemon=True)
        thread.start()

    def _run_job(self, values: DesktopFormValues, settings: LlmCheckSettings) -> None:
        try:
            if should_run_batch(values.input_path):
                report = run_batch(
                    source_dir=values.input_path.expanduser(),
                    output_dir=values.output_dir.expanduser(),
                    settings=settings,
                    book_concurrency=max(1, values.book_concurrency),
                    start_index=max(1, values.start_index),
                    limit=max(0, values.limit),
                    force=values.force,
                    progress_callback=lambda event: self.log_queue.put(("progress", event)),
                )
            else:
                report = process_documents(input_path=values.input_path.expanduser(), output_dir=values.output_dir.expanduser(), settings=settings)
            self.log_queue.put(("done", report))
        except Exception as error:
            self.log_queue.put(("error", str(error)))

    def _poll_log_queue(self) -> None:
        while True:
            try:
                kind, payload = self.log_queue.get_nowait()
            except Empty:
                break
            if kind == "progress":
                self._handle_progress(payload)
            elif kind == "done":
                self.running = False
                self.progress_var.set(100)
                self.status_var.set(f"完成：{payload.get('status', 'unknown')}")
                self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))
            elif kind == "error":
                self.running = False
                self.progress_var.set(100)
                self.status_var.set("失败")
                self._append_log(f"ERROR: {payload}")
        self.root.after(250, self._poll_log_queue)

    def _handle_progress(self, event: dict[str, Any]) -> None:
        name = str(event.get("book_name") or Path(str(event.get("source_path") or "")).name)
        if event.get("event") == "batch_started":
            self.progress_var.set(10)
            self.status_var.set(f"批处理启动：{event.get('selected_total', 0)} 本")
        elif event.get("event") == "book_started":
            self.status_var.set(f"处理中：{event.get('index', '?')}/{event.get('total', '?')} {name}")
        elif event.get("event") == "book_finished":
            completed = int(event.get("index") or 0)
            total = max(1, int(event.get("total") or completed or 1))
            self.progress_var.set(min(95, 10 + 85 * completed / total))
            self.status_var.set(f"已完成：{name} -> {event.get('status', 'unknown')}")
        self._append_log(json.dumps(event, ensure_ascii=False))

    def _form_values(self) -> DesktopFormValues:
        get = lambda key: str(self.vars[key].get()).strip()
        input_raw = get("input_path")
        output_raw = get("output_dir")
        if not input_raw:
            raise ValueError("请选择输入文件或文件夹。")
        if not output_raw:
            raise ValueError("请选择输出目录。")
        profile_label = get("profile_label")
        profile_id = self.profile_by_label.get(profile_label)
        if profile_id is None and profile_label in self.label_by_profile:
            profile_id = profile_label
        return DesktopFormValues(
            input_path=Path(input_raw).expanduser(),
            output_dir=Path(output_raw).expanduser(),
            profile_id=profile_id or DEFAULT_PROFILE_ID,
            llm_api_url=get("llm_api_url"),
            llm_api_key=get("llm_api_key"),
            llm_model=get("llm_model"),
            concurrency=self._int_value("concurrency"),
            llm_chunk_chars=self._int_value("llm_chunk_chars"),
            acceptance_repair_rounds=self._int_value("acceptance_repair_rounds"),
            timeout_seconds=self._int_value("timeout_seconds"),
            mineru_api_url=get("mineru_api_url"),
            mineru_api_key=get("mineru_api_key"),
            mineru_concurrency=self._int_value("mineru_concurrency"),
            mineru_batch_size=self._int_value("mineru_batch_size"),
            mineru_timeout_seconds=self._int_value("mineru_timeout_seconds"),
            mineru_request_timeout_seconds=self._int_value("mineru_request_timeout_seconds"),
            mineru_max_retries=self._int_value("mineru_max_retries"),
            mineru_retry_backoff_seconds=self._float_value("mineru_retry_backoff_seconds"),
            pdf_page_chunk_size=self._int_value("pdf_page_chunk_size"),
            enable_ppx=bool(self.vars["enable_ppx"].get()),
            mineru_fallback=get("mineru_fallback") or "none",
            ppx_command=get("ppx_command"),
            ppx_cwd=get("ppx_cwd"),
            ppx_timeout_seconds=self._int_value("ppx_timeout_seconds"),
            ppx_backend=get("ppx_backend"),
            ppx_ocr=get("ppx_ocr"),
            ppx_formula=get("ppx_formula"),
            book_concurrency=self._int_value("book_concurrency"),
            start_index=self._int_value("start_index"),
            limit=self._int_value("limit"),
            force=bool(self.vars["force"].get()),
        )

    def _validate_required(self, values: DesktopFormValues, settings: LlmCheckSettings) -> None:
        if not values.input_path.exists():
            raise ValueError(f"输入路径不存在：{values.input_path}")
        if not settings.llm_api_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("请填写 LLM API URL、Key 和 Model。")

    def _int_value(self, key: str) -> int:
        try:
            return int(str(self.vars[key].get()).strip())
        except ValueError as error:
            raise ValueError(f"{key} 必须是整数。") from error

    def _float_value(self, key: str) -> float:
        try:
            return float(str(self.vars[key].get()).strip())
        except ValueError as error:
            raise ValueError(f"{key} 必须是数字。") from error

    def _choose_input_file(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title="选择输入文件")
        if path:
            self.vars["input_path"].set(path)

    def _choose_input_dir(self) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(title="选择输入文件夹")
        if path:
            self.vars["input_path"].set(path)

    def _choose_output_dir(self) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.vars["output_dir"].set(path)

    def _set_profile(self, profile_id: str) -> None:
        self.vars["profile_label"].set(self.label_by_profile.get(profile_id, profile_id))

    def _open_reports_dir(self) -> None:
        output_dir = Path(str(self.vars["output_dir"].get())).expanduser()
        self._open_path(str(output_dir / "process" / "reports"))

    def _open_path(self, value: str) -> None:
        path = Path(value).expanduser()
        if not path.exists():
            self.messagebox.showwarning("路径不存在", str(path))
            return
        import os
        import subprocess
        import sys

        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _show_about(self) -> None:
        self.messagebox.showinfo(
            "关于 LLMcheck",
            "LLMcheck 桌面版\n\nProfile 驱动的文档转换、清洗、验收、收口和标准文档交付工具。",
        )

    def _append_log(self, text: str) -> None:
        if self.log_text is None:
            return
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        if self.log_text is not None:
            self.log_text.delete("1.0", "end")
