"""Read-only access to the team's ABAP source files.

Claude uses these as tools to answer questions about our own code
("what does the bid submission do when logintype is S?", "why does ATC
flag line 617?"). Access is restricted to files under one directory.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE_SUFFIXES = {".abap", ".txt", ".cds", ".ddls", ".asddls", ".xml", ".json", ".md"}
MAX_FILE_CHARS = 120_000
MAX_SEARCH_HITS = 40


class AbapSourceLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            p
            for p in self.root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in SOURCE_SUFFIXES
            and not any(part.startswith(".") for part in p.relative_to(self.root).parts)
        )

    def _resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError(f"No source file named {relative_path!r}.")
        return path

    def list_files(self) -> str:
        files = self._files()
        if not files:
            return "No ABAP source files are available."
        return "\n".join(
            f"{p.relative_to(self.root).as_posix()} ({sum(1 for _ in p.open(errors='replace'))} lines)"
            for p in files
        )

    def search(self, pattern: str) -> str:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"Invalid regular expression: {exc}"

        hits = []
        for path in self._files():
            rel = path.relative_to(self.root).as_posix()
            with path.open(errors="replace") as f:
                for line_no, line in enumerate(f, start=1):
                    if regex.search(line):
                        hits.append(f"{rel}:{line_no}: {line.rstrip()}")
                        if len(hits) >= MAX_SEARCH_HITS:
                            hits.append(f"(stopped after {MAX_SEARCH_HITS} matches; narrow the pattern)")
                            return "\n".join(hits)
        return "\n".join(hits) if hits else f"No matches for {pattern!r}."

    def read(self, relative_path: str, start_line: int = 1, end_line: int | None = None) -> str:
        try:
            path = self._resolve(relative_path)
        except FileNotFoundError as exc:
            return str(exc)

        lines = path.read_text(errors="replace").splitlines()
        start = max(start_line, 1)
        end = min(end_line or len(lines), len(lines))
        numbered = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        if len(numbered) > MAX_FILE_CHARS:
            numbered = numbered[:MAX_FILE_CHARS] + "\n(truncated; request a smaller line range)"
        return f"{relative_path} lines {start}-{end} of {len(lines)}\n{numbered}"
