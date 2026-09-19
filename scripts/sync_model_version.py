#!/usr/bin/env python3
"""
sync_model_version.py

Detects a change to a `model="..."` (or model='...') assignment introduced
by the current push, and propagates that exact change to every other .py
file in the repo that still has the OLD value.

Design goals:
- Safe by default: only touches files whose `model=` value exactly matches
  the OLD value found in the diff. A file that already uses a different,
  intentionally-overridden model (e.g. a_single_agent/day_trip.py using
  "gemini-3.8-flash") is left untouched.
- Ambiguity-aware: if the push contains conflicting changes to the same
  OLD value (mapped to two different NEW values), the script aborts with
  a clear error instead of guessing.
- Idempotent / no-op safe: if nothing changed, or every file is already in
  sync, it exits 0 without creating an empty commit.

Exit codes:
  0 -> success (with or without changes; CHANGED file at $GITHUB_OUTPUT
       tells the workflow whether to commit)
  1 -> ambiguous or unparseable diff, needs human attention
"""

import os
import re
import subprocess
import sys
from pathlib import Path

# Matches: model="gemini-flash-latest"  or  model='gemini-flash-latest'
# (?<!\w) / (?!\w) avoid matching "model_name=", "base_model=", etc.
MODEL_ASSIGN_RE = re.compile(
    r"(?<!\w)model(?!\w)\s*=\s*(['\"])([^'\"]+)\1"
)

# Directories we never want to touch even if a .py file lives there.
EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def get_diff(before: str, after: str) -> str:
    # --unified=0 keeps hunks tight so we can pair up removed/added lines.
    return run(
        [
            "git",
            "diff",
            "--unified=0",
            "--no-color",
            before,
            after,
            "--",
            "*.py",
        ]
    )


def extract_model_changes(diff_text: str) -> dict[str, str]:
    """
    Walk the unified diff hunk by hunk. Within each hunk, pair up removed
    `model="X"` lines with added `model="Y"` lines (in order) to build a
    mapping old_value -> new_value. Returns the mapping, raising on
    conflicting mappings for the same old_value.
    """
    changes: dict[str, str] = {}
    current_removed: list[str] = []
    current_added: list[str] = []

    def flush_hunk():
        # Pair removed/added model values positionally within the hunk.
        for old_val, new_val in zip(current_removed, current_added):
            if old_val == new_val:
                continue
            if old_val in changes and changes[old_val] != new_val:
                print(
                    f"::error::Ambiguous model version change detected: "
                    f"'{old_val}' -> both '{changes[old_val]}' and '{new_val}'. "
                    f"Refusing to guess — please push a single, unambiguous "
                    f"model version change at a time.",
                    file=sys.stderr,
                )
                sys.exit(1)
            changes[old_val] = new_val
        current_removed.clear()
        current_added.clear()

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            flush_hunk()
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("-"):
            content = line[1:]
            if content.lstrip().startswith("#"):
                continue
            m = MODEL_ASSIGN_RE.search(content)
            if m:
                current_removed.append(m.group(2))
        elif line.startswith("+"):
            content = line[1:]
            if content.lstrip().startswith("#"):
                continue
            m = MODEL_ASSIGN_RE.search(content)
            if m:
                current_added.append(m.group(2))

    flush_hunk()
    return changes


def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        yield path


def apply_changes(
    root: Path, changes: dict[str, str], dry_run: bool = False
) -> tuple[list[str], list[dict]]:
    """
    Returns (touched_files, line_details). line_details is a list of dicts
    with keys: file, line_no, before, after — one entry per changed line,
    used to build the before/after report shown for confirmation.
    """
    touched: list[str] = []
    details: list[dict] = []

    for path in iter_python_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        file_changed = False
        out_lines = []

        for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                out_lines.append(line)
                continue

            def replacer(m: re.Match, _line_no=line_no) -> str:
                nonlocal file_changed
                quote, value = m.group(1), m.group(2)
                if value in changes:
                    file_changed = True
                    new_line_fragment = f"model={quote}{changes[value]}{quote}"
                    details.append(
                        {
                            "file": str(path.relative_to(root)),
                            "line_no": _line_no,
                            "before": line.rstrip("\n"),
                            "after": None,  # filled in after substitution below
                            "_orig_match": m.group(0),
                            "_new_match": new_line_fragment,
                        }
                    )
                    return new_line_fragment
                return m.group(0)

            new_line = MODEL_ASSIGN_RE.sub(replacer, line)
            out_lines.append(new_line)

        new_text = "".join(out_lines)

        if file_changed and new_text != text:
            if not dry_run:
                path.write_text(new_text, encoding="utf-8")
            touched.append(str(path.relative_to(root)))

    # Backfill the "after" (full new line text) now that substitution is done.
    for d in details:
        d["after"] = d["before"].replace(d.pop("_orig_match"), d.pop("_new_match"))

    return touched, details


def build_report(changes: dict[str, str], details: list[dict]) -> str:
    lines = ["## Model version sync — before / after preview", ""]
    lines.append("**Detected change(s):**")
    for old, new in changes.items():
        lines.append(f"- `{old}` &rarr; `{new}`")
    lines.append("")
    lines.append("**Files that will be updated:**")
    lines.append("")
    lines.append("| File | Line | Before | After |")
    lines.append("|---|---|---|---|")
    for d in sorted(details, key=lambda x: (x["file"], x["line_no"])):
        before_md = d["before"].strip().replace("|", "\\|")
        after_md = d["after"].strip().replace("|", "\\|")
        lines.append(f"| `{d['file']}` | {d['line_no']} | `{before_md}` | `{after_md}` |")
    lines.append("")
    lines.append(
        "_Review the diff below (or in the PR's \"Files changed\" tab) and "
        "merge to confirm, or close the PR to discard._"
    )
    return "\n".join(lines)


def main() -> int:
    before = os.environ.get("BEFORE_SHA", "")
    after = os.environ.get("AFTER_SHA", "HEAD")
    root = Path(os.environ.get("REPO_ROOT", ".")).resolve()
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    report_path = os.environ.get("REPORT_PATH", "")

    zero_sha = "0" * 40
    if not before or before == zero_sha:
        print(
            "No usable 'before' SHA (new branch or force-push) — "
            "skipping automatic sync for this push."
        )
        _write_output("changed", "false")
        return 0

    diff_text = get_diff(before, after)
    if not diff_text.strip():
        print("No .py changes in this push — nothing to do.")
        _write_output("changed", "false")
        return 0

    changes = extract_model_changes(diff_text)
    if not changes:
        print("No `model=\"...\"` value changes detected in this push.")
        _write_output("changed", "false")
        return 0

    print("Detected model version change(s):")
    for old, new in changes.items():
        print(f"  {old!r} -> {new!r}")

    touched, details = apply_changes(root, changes, dry_run=dry_run)

    if not touched:
        print("All files already in sync — nothing to do.")
        _write_output("changed", "false")
        return 0

    print(f"{'Would update' if dry_run else 'Updated'} {len(touched)} file(s):")
    for f in touched:
        print(f"  - {f}")

    report = build_report(changes, details)
    print("\n" + report)

    if report_path:
        Path(report_path).write_text(report, encoding="utf-8")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")

    _write_output("changed", "true")
    # Expose a human-readable summary for the commit/PR title.
    summary = "; ".join(f"{old} -> {new}" for old, new in changes.items())
    _write_output("summary", summary)
    return 0


def _write_output(key: str, value: str) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
