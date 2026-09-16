"""Minimal native desktop UI for docket — Tkinter, no browser, no extra deps
beyond the standard library.

    python gui.py
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext

from docket.pipeline import process
from docket.schemas import PipelineResult


class DocketApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.selected_path: Path | None = None

        root.title("docket")
        root.geometry("720x600")

        top = tk.Frame(root)
        top.pack(fill="x", padx=10, pady=10)

        self.path_var = tk.StringVar(value="No file selected")
        tk.Button(top, text="Choose document...", command=self.choose_file).pack(side="left")
        tk.Label(top, textvariable=self.path_var, anchor="w").pack(
            side="left", padx=10, fill="x", expand=True
        )
        self.run_button = tk.Button(top, text="Run", command=self.run, state="disabled")
        self.run_button.pack(side="right")

        self.status_var = tk.StringVar(value="Pick a document to begin.")
        tk.Label(root, textvariable=self.status_var, anchor="w", fg="gray").pack(fill="x", padx=10)

        self.output = scrolledtext.ScrolledText(root, wrap="word", font=("Menlo", 12))
        self.output.pack(fill="both", expand=True, padx=10, pady=10)
        self.output.tag_config("h1", font=("Menlo", 13, "bold"))
        self.output.tag_config("ok", foreground="#1a7f37")
        self.output.tag_config("err", foreground="#cf222e")
        self.output.tag_config("warn", foreground="#9a6700")
        self.output.config(state="disabled")

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a document",
            filetypes=[("Documents", "*.pdf *.png *.jpg *.jpeg *.txt"), ("All files", "*.*")],
        )
        if path:
            self.selected_path = Path(path)
            self.path_var.set(self.selected_path.name)
            self.run_button.config(state="normal")

    def run(self) -> None:
        if self.selected_path is None:
            return
        self.run_button.config(state="disabled")
        self.status_var.set("Running OCR/VLM → classify → extract → validate...")
        self._write("", clear=True)
        threading.Thread(target=self._run_pipeline, daemon=True).start()

    def _run_pipeline(self) -> None:
        assert self.selected_path is not None
        try:
            result = process(self.selected_path)
        except Exception as exc:  # noqa: BLE001 — surface any failure in the UI, not a traceback
            self.root.after(0, self._show_error, str(exc))
            return
        self.root.after(0, self._show_result, result)

    def _show_error(self, message: str) -> None:
        self._write(f"Pipeline failed: {message}\n", tag="err", clear=True)
        self.status_var.set("Failed.")
        self.run_button.config(state="normal")

    def _show_result(self, result: PipelineResult) -> None:
        self._write("", clear=True)
        self._write("Summary\n", tag="h1")
        c = result.classification
        self._write(f"  Doc type:         {c.doc_type.value}\n")
        self._write(f"  Classified via:   {c.method} ({c.confidence:.0%} confidence)\n")
        self._write(f"  OCR method:       {result.ocr_method}\n")
        self._write(f"  Extract attempts: {result.extract_attempts}\n\n")

        self._write("Extracted fields\n", tag="h1")
        if result.extracted is not None:
            self._write(json.dumps(result.extracted, indent=2, ensure_ascii=False) + "\n\n")
        else:
            self._write("  (extraction failed)\n\n", tag="err")

        self._write("Validation\n", tag="h1")
        if not result.validation_issues:
            self._write("  All business rules passed.\n", tag="ok")
        else:
            for issue in result.validation_issues:
                tag = "err" if issue.severity == "error" else "warn"
                self._write(f"  [{issue.severity}] {issue.field}: {issue.message}\n", tag=tag)

        if result.needs_review:
            self._write("\nReview\n", tag="h1")
            for reason in result.review_reasons:
                self._write(f"  • {reason}\n", tag="warn")

        self.status_var.set(f"Done — {result.raw_text_chars} chars of text acquired.")
        self.run_button.config(state="normal")

    def _write(self, text: str, tag: str | None = None, clear: bool = False) -> None:
        self.output.config(state="normal")
        if clear:
            self.output.delete("1.0", "end")
        if text:
            if tag is not None:
                self.output.insert("end", text, tag)
            else:
                self.output.insert("end", text)
        self.output.config(state="disabled")
        self.output.see("end")


def main() -> None:
    root = tk.Tk()
    DocketApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
