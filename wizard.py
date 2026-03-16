#!/usr/bin/env python3
# /wizard.py

"""
Google Forms Config Wizard + Crawler (Playwright, sync)

Wizard V2 upgrades:
- Richer extraction coverage (dropdown, checkbox, paragraph, date/time, grids, file upload).
- Adds semantic_key inference and default generation specs per field.
- Records branching transitions: option_text -> next section_signature (bounded exploration).

Backward compatibility:
- Existing runner configs that use legacy field keys/labels/types/generation should still work.
- Wizard emits extra V2 fields (field_key, label, help_text, semantic_key, grid, transitions, allow_other, unsupported)
  while still emitting legacy aliases (key, label_text, semantic_type).

Safety / boundaries:
- No CAPTCHA bypass, stealth/fingerprint spoofing, or evasion tactics.
- Crawl mode never clicks Submit.

Usage:
  python wizard.py build --url "<FORM_URL>" --out "configs/form1.json" --headless false --timeout 30000 --crawl true
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# -----------------------------
# Models
# -----------------------------

@dataclass
class GridExtract:
    rows: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    kind: str = ""  # "mc"|"checkbox"
    required_per_row: bool = False
    required_mode: str = "unknown"  # "none"|"per_row"|"at_least_one"|"unknown"
    row_required: Optional[Dict[str, bool]] = None


@dataclass
class QuestionExtract:
    index: int
    label_text: str
    help_text: str
    required: bool
    type_guess: str
    options: List[str] = field(default_factory=list)
    allow_other: bool = False
    grid: Optional[GridExtract] = None
    transitions: Dict[str, str] = field(default_factory=dict)
    unsupported: bool = False
    error: Optional[str] = None
    input_attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class BlockInfo:
    block: Any  # Locator for div[role=listitem]
    label_text: str
    help_text: str
    kind: str  # text|paragraph|radio|dropdown|checkbox|date|time|mc_grid|checkbox_grid|file_upload|unknown
    required: bool
    error_text: str
    options: List[str] = field(default_factory=list)  # radio/checkbox (dropdown extracted lazily)
    allow_other: bool = False
    grid: Optional[GridExtract] = None
    input_attrs: Dict[str, str] = field(default_factory=dict)


FIELD_TYPES = [
    "text",
    "paragraph",
    "textarea",  # legacy alias for paragraph
    "radio",
    "checkbox",
    "dropdown",
    "scale",
    "date",
    "time",
    "mc_grid",
    "checkbox_grid",
    "file_upload",
    "unknown",
]


# -----------------------------
# CLI helpers
# -----------------------------


def str_to_bool(v: str) -> bool:
    s = v.strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean: {v!r}. Use true/false.")


def preview(text: str, n: int = 70) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else (t[: n - 1] + "…")


def prompt_text(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default


def prompt_int(prompt: str, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            v = default
        else:
            try:
                v = int(raw)
            except ValueError:
                print("  Enter a whole number.")
                continue
        if min_value is not None and v < min_value:
            print(f"  Must be >= {min_value}.")
            continue
        if max_value is not None and v > max_value:
            print(f"  Must be <= {max_value}.")
            continue
        return v


def prompt_float(prompt: str, default: float, min_value: Optional[float] = None) -> float:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            v = default
        else:
            try:
                v = float(raw)
            except ValueError:
                print("  Enter a number.")
                continue
        if min_value is not None and v < min_value:
            print(f"  Must be >= {min_value}.")
            continue
        return v


def prompt_choice(prompt: str, choices: Sequence[Tuple[str, str]], default_key: Optional[str] = None) -> str:
    key_set = {k for k, _ in choices}
    while True:
        print(prompt)
        for k, label in choices:
            d = " (default)" if default_key is not None and k == default_key else ""
            print(f"  {k}) {label}{d}")
        raw = input("Select: ").strip()
        if not raw and default_key is not None:
            return default_key
        if raw in key_set:
            return raw
        print("  Invalid selection.")


def normalize_button_label(text: str) -> str:
    return " ".join((text or "").split()).strip()


def normalize_label_to_key(label: str) -> str:
    s = (label or "").strip().lower()
    s = re.sub(r"[*]+$", "", s).strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def normalize_question_identity(label: str) -> str:
    base = normalize_label_to_key(label)
    return base or "question"


def ensure_unique_key(base: str, used: set[str]) -> str:
    if base and base not in used:
        used.add(base)
        return base
    if not base:
        base = "field"
    i = 2
    while True:
        cand = f"{base}_{i}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


# -----------------------------
# Generator primitives (repairs)
# -----------------------------


class Gen:
    @staticmethod
    def text() -> str:
        return "Test"

    @staticmethod
    def name() -> str:
        return "Test User"

    @staticmethod
    def numeric(min_v: Optional[int] = None, max_v: Optional[int] = None) -> str:
        if min_v is None and max_v is None:
            return "42"
        if min_v is None:
            min_v = 0
        if max_v is None:
            max_v = min_v + 100
        if max_v < min_v:
            max_v = min_v
        return str(random.randint(min_v, max_v))

    @staticmethod
    def email() -> str:
        return "test.user@example.com"

    @staticmethod
    def phone() -> str:
        return "5551234567"

    @staticmethod
    def pin(length: int = 6) -> str:
        return "".join(random.choice("0123456789") for _ in range(max(1, length)))

    @staticmethod
    def date_mmddyyyy() -> str:
        return "01/15/2026"

    @staticmethod
    def time_hhmm() -> str:
        return "10:30"

    @staticmethod
    def exact(literal: str) -> str:
        return literal


# -----------------------------
# ConstraintSolver (generic, visible-only, robust nav)
# -----------------------------


class ConstraintSolver:
    """
    Generic, visible-only solver for Google Forms navigation + validation repair.

    Navigation robustness:
      - section_signature() is terminal-aware:
          if Submit visible and Next NOT visible -> "__TERMINAL_SUBMIT__" even if 0 questions.
      - Never uses the form title alone as a signature.
      - Prefers fingerprint from top 3 visible question labels: "Q:label1||label2||label3"
      - Fallback when Submit is present: "NAV:" + nav button labels
      - Polling-based waits (no long single waits).

    Two-phase fill:
      a) fill_defaults_for_required_visible_questions() runs before clicking Next
      b) repair_from_validation_errors() runs only when nav didn't change and visible errors exist
    """

    def __init__(
        self,
        page,
        timeout_ms: int = 30000,
        diagnostics_dir: Path = Path("solver_diagnostics"),
        learning_store: Optional[Dict[str, Any]] = None,
        signature_repeat_max: int = 10,
        never_submit: bool = False,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.diagnostics_dir = diagnostics_dir
        self.learning_store = learning_store if learning_store is not None else {}
        self.signature_repeat_max = signature_repeat_max
        self.never_submit = never_submit
        self._sig_counts: Dict[str, int] = {}

    # ---------- DOM basics ----------

    def form_container(self) -> Any:
        c = self.page.locator('div[role="main"]').first
        try:
            if c.count() > 0 and c.is_visible():
                return c
        except Exception:
            pass
        f = self.page.locator("form").first
        try:
            if f.count() > 0 and f.is_visible():
                return f
        except Exception:
            pass
        return self.page.locator("body").first

    def _safe_inner_text(self, loc, timeout_ms: int) -> str:
        try:
            return normalize_button_label(loc.inner_text(timeout=timeout_ms) or "")
        except Exception:
            return ""

    def _safe_input_value(self, loc, timeout_ms: int) -> str:
        try:
            return (loc.input_value(timeout=timeout_ms) or "").strip()
        except Exception:
            return ""

    def _safe_attr(self, loc, name: str) -> str:
        try:
            return (loc.get_attribute(name) or "").strip()
        except Exception:
            return ""

    def log_empty_signature_debug(self) -> None:
        try:
            blocks = self.page.locator("div[role='listitem']")
            total = blocks.count()
        except Exception:
            total = -1

        visible = 0
        try:
            if total >= 0:
                blocks = self.page.locator("div[role='listitem']")
                for i in range(total):
                    b = blocks.nth(i)
                    try:
                        if b.is_visible():
                            visible += 1
                    except Exception:
                        continue
        except Exception:
            visible = -1

        preview_text = ""
        try:
            preview_text = (self.form_container().inner_text(timeout=min(self.timeout_ms, 1500)) or "")[:200]
        except Exception:
            preview_text = ""

        print(f"DEBUG: empty signature; listitems total={total} visible={visible}")
        if preview_text:
            print(f"DEBUG: form container text[0:200]={preview_text!r}")

    # ---------- Visible-only block extraction ----------

    def _extract_label(self, block, timeout_ms: int) -> str:
        candidates = [
            block.locator("[role='heading'][aria-level='3']").first,
            block.locator("[role='heading'][aria-level='2']").first,
            block.locator("[role='heading']").first,
            block.locator("div[dir='auto']").first,
        ]
        for c in candidates:
            txt = self._safe_inner_text(c, timeout_ms)
            if txt:
                return re.sub(r"\*\s*$", "", txt).strip()
        return ""

    def _detect_required(self, block, label_text: str) -> bool:
        try:
            req = block.locator("[aria-label*='Required'], [aria-label*='required']")
            if req.count() > 0:
                return True
        except Exception:
            pass
        return bool(re.search(r"\*\s*$", (label_text or "").strip()))

    def _extract_error_text(self, block, timeout_ms: int) -> str:
        loc = block.locator('[role="alert"], [aria-live="assertive"], [aria-live="polite"]')
        best = ""
        best_score = -1
        try:
            n = loc.count()
        except Exception:
            return ""
        for i in range(n):
            it = loc.nth(i)
            try:
                if not it.is_visible():
                    continue
            except Exception:
                continue
            txt = self._safe_inner_text(it, timeout_ms)
            if not txt:
                continue
            score = 0
            low = txt.lower()
            for k in ("required", "must", "valid", "match", "pattern", "format", "email", "number", "exactly", "row"):
                if k in low:
                    score += 2
            if score > best_score:
                best_score = score
                best = txt
        return best

    def _extract_radio_options(self, block, timeout_ms: int) -> Tuple[List[str], bool]:
        out: List[str] = []
        allow_other = False

        def radio_label(loc) -> str:
            txt = normalize_button_label(self._safe_attr(loc, "aria-label"))
            if txt:
                return txt
            txt = normalize_button_label(self._safe_inner_text(loc, timeout_ms))
            if txt:
                return txt
            ids = self._safe_attr(loc, "aria-labelledby")
            if ids:
                parts: List[str] = []
                for _id in ids.split():
                    try:
                        part = normalize_button_label(self.page.locator(f"#{_id}").first.inner_text(timeout=timeout_ms) or "")
                    except Exception:
                        part = ""
                    if part:
                        parts.append(part)
                if parts:
                    return normalize_button_label(" ".join(parts))
            try:
                parts = [normalize_button_label(x) for x in loc.locator("div[dir='auto']").all_text_contents()]
            except Exception:
                parts = []
            parts = [p for p in parts if p]
            return max(parts, key=len) if parts else ""

        try:
            radios = block.locator("[role='radio']")
            n = radios.count()
            for i in range(n):
                r = radios.nth(i)
                try:
                    if not r.is_visible():
                        continue
                except Exception:
                    continue
                label = normalize_button_label(radio_label(r))
                low = label.lower()
                if not label:
                    continue
                if low in {"other", "other:"} or low.startswith("other"):
                    allow_other = True
                    continue
                if label not in out:
                    out.append(label)
        except Exception:
            pass
        return out, allow_other

    def _extract_checkbox_options(self, block, timeout_ms: int) -> Tuple[List[str], bool]:
        out: List[str] = []
        allow_other = False

        def cb_label(loc) -> str:
            txt = normalize_button_label(self._safe_attr(loc, "aria-label"))
            if txt:
                return txt
            txt = normalize_button_label(self._safe_inner_text(loc, timeout_ms))
            if txt:
                return txt
            ids = self._safe_attr(loc, "aria-labelledby")
            if ids:
                parts: List[str] = []
                for _id in ids.split():
                    try:
                        part = normalize_button_label(self.page.locator(f"#{_id}").first.inner_text(timeout=timeout_ms) or "")
                    except Exception:
                        part = ""
                    if part:
                        parts.append(part)
                if parts:
                    return normalize_button_label(" ".join(parts))
            try:
                parts = [normalize_button_label(x) for x in loc.locator("div[dir='auto']").all_text_contents()]
            except Exception:
                parts = []
            parts = [p for p in parts if p]
            return max(parts, key=len) if parts else ""

        try:
            cbs = block.locator("[role='checkbox']")
            n = cbs.count()
            for i in range(n):
                c = cbs.nth(i)
                try:
                    if not c.is_visible():
                        continue
                except Exception:
                    continue
                label = normalize_button_label(cb_label(c))
                low = label.lower()
                if not label:
                    continue
                if low in {"other", "other:"} or low.startswith("other"):
                    allow_other = True
                    continue
                if label not in out:
                    out.append(label)
        except Exception:
            pass
        return out, allow_other

    def _extract_help_text(self, block, label_text: str, option_texts: Sequence[str], timeout_ms: int) -> str:
        """
        Best-effort help/description text extraction, excluding label + options.
        """
        label_low = normalize_button_label(label_text).lower()
        opt_low = {normalize_button_label(o).lower() for o in (option_texts or []) if normalize_button_label(o)}
        candidates: List[str] = []
        try:
            parts = [normalize_button_label(t) for t in block.locator("div[dir='auto']").all_text_contents()]
        except Exception:
            parts = []
        for t in parts:
            low = t.lower()
            if not t:
                continue
            if low == label_low:
                continue
            if low in opt_low:
                continue
            if low in {"required", "optional"}:
                continue
            if len(t) <= 2:
                continue
            candidates.append(t)
        for t in candidates:
            if 5 <= len(t) <= 160:
                return t
        return max(candidates, key=len) if candidates else ""

    def _extract_grid_info(self, block, timeout_ms: int, required: bool, error_text: str) -> Optional[GridExtract]:
        container = None
        # Check for explicit grid or table
        explicit = block.locator("[role='grid'], [role='table']").first
        try:
            if explicit.count() > 0 and explicit.is_visible():
                container = explicit
        except Exception:
            pass

        # If not, check for presence of row and column headers directly in block
        if container is None:
            try:
                if block.locator("[role='rowheader']").count() > 0 and block.locator("[role='columnheader']").count() > 0:
                    container = block
            except Exception:
                pass

        fallback_triggered = False
        if container is None:
            # Fallback: reconstruct from aria-label patterns
            cells = block.locator("[role='radio'], [role='checkbox']")
            try:
                n_cells = cells.count()
            except Exception:
                n_cells = 0
            if n_cells < 6:
                return None

            row_parts = []
            col_parts = []
            separators = [",", " - ", ":"]
            for i in range(n_cells):
                cell = cells.nth(i)
                try:
                    if not cell.is_visible():
                        continue
                except Exception:
                    continue
                aria_label = self._safe_attr(cell, "aria-label")
                if not aria_label:
                    continue
                for sep in separators:
                    parts = aria_label.split(sep, 1)
                    if len(parts) == 2:
                        a = parts[0].strip()
                        b = parts[1].strip()
                        if b.lower().startswith("response for "):
                            row = b[len("response for "):].strip()
                            col = a.strip()
                        else:
                            row = a.strip()
                            col = b.strip()
                        if row and col:
                            row_parts.append(row)
                            col_parts.append(col)
                            break

            # Unique in order of appearance
            seen_rows = set()
            rows = []
            for r in row_parts:
                if r not in seen_rows:
                    seen_rows.add(r)
                    rows.append(r)

            seen_cols = set()
            cols = []
            for c in col_parts:
                if c not in seen_cols:
                    seen_cols.add(c)
                    cols.append(c)

            if len(rows) < 2 or len(cols) < 2:
                return None

            kind = ""
            try:
                if block.locator("[role='radio']").count() > 0:
                    kind = "mc"
                elif block.locator("[role='checkbox']").count() > 0:
                    kind = "checkbox"
            except Exception:
                kind = ""

            if not kind:
                return None

            fallback_triggered = True
            print(f"GRID_DETECTED_ARIA: kind={kind} rows={len(rows)} cols={len(cols)}")

        else:
            def uniq_texts(loc) -> List[str]:
                out: List[str] = []
                try:
                    n = loc.count()
                except Exception:
                    n = 0
                for i in range(n):
                    it = loc.nth(i)
                    try:
                        if not it.is_visible():
                            continue
                    except Exception:
                        continue
                    txt = self._safe_inner_text(it, timeout_ms=min(timeout_ms, 800))
                    txt = normalize_button_label(txt)
                    if txt and txt not in out:
                        out.append(txt)
                return out

            rows = uniq_texts(container.locator("[role='rowheader']"))
            cols = uniq_texts(container.locator("[role='columnheader']"))
            cols = [c for c in cols if c and c.lower() not in {"", " "}]
            rows = [r for r in rows if r and r.lower() not in {"", " "}]

            if len(rows) < 1 or len(cols) < 1:
                # Alternative detection for table-like layout without explicit headers
                row_locs = container.locator("[role='row']")
                try:
                    num_rows = row_locs.count()
                except Exception:
                    num_rows = 0
                if num_rows < 2:
                    return None

                # Assume first row is column headers
                header_row = row_locs.nth(0)
                col_texts = []
                try:
                    header_cells = header_row.locator("div[dir='auto']")
                    n_header_cells = header_cells.count()
                    for j in range(n_header_cells):
                        txt = self._safe_inner_text(header_cells.nth(j), min(timeout_ms, 800))
                        if txt:
                            col_texts.append(txt)
                except Exception:
                    col_texts = []

                if col_texts and col_texts[0] == "":
                    cols = col_texts[1:]
                else:
                    cols = col_texts

                # Rows from subsequent rows' first cell
                rows = []
                for i in range(1, num_rows):
                    r_row = row_locs.nth(i)
                    try:
                        first_cell = r_row.locator("div[dir='auto']").first
                        txt = self._safe_inner_text(first_cell, min(timeout_ms, 800))
                        if txt:
                            rows.append(txt)
                    except Exception:
                        pass

                if not rows or not cols:
                    return None

                # Check cells in first data row
                first_data_row = row_locs.nth(1)
                cells_selector = "[role='radio'], [role='checkbox']"
                try:
                    cells = first_data_row.locator(cells_selector)
                    num_cells = cells.count()
                    if num_cells != len(cols):
                        return None
                    if num_cells == 0:
                        return None
                    cell_role = cells.first.get_attribute("role")
                    kind = "mc" if cell_role == "radio" else "checkbox" if cell_role == "checkbox" else ""
                    if not kind:
                        return None
                except Exception:
                    return None

                # Check consistency for other rows
                consistent = True
                for i in range(2, num_rows):
                    r = row_locs.nth(i)
                    try:
                        if r.locator(cells_selector).count() != num_cells:
                            consistent = False
                            break
                    except Exception:
                        consistent = False
                        break

                if not consistent:
                    return None

                print(f"GRID_DETECTED_ALTERNATIVE: kind={kind} rows={len(rows)} cols={len(cols)}")

            else:
                kind = ""
                try:
                    if container.locator("[role='radio']").count() > 0:
                        kind = "mc"
                    elif container.locator("[role='checkbox']").count() > 0:
                        kind = "checkbox"
                except Exception:
                    kind = ""

                if not kind:
                    return None

                print(f"GRID_DETECTED: kind={kind} rows={len(rows)} cols={len(cols)}")

        low_err = (error_text or "").lower()
        if not required:
            required_mode = "none"
        elif "each row" in low_err or "every row" in low_err:
            required_mode = "per_row"
        elif "at least one" in low_err or "select at least one" in low_err:
            required_mode = "at_least_one"
        elif required and kind == "mc":
            required_mode = "per_row"
        else:
            required_mode = "unknown"
        required_per_row = (required_mode == "per_row")
        row_required = None
        if required_per_row:
            row_required = {row: True for row in rows}

        return GridExtract(rows=rows, columns=cols, kind=kind, required_per_row=required_per_row, required_mode=required_mode, row_required=row_required)

    def _detect_date_kind(self, block) -> bool:
        try:
            if block.locator("input[type='date']").count() > 0:
                return True
        except Exception:
            pass
        tb = block.get_by_role("textbox")
        try:
            if tb.count() > 0:
                ph = (tb.first.get_attribute("placeholder") or "").lower()
                if any(p in ph for p in ["mm/dd", "dd/mm", "yyyy", "dd-mm", "mm-dd"]):
                    return True
        except Exception:
            pass
        return False

    def _detect_time_kind(self, block) -> bool:
        try:
            if block.locator("input[type='time']").count() > 0:
                return True
        except Exception:
            pass
        try:
            if block.locator("input[aria-label*='Hour'], input[aria-label*='hour']").count() > 0:
                return True
            if block.locator("input[aria-label*='Minute'], input[aria-label*='minute']").count() > 0:
                return True
        except Exception:
            pass
        tb = block.get_by_role("textbox")
        try:
            if tb.count() > 0:
                ph = (tb.first.get_attribute("placeholder") or "").lower()
                if "hh" in ph and "mm" in ph:
                    return True
        except Exception:
            pass
        return False

    def _detect_file_upload(self, block) -> bool:
        try:
            if block.locator("input[type='file']").count() > 0:
                return True
        except Exception:
            pass
        try:
            btn = block.get_by_role("button", name=re.compile(r"add file|upload", re.IGNORECASE))
            if btn.count() > 0:
                return True
        except Exception:
            pass
        return False

    def _classify_block(self, block, label: str, required: bool, error_text: str, timeout_ms: int) -> BlockInfo:
        if self._detect_file_upload(block):
            help_text = self._extract_help_text(block, label, [], timeout_ms)
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind="file_upload",
                required=required,
                error_text=error_text,
                options=[],
                allow_other=False,
                grid=None,
                input_attrs={},
            )

        grid = self._extract_grid_info(block, timeout_ms=timeout_ms, required=required, error_text=error_text)
        if grid is not None:
            help_text = self._extract_help_text(block, label, [], timeout_ms)
            kind = "mc_grid" if grid.kind == "mc" else "checkbox_grid"
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind=kind,
                required=required,
                error_text=error_text,
                options=[],
                allow_other=False,
                grid=grid,
                input_attrs={},
            )

        if self._detect_date_kind(block):
            help_text = self._extract_help_text(block, label, [], timeout_ms)
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind="date",
                required=required,
                error_text=error_text,
                input_attrs={},
            )

        if self._detect_time_kind(block):
            help_text = self._extract_help_text(block, label, [], timeout_ms)
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind="time",
                required=required,
                error_text=error_text,
                input_attrs={},
            )

        try:
            if block.locator("[role='radio'], input[type='radio']").count() > 0:
                opts, allow_other = self._extract_radio_options(block, timeout_ms=timeout_ms)
                help_text = self._extract_help_text(block, label, opts, timeout_ms)
                return BlockInfo(
                    block=block,
                    label_text=label,
                    help_text=help_text,
                    kind="radio",
                    required=required,
                    error_text=error_text,
                    options=opts,
                    allow_other=allow_other,
                    input_attrs={},
                )
        except Exception:
            pass

        try:
            if block.locator("[role='checkbox'], input[type='checkbox']").count() > 0:
                opts, allow_other = self._extract_checkbox_options(block, timeout_ms=timeout_ms)
                help_text = self._extract_help_text(block, label, opts, timeout_ms)
                return BlockInfo(
                    block=block,
                    label_text=label,
                    help_text=help_text,
                    kind="checkbox",
                    required=required,
                    error_text=error_text,
                    options=opts,
                    allow_other=allow_other,
                    input_attrs={},
                )
        except Exception:
            pass

        try:
            if block.locator("[role='listbox'], [role='combobox']").count() > 0:
                help_text = self._extract_help_text(block, label, [], timeout_ms)
                return BlockInfo(
                    block=block,
                    label_text=label,
                    help_text=help_text,
                    kind="dropdown",
                    required=required,
                    error_text=error_text,
                    input_attrs={},
                )
        except Exception:
            pass

        try:
            if block.locator("textarea").count() > 0:
                help_text = self._extract_help_text(block, label, [], timeout_ms)
                loc = block.locator("textarea").first
                attrs = {}
                if loc.count() > 0:
                    try:
                        if loc.is_visible():
                            for attr in ["min", "max", "minlength", "maxlength", "pattern", "inputmode", "step", "type", "aria-label", "aria-describedby"]:
                                val = self._safe_attr(loc, attr)
                                if val:
                                    attrs[attr] = val
                    except:
                        pass
                return BlockInfo(
                    block=block,
                    label_text=label,
                    help_text=help_text,
                    kind="paragraph",
                    required=required,
                    error_text=error_text,
                    input_attrs=attrs,
                )
        except Exception:
            pass

        try:
            if block.locator("input[type='text'], input:not([type])").count() > 0 or block.get_by_role("textbox").count() > 0:
                help_text = self._extract_help_text(block, label, [], timeout_ms)
                loc = block.locator('input[jsname="YPqjbf"]').first
                if loc.count() == 0:
                    loc = block.locator('input[type="text"], input[type="number"]').first
                if loc.count() == 0:
                    loc = block.get_by_role("textbox").first
                attrs = {}
                if loc.count() > 0:
                    try:
                        if loc.is_visible():
                            for attr in ["min", "max", "minlength", "maxlength", "pattern", "inputmode", "step", "type", "aria-label", "aria-describedby"]:
                                val = self._safe_attr(loc, attr)
                                if val:
                                    attrs[attr] = val
                    except:
                        pass
                return BlockInfo(
                    block=block,
                    label_text=label,
                    help_text=help_text,
                    kind="text",
                    required=required,
                    error_text=error_text,
                    input_attrs=attrs,
                )
        except Exception:
            pass

        help_text = self._extract_help_text(block, label, [], timeout_ms)
        return BlockInfo(
            block=block,
            label_text=label,
            help_text=help_text,
            kind="unknown",
            required=required,
            error_text=error_text,
            input_attrs={},
        )

    def visible_blocks(self, timeout_ms: Optional[int] = None) -> List[BlockInfo]:
        t_ms = timeout_ms or self.timeout_ms
        blocks_out: List[BlockInfo] = []

        # Fast path: check if any visible candidates exist
        candidates = [
            "div.freebirdFormviewerViewItemsItemItem",
            "div[role='listitem']",
            "div[jscontroller][data-item-id]",
        ]
        has_visible = False
        for sel in candidates:
            try:
                if self.page.locator(sel).filter(visible=True).count() > 0:
                    has_visible = True
                    break
            except:
                pass
        if not has_visible:
            return []

        def _add_block(loc) -> None:
            label = self._extract_label(loc, timeout_ms=min(t_ms, 300))  # Shortened timeout
            if not label:
                try:
                    alt = loc.locator(
                        "[role='heading'], [data-item-title], .freebirdFormviewerViewItemsItemItemTitle, .M7eMe"
                    ).first
                    label = self._safe_inner_text(alt, timeout_ms=min(t_ms, 300))
                except Exception:
                    label = ""
            label = normalize_button_label(label)
            if not label:
                return

            required = self._detect_required(loc, label_text=label)
            err = self._extract_error_text(loc, timeout_ms=min(t_ms, 300))
            bi = self._classify_block(loc, label=label, required=required, error_text=err, timeout_ms=min(t_ms, 300))
            blocks_out.append(bi)

        # Primary path: role=listitem (keep behavior when it works)
        blocks = self.page.locator("div[role='listitem']")
        try:
            count = blocks.count()
        except Exception:
            count = 0

        for i in range(count):
            b = blocks.nth(i)
            try:
                if not b.is_visible():
                    continue
            except Exception:
                continue
            _add_block(b)

        # Fallbacks only when listitems yield 0 usable blocks (e.g., final submit page / unstable DOM)
        if not blocks_out:
            seen: set[str] = set()
            for sel in candidates:
                locs = self.page.locator(sel)
                try:
                    n = locs.count()
                except Exception:
                    n = 0
                for i in range(n):
                    b = locs.nth(i)
                    try:
                        if not b.is_visible():
                            continue
                    except Exception:
                        continue

                    label = self._extract_label(b, timeout_ms=min(t_ms, 300))
                    if not label:
                        try:
                            alt = b.locator(
                                "[role='heading'], [data-item-title], .freebirdFormviewerViewItemsItemItemTitle, .M7eMe"
                            ).first
                            label = self._safe_inner_text(alt, timeout_ms=min(t_ms, 300))
                        except Exception:
                            label = ""
                    label = normalize_button_label(label)
                    if not label:
                        continue
                    key = normalize_question_identity(label)
                    if key in seen:
                        continue
                    seen.add(key)

                    required = self._detect_required(b, label_text=label)
                    err = self._extract_error_text(b, timeout_ms=min(t_ms, 300))
                    bi = self._classify_block(b, label=label, required=required, error_text=err, timeout_ms=min(t_ms, 300))
                    blocks_out.append(bi)

        print(f"VISIBLE QUESTIONS: {[b.label_text for b in blocks_out]}")
        return blocks_out

    # ---------- Dropdown helpers ----------

    def _dropdown_current_text(self, dd_loc) -> str:
        try:
            return normalize_button_label(dd_loc.inner_text(timeout=500)) or ""
        except:
            return ""

    def _dropdown_is_unselected(self, dd_loc) -> bool:
        txt = self._dropdown_current_text(dd_loc).lower()
        placeholders = {"", "choose", "select", "choose an option", "select an option", "select one", "choose one"}
        return txt in placeholders

    def _dropdown_has_valid_selection(self, dd_loc) -> bool:
        return not self._dropdown_is_unselected(dd_loc)

    def _dropdown_open(self, block_or_dd) -> bool:
        if hasattr(block_or_dd, 'locator'):
            dd = block_or_dd.locator("[role='listbox'], [role='combobox']").first
        else:
            dd = block_or_dd
        try:
            dd.click(timeout=1500)
            return True
        except:
            return False

    def _dropdown_pick_first_valid_option(self, block_or_dd, avoid_placeholder=True) -> Optional[str]:
        if not self._dropdown_open(block_or_dd):
            return None
        opts = self.page.locator("[role='option']")
        try:
            n = opts.count()
        except:
            n = 0
        for i in range(n):
            o = opts.nth(i)
            try:
                if not o.is_visible():
                    continue
            except:
                continue
            txt = self._safe_inner_text(o, 500).lower()
            if avoid_placeholder and txt in {"choose", "select", "choose an option", "select an option"}:
                continue
            try:
                o.click(timeout=1500)
                time.sleep(0.5)
                dd = block_or_dd.locator("[role='listbox'], [role='combobox']").first if hasattr(block_or_dd, 'locator') else block_or_dd
                new_txt = self._dropdown_current_text(dd)
                if new_txt and new_txt.lower() not in {"", "choose", "select"}:
                    return new_txt
                # Retry
                self._dropdown_open(block_or_dd)
                o.click(timeout=1500)
                time.sleep(0.5)
                new_txt = self._dropdown_current_text(dd)
                if new_txt and new_txt.lower() not in {"", "choose", "select"}:
                    return new_txt
            except:
                pass
        try:
            self.page.keyboard.press("Escape")
        except:
            pass
        return None

    # ---------- Signature ----------

    def section_signature(self) -> str:
        """
        Runner-aligned, robust section signature.

        ORDER (stable):
          1) Try visible_blocks -> return "Q:label1||label2||label3" (top 3 labels) if found.
          2) Compute submit_visible + next_visible ONCE. If submit_visible and NOT next_visible:
               - poll up to 2000ms (every 200ms) trying visible_blocks again
               - if blocks appear, return "Q:..."
               - else return "__TERMINAL_SUBMIT__"
          3) Headings fallback (aria-level 2/3) ONLY if:
               - not equal to form title (aria-level 1)
               - does NOT contain "*" (avoid unstable signatures like "Full Name *")
          4) NAV fallback ALWAYS:
               - labels = nav_button_labels(); if labels return "NAV:" + "|".join(labels)
          5) BODYHASH fallback:
               - fp = _container_text_fingerprint(); if fp return fp
          6) else return ""
        Never uses the form title alone as a signature.
        """
        # 1) Q fingerprint (top 3 visible labels)
        try:
            blocks = self.visible_blocks(timeout_ms=min(self.timeout_ms, 1500))
            top = [normalize_button_label(b.label_text) for b in blocks[:3] if normalize_button_label(b.label_text)]
            if top:
                return "Q:" + "||".join(top)
        except Exception:
            pass

        # 2) Terminal-aware submit check (poll for late-rendered blocks)
        try:
            submit_visible = self.is_submit_visible()
            next_visible = self.is_next_visible()
        except Exception:
            submit_visible, next_visible = False, False

        if submit_visible and (not next_visible):
            t0 = time.perf_counter()
            while (time.perf_counter() - t0) * 1000.0 < 2000:
                try:
                    blocks = self.visible_blocks(timeout_ms=min(self.timeout_ms, 1200))
                    top = [normalize_button_label(b.label_text) for b in blocks[:3] if normalize_button_label(b.label_text)]
                    if top:
                        return "Q:" + "||".join(top)
                except Exception:
                    pass
                try:
                    self.page.wait_for_timeout(200)
                except Exception:
                    break
            return "__TERMINAL_SUBMIT__"

        # 3) Headings fallback (2/3), stable only
        form_title = ""
        try:
            form_title = self._safe_inner_text(self.page.locator("[role='heading'][aria-level='1']").first, timeout_ms=500)
        except Exception:
            form_title = ""

        for level in ("2", "3"):
            try:
                h = self.page.locator(f"[role='heading'][aria-level='{level}']").first
                txt = self._safe_inner_text(h, timeout_ms=500)
                if not txt:
                    continue
                if txt == form_title:
                    continue
                if "*" in txt:
                    continue
                return txt
            except Exception:
                continue

        # 4) NAV fallback ALWAYS
        try:
            labels = self.nav_button_labels()
            if labels:
                return "NAV:" + "|".join(labels)
        except Exception:
            pass

        # 5) BODYHASH fallback
        try:
            fp = self._container_text_fingerprint()
            if fp:
                return fp
        except Exception:
            pass

        return ""

    # ---------- Loop protection ----------

    def note_signature(self, sig: str) -> None:
        if not sig:
            return
        self._sig_counts[sig] = self._sig_counts.get(sig, 0) + 1
        if self._sig_counts[sig] > self.signature_repeat_max:
            self.dump_diagnostics("loop_protection")
            raise RuntimeError(f"Loop protection: signature repeated too many times: {sig}")

    # ---------- Nav buttons (role=button only, exact trimmed match) ----------

    def nav_button_labels(self) -> List[str]:
        # Prefer bounded JS query (avoids hangs / slow locator iteration on some pages)
        try:
            raw = self.page.eval_on_selector_all(
                'div[role="button"]',
                "els => els.map(e => (e.innerText||'').trim()).filter(Boolean)",
                timeout=min(self.timeout_ms, 700),
            )
            labels = [normalize_button_label(str(x)) for x in (raw or [])]
            labels = [x for x in labels if x]
            if labels:
                seen: set[str] = set()
                deduped: List[str] = []
                for x in labels:
                    if x in seen:
                        continue
                    seen.add(x)
                    deduped.append(x)
                return deduped
        except Exception:
            pass

        labels: List[str] = []
        btns = self.page.locator('div[role="button"]')
        try:
            n = btns.count()
        except Exception:
            n = 0
        for i in range(n):
            b = btns.nth(i)
            try:
                if not b.is_visible():
                    continue
            except Exception:
                continue
            txt = self._safe_inner_text(b, timeout_ms=800)
            if txt:
                labels.append(txt)

        if not labels:
            return labels

        seen2: set[str] = set()
        deduped2: List[str] = []
        for x in labels:
            if x in seen2:
                continue
            seen2.add(x)
            deduped2.append(x)
        return deduped2

    def _container_text_fingerprint(self) -> str:
        try:
            txt = self.form_container().inner_text(timeout=min(self.timeout_ms, 800)) or ""
        except Exception:
            txt = ""
        txt = normalize_button_label(txt)[:600]
        if not txt:
            return ""
        h = hashlib.md5(txt.encode("utf-8", errors="ignore")).hexdigest()[:10]
        return f"BODYHASH:{h}"

    def find_nav_button(self, exact_text: str) -> Optional[Any]:
        target = exact_text.strip().lower()
        btns = self.page.locator('div[role="button"]')
        try:
            n = btns.count()
        except Exception:
            return None
        for i in range(n):
            b = btns.nth(i)
            try:
                if not b.is_visible():
                    continue
            except Exception:
                continue
            txt = self._safe_inner_text(b, timeout_ms=800).lower()
            if txt == target:
                return b
        return None

    def is_next_visible(self) -> bool:
        return self.find_nav_button("Next") is not None

    def is_submit_visible(self) -> bool:
        return self.find_nav_button("Submit") is not None

    def is_terminal(self) -> bool:
        return self.is_submit_visible() and (not self.is_next_visible())

    # ---------- Quick state for nav change detection ----------

    def _quick_nav_state(self) -> str:
        try:
            nav_labels = self.nav_button_labels()
            sig = self.section_signature()
            return "|".join(nav_labels) + "||" + sig
        except Exception:
            return ""

    def _poll_quick_state_change(self, before: str, total_ms: int = 1500, sleep_ms: int = 100) -> bool:
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) * 1000.0 < total_ms:
            now = self._quick_nav_state()
            if now != before:
                return True
            try:
                self.page.wait_for_timeout(sleep_ms)
            except Exception:
                break
        return False

    # ---------- Robust click ----------

    def _robust_click_locator(self, loc: Any, label: str = "", max_attempts: int = 3) -> bool:
        for attempt in range(max_attempts):
            try:
                if not loc.is_visible() or not loc.is_enabled():
                    return False
            except Exception:
                return False

            try:
                loc.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass

            strategies = [
                lambda: loc.click(timeout=1000, no_wait_after=True),
                lambda: loc.click(timeout=1000, force=True, no_wait_after=True),
                lambda: loc.evaluate("el => el.click()"),
            ]

            for strat in strategies:
                try:
                    strat()
                    time.sleep(0.2)  # Brief settle
                    return True
                except PlaywrightTimeoutError:
                    print(f"Robust click attempt {attempt+1} timeout for {label}; retrying strategy")
                except Exception:
                    pass

            time.sleep(0.5)  # Brief delay between attempts

        return False

    # ---------- Diagnostics ----------

    def dump_diagnostics(self, prefix: str) -> None:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        png = self.diagnostics_dir / f"{prefix}_{ts}.png"
        html = self.diagnostics_dir / f"{prefix}_{ts}.container.html"
        try:
            self.page.screenshot(path=str(png), full_page=True)
        except Exception:
            pass
        try:
            container_html = self.form_container().inner_html(timeout=min(self.timeout_ms, 2000))
            html.write_text(container_html or "", encoding="utf-8")
        except Exception:
            try:
                html.write_text(self.page.content(), encoding="utf-8")
            except Exception:
                pass

    # ---------- Validation check ----------

    def has_visible_validation_errors(self) -> bool:
        blocks = self.visible_blocks(timeout_ms=min(self.timeout_ms, 1500))
        return any(b.error_text for b in blocks)

    # ---------- Two-phase fill ----------

    def _parse_exact_literal(self, text: str) -> Optional[str]:
        low = (text or "").lower()
        if "exactly" not in low:
            return None
        m = re.search(r"exactly\s+(.+?)(?:[.!?]$|$)", text, flags=re.IGNORECASE)
        if not m:
            return None
        lit = m.group(1).strip().strip(' "\'')
        return lit or None

    def _parse_digit_constraint(self, text: str) -> Optional[int]:
        low = text.lower()
        m = re.search(r"(\d+)\s*(?:digits?|digit)", low)
        if m:
            return int(m.group(1))
        m = re.search(r"exactly\s*(\d+)\s*(?:digits?|digit)", low)
        if m:
            return int(m.group(1))
        m = re.search(r"must be\s*(\d+)\s*(?:digits?|digit)", low)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)-digit", low)
        if m:
            return int(m.group(1))
        return None

    def _parse_min_max(self, text: str) -> Tuple[Optional[int], Optional[int]]:
        low = text.lower()
        # between X and Y
        m = re.search(r"between\s+(\d+)\s+and\s+(\d+)", low)
        if m:
            return int(m.group(1)), int(m.group(2))
        # X-Y / X–Y / X—Y
        m = re.search(r"(\d+)\s*[-–—]\s*(\d+)", low)
        if m:
            return int(m.group(1)), int(m.group(2))
        # at least X
        m = re.search(r"at least\s+(\d+)", low)
        if m:
            return int(m.group(1)), None
        # at most Y
        m = re.search(r"at most\s+(\d+)", low)
        if m:
            return None, int(m.group(1))
        return None, None

    def _label_obvious_text(self, label: str) -> Optional[str]:
        low = (label or "").lower()
        lit = self._parse_exact_literal(label)
        if lit:
            return Gen.exact(lit)
        if "confirm email" in low or "re-enter email" in low or "reenter email" in low:
            return Gen.email()
        if "email" in low:
            return Gen.email()
        if any(k in low for k in ["phone", "mobile", "whatsapp"]):
            return Gen.phone()
        if any(k in low for k in ["pincode", "zip"]):
            return Gen.pin()
        if "date" in low:
            return Gen.date_mmddyyyy()
        if "time" in low:
            return Gen.time_hhmm()

        if (
            "₹" in (label or "")
            or re.search(r"\brs\b", low) is not None
            or any(k in low for k in ["rupee", "amount", "spend", "budget", "cost", "price", "income", "salary"])
        ):
            return Gen.numeric(1000, 250000)

        if any(k in low for k in ["age", "year", "number", "digits", "zip", "postal", "code", "id"]):
            return Gen.numeric()
        if any(k in low for k in ["message", "comment", "note"]):
            return "Interested in learning more. Thank you!"
        return Gen.text()

    def _grid_fill_defaults(self, b: BlockInfo) -> bool:
        if not b.grid:
            return False
        grid = b.grid
        container = b.block.locator("[role='grid'], [role='table']").first
        try:
            if container.count() == 0 or not container.is_visible():
                container = None
        except:
            container = None

        changed = False

        rows = []
        if container:
            row_locs = container.locator("[role='row']")
            try:
                n_rows = row_locs.count()
            except:
                n_rows = 0
            for i in range(n_rows):
                r = row_locs.nth(i)
                try:
                    if not r.is_visible():
                        continue
                except:
                    continue
                if r.locator("[role='rowheader']").count() == 0:
                    continue
                rows.append(r)
            if not rows and n_rows > 1:
                for i in range(1, n_rows):
                    r = row_locs.nth(i)
                    try:
                        if r.is_visible():
                            rows.append(r)
                    except:
                        pass

        use_fallback = container is None or not rows

        selector = '[role="radio"]' if grid.kind == "mc" else '[role="checkbox"]'

        if grid.required_per_row:
            row_names = grid.rows if use_fallback else [self._safe_inner_text(r.locator("[role='rowheader']").first, 800) for r in rows if r]
            for row_idx, row_name in enumerate(row_names):
                if grid.row_required and row_name in grid.row_required and not grid.row_required[row_name]:
                    continue
                if use_fallback:
                    cells = b.block.locator(f'{selector}[aria-label*="response for {row_name}"]')
                else:
                    cells = rows[row_idx].locator(selector)
                try:
                    n_cells = cells.count()
                except:
                    n_cells = 0
                if n_cells == 0:
                    continue
                has_selection = False
                for j in range(n_cells):
                    if self._safe_attr(cells.nth(j), "aria-checked") == "true":
                        has_selection = True
                        break
                if has_selection:
                    continue
                to_select = 1
                selected = 0
                for j in range(n_cells):
                    try:
                        cells.nth(j).click(timeout=1500)
                        changed = True
                        selected += 1
                        if selected >= to_select:
                            break
                    except:
                        pass
        else:
            if not b.required:
                return False
            has_any = b.block.locator(f'{selector}[aria-checked="true"]').count() > 0
            if has_any:
                return False
            cells = b.block.locator(selector)
            try:
                n_cells = cells.count()
            except:
                n_cells = 0
            if n_cells > 0:
                try:
                    cells.first.click(timeout=1500)
                    changed = True
                except:
                    pass

        return changed

    def fill_defaults_for_required_visible_questions(self) -> bool:
        """
        Phase A (defaults for required fields), visible-only.

        - Never override already-answered fields (branch probing must stick).
        - radios: choose best-scoring visible option (avoid Submit/Finish-ish).
        - checkboxes: select best-scoring option (at least 1 when required).
        - dropdown: pick first visible option if empty/unselected.
        - text/paragraph/date/time: fill heuristics.
        - grids: pick first column per row when required_per_row.
        """
        blocks = self.visible_blocks(timeout_ms=min(self.timeout_ms, 2500))
        changed_any = False

        positive = ["continue", "next", "detailed", "yes", "agree", "start", "proceed"]
        negative = ["end", "submit", "finish", "stop", "exit", "none", "no", "don't", "dont", "not"]
        crawl_avoid = ["end", "submit", "finish"]

        def score_option(text: str) -> int:
            low = (text or "").strip().lower()
            s = 0
            if low.startswith("other"):
                return -1
            for p in positive:
                if p in low:
                    s += 2
            for n in negative:
                if n in low:
                    s -= 1
            for ca in crawl_avoid:
                if ca in low:
                    s -= 10
            return s

        for b in blocks:
            if not b.required:
                continue
            if b.kind == "text":
                loc = b.block.locator('input[jsname="YPqjbf"], input[type="text"], input[type="number"], input[type="email"], input[type="tel"]').first
                if loc.count() == 0:
                    loc = b.block.get_by_role("textbox").first
                try:
                    if loc.count() == 0 or not loc.is_visible():
                        continue
                except Exception:
                    continue
                val = self._safe_input_value(loc, 500)
                if val:
                    continue
                lit = self._parse_exact_literal(b.error_text)
                if lit:
                    try:
                        loc.fill(lit, timeout=1500)
                        changed_any = True
                        continue
                    except Exception:
                        pass
                text = self._label_obvious_text(b.label_text) or Gen.text()
                try:
                    loc.fill(text, timeout=1500)
                    changed_any = True
                except Exception:
                    pass
            elif b.kind == "paragraph":
                loc = b.block.locator("textarea").first
                try:
                    if loc.count() == 0 or not loc.is_visible():
                        continue
                except Exception:
                    continue
                val = self._safe_input_value(loc, 500)
                if val:
                    continue
                text = self._label_obvious_text(b.label_text) or Gen.text()
                try:
                    loc.fill(text, timeout=1500)
                    changed_any = True
                except Exception:
                    pass
            elif b.kind == "date":
                try:
                    if b.block.locator("input[type='date']").count() == 0:
                        continue
                except Exception:
                    continue
                text = Gen.date_mmddyyyy()
                try:
                    b.block.locator("input[type='date']").first.fill(text, timeout=1500)
                    changed_any = True
                except Exception:
                    pass
            elif b.kind == "time":
                try:
                    if b.block.locator("input[type='time']").count() == 0:
                        continue
                except Exception:
                    continue
                text = Gen.time_hhmm()
                try:
                    b.block.locator("input[type='time']").first.fill(text, timeout=1500)
                    changed_any = True
                except Exception:
                    pass
            elif b.kind == "radio":
                try:
                    if b.block.locator("[role='radio'][aria-checked='true']").count() > 0:
                        continue
                except Exception:
                    pass
                radios = b.block.locator("[role='radio']")
                try:
                    n = radios.count()
                except Exception:
                    n = 0
                best_i = -1
                best_s = -999
                for i in range(n):
                    r = radios.nth(i)
                    try:
                        if not r.is_visible():
                            continue
                    except Exception:
                        continue
                    label = self._safe_attr(r, "aria-label")
                    s = score_option(label)
                    if s > best_s:
                        best_s = s
                        best_i = i
                if best_i >= 0:
                    try:
                        radios.nth(best_i).click(timeout=1500)
                        changed_any = True
                        continue
                    except Exception:
                        pass
                try:
                    radios.first.click(timeout=1500)
                    changed_any = True
                except Exception:
                    pass
            elif b.kind == "checkbox":
                try:
                    if b.block.locator("[role='checkbox'][aria-checked='true']").count() > 0:
                        continue
                except Exception:
                    pass
                cbs = b.block.locator("[role='checkbox']")
                try:
                    n = cbs.count()
                except Exception:
                    n = 0
                best_i = -1
                best_s = -999
                for i in range(n):
                    c = cbs.nth(i)
                    try:
                        if not c.is_visible():
                            continue
                    except Exception:
                        continue
                    label = self._safe_attr(c, "aria-label")
                    s = score_option(label)
                    if s > best_s:
                        best_s = s
                        best_i = i
                if best_i >= 0:
                    try:
                        cbs.nth(best_i).click(timeout=1500)
                        changed_any = True
                        continue
                    except Exception:
                        pass
                try:
                    cbs.first.click(timeout=1500)
                    changed_any = True
                except Exception:
                    pass
            elif b.kind == "dropdown":
                dd = b.block.locator("[role='listbox'], [role='combobox']").first
                try:
                    if dd.count() == 0 or not dd.is_visible():
                        continue
                except Exception:
                    continue
                if self._dropdown_has_valid_selection(dd):
                    continue
                sel = self._dropdown_pick_first_valid_option(b.block)
                if sel:
                    changed_any = True
            elif b.kind in {"mc_grid", "checkbox_grid"}:
                changed_any |= self._grid_fill_defaults(b)

        return changed_any

    def _repair_radio(self, block: BlockInfo) -> bool:
        try:
            radios = block.block.locator("[role='radio']")
            n = radios.count()
        except Exception:
            n = 0
        for i in range(n):
            r = radios.nth(i)
            try:
                if not r.is_visible():
                    continue
            except Exception:
                continue
            try:
                r.click(timeout=1500)
                return True
            except Exception:
                continue
        return False

    def _repair_checkbox(self, block: BlockInfo) -> bool:
        try:
            cbs = block.block.locator("[role='checkbox']")
            n = cbs.count()
        except Exception:
            n = 0
        for i in range(n):
            c = cbs.nth(i)
            try:
                if not c.is_visible():
                    continue
            except Exception:
                continue
            try:
                c.click(timeout=1500)
                return True
            except Exception:
                continue
        return False

    def _repair_dropdown(self, block: BlockInfo) -> bool:
        dd = block.block.locator("[role='listbox'], [role='combobox']").first
        try:
            if dd.count() == 0 or not dd.is_visible():
                return False
        except Exception:
            return False
        sel = self._dropdown_pick_first_valid_option(block.block)
        return bool(sel)

    def _repair_text(self, block: BlockInfo, error_text: str) -> bool:
        loc = block.block.locator('input[jsname="YPqjbf"], input[type="text"], input[type="number"], input[type="email"], input[type="tel"]').first
        if loc.count() == 0:
            loc = block.block.get_by_role("textbox").first
        try:
            if loc.count() == 0 or not loc.is_visible():
                return False
        except Exception:
            return False

        lit = self._parse_exact_literal(error_text)
        if lit:
            try:
                loc.fill(lit, timeout=1500)
                return True
            except Exception:
                pass

        digits = self._parse_digit_constraint(error_text)
        if digits:
            try:
                loc.fill(Gen.pin(digits), timeout=1500)
                return True
            except Exception:
                pass

        mn, mx = self._parse_min_max(error_text)
        if mn or mx:
            try:
                loc.fill(Gen.numeric(mn, mx), timeout=1500)
                return True
            except Exception:
                pass

        low_err = error_text.lower()
        if "email" in low_err:
            try:
                loc.fill(Gen.email(), timeout=1500)
                return True
            except Exception:
                pass
        if "number" in low_err or "digits" in low_err:
            try:
                loc.fill(Gen.numeric(), timeout=1500)
                return True
            except Exception:
                pass
        if "phone" in low_err:
            try:
                loc.fill(Gen.phone(), timeout=1500)
                return True
            except Exception:
                pass

        try:
            loc.fill(Gen.text(), timeout=1500)
            return True
        except Exception:
            pass
        return False

    def _repair_paragraph(self, block: BlockInfo, error_text: str) -> bool:
        loc = block.block.locator("textarea").first
        try:
            if loc.count() == 0 or not loc.is_visible():
                return False
        except Exception:
            return False

        lit = self._parse_exact_literal(error_text)
        if lit:
            try:
                loc.fill(lit, timeout=1500)
                return True
            except Exception:
                pass

        try:
            loc.fill("Test message.", timeout=1500)
            return True
        except Exception:
            pass
        return False

    def _repair_date(self, block: BlockInfo) -> bool:
        try:
            if block.block.locator("input[type='date']").count() == 0:
                return False
        except Exception:
            return False
        text = Gen.date_mmddyyyy()
        try:
            block.block.locator("input[type='date']").first.fill(text, timeout=1500)
            return True
        except Exception:
            pass
        return False

    def _repair_time(self, block: BlockInfo) -> bool:
        try:
            if block.block.locator("input[type='time']").count() == 0:
                return False
        except Exception:
            return False
        text = Gen.time_hhmm()
        try:
            block.block.locator("input[type='time']").first.fill(text, timeout=1500)
            return True
        except Exception:
            pass
        return False

    def repair_from_validation_errors(self) -> bool:
        """
        Phase B (repair from visible errors), visible-only.

        - radio: try next option
        - checkbox: toggle first
        - dropdown: try next option
        - text: try parsed literal/pattern, or heuristics from error text
        - grids: fill unsatisfied rows based on error
        """
        blocks = self.visible_blocks(timeout_ms=min(self.timeout_ms, 2500))
        changed_any = False

        for b in blocks:
            if not b.error_text:
                continue
            if b.kind == "radio":
                changed_any |= self._repair_radio(b)
            elif b.kind == "checkbox":
                changed_any |= self._repair_checkbox(b)
            elif b.kind == "dropdown":
                changed_any |= self._repair_dropdown(b)
            elif b.kind == "text":
                changed_any |= self._repair_text(b, b.error_text)
            elif b.kind == "paragraph":
                changed_any |= self._repair_paragraph(b, b.error_text)
            elif b.kind == "date":
                changed_any |= self._repair_date(b)
            elif b.kind == "time":
                changed_any |= self._repair_time(b)
            elif b.kind in {"mc_grid", "checkbox_grid"}:
                low_err = b.error_text.lower()
                if "row" in low_err or "response" in low_err or "required" in low_err:
                    changed_any |= self._grid_fill_defaults(b)

        return changed_any

    def require_non_empty_signature(self, prefix: str, timeout_ms: Optional[int] = None) -> str:
        t0 = time.perf_counter()
        t_max = (timeout_ms or self.timeout_ms) / 1000.0
        sig = ""
        while not sig:
            sig = self.section_signature()
            if sig:
                return sig
            if (time.perf_counter() - t0) > t_max:
                self.log_empty_signature_debug()
                self.dump_diagnostics(f"{prefix}_empty_sig")
                raise RuntimeError(f"{prefix}: empty signature after timeout")
            try:
                self.page.wait_for_timeout(200)
            except Exception:
                break
        return sig

    def wait_for_signature_change(self, old_sig: str, timeout_ms: Optional[int] = None) -> str:
        t0 = time.perf_counter()
        t_max = (timeout_ms or self.timeout_ms) / 1000.0
        while (time.perf_counter() - t0) < t_max:
            sig_now = self.section_signature()
            if sig_now != old_sig:
                return sig_now
            try:
                self.page.wait_for_timeout(200)
            except Exception:
                break
        self.log_empty_signature_debug()
        self.dump_diagnostics("sig_change_timeout")
        raise RuntimeError(f"Signature change timeout (stuck on {old_sig})")

    def wait_for_section_settle(self, old_sig: str, timeout_ms: Optional[int] = None) -> str:
        t0 = time.perf_counter()
        t_max = (timeout_ms or self.timeout_ms) / 1000.0
        while (time.perf_counter() - t0) < t_max:
            if self.has_visible_validation_errors():
                return old_sig  # Errors visible; not settled
            if self.is_terminal():
                return "__TERMINAL_SUBMIT__"
            try:
                self.page.wait_for_timeout(200)
            except Exception:
                break
        return self.section_signature()  # Recompute with blocks

    def click_nav(self, label: str, no_wait_after: bool = False) -> None:
        btn = (
            self.find_nav_button(label)
            if label not in {"Back", "Previous"}
            else (self.find_nav_button("Back") or self.find_nav_button("Previous"))
        )
        if btn is None:
            raise RuntimeError(f"Nav button not found: {label}")
        success = self._robust_click_locator(btn, label=label)
        if not success:
            raise RuntimeError(f"Robust click failed for {label}")
        print(f"NAV: clicked {label}")

    def click_next_with_solver(self, max_repairs: int = 2) -> str:
        old_sig = self.require_non_empty_signature("pre_next", timeout_ms=8000)
        self.note_signature(old_sig)

        if not self.is_next_visible():
            raise RuntimeError("Next not visible")

        for attempt in range(max_repairs + 1):
            self.fill_defaults_for_required_visible_questions()
            before_quick = self._quick_nav_state()
            self.click_nav("Next", no_wait_after=True)

            # Brief poll for quick change
            if self._poll_quick_state_change(before_quick, total_ms=1500):
                pass  # Proceed to settle

            # Short poll for change or errors
            t0 = time.perf_counter()
            while (time.perf_counter() - t0) * 1000.0 < 1500:
                sig_now = self.section_signature()
                if sig_now != old_sig:
                    return sig_now
                if self.has_visible_validation_errors():
                    break
                self.page.wait_for_timeout(200)

            if self.has_visible_validation_errors():
                repaired = self.repair_from_validation_errors()
                if repaired:
                    continue
                else:
                    self.dump_diagnostics("next_failed")
                    raise RuntimeError("Next failed after repairs")

            try:
                new_sig = self.wait_for_section_settle(old_sig, timeout_ms=2500)
                if new_sig != old_sig:
                    if new_sig == "__TERMINAL_SUBMIT__" and self.never_submit:
                        print("CRAWL: reached terminal submit screen")
                    return new_sig
                new_sig = self.wait_for_signature_change(old_sig, timeout_ms=8000)
                if new_sig == "__TERMINAL_SUBMIT__" and self.never_submit:
                    print("CRAWL: reached terminal submit screen")
                return new_sig
            except Exception:
                if self.has_visible_validation_errors():
                    repaired = self.repair_from_validation_errors()
                    if repaired and attempt < max_repairs:
                        continue
                self.dump_diagnostics("next_failed")
                raise

        self.dump_diagnostics("next_failed_final")
        raise RuntimeError("Next failed after repairs")

    def click_back_with_solver(self) -> str:
        old_sig = self.require_non_empty_signature("pre_back", timeout_ms=8000)
        self.note_signature(old_sig)
        before_quick = self._quick_nav_state()
        label = "Back" if self.find_nav_button("Back") else "Previous"
        if not label:
            raise RuntimeError("Back/Previous not visible")
        self.click_nav(label, no_wait_after=True)

        # Brief poll for quick change
        self._poll_quick_state_change(before_quick, total_ms=1500)

        new_sig = self.wait_for_section_settle(old_sig, timeout_ms=2500)
        if new_sig != old_sig:
            return new_sig
        return self.wait_for_signature_change(old_sig, timeout_ms=8000)

    def back_until_label_visible(self, label: str, max_steps: int = 6) -> None:
        target = normalize_button_label(label)
        for _ in range(max_steps + 1):
            blocks = self.visible_blocks(timeout_ms=min(self.timeout_ms, 2000))
            if any(normalize_button_label(b.label_text) == target for b in blocks):
                return
            if (self.find_nav_button("Back") is None) and (self.find_nav_button("Previous") is None):
                return
            self.click_back_with_solver()


# -----------------------------
# Wizard extraction + crawl graph discovery
# -----------------------------


def kind_to_type_guess(kind: str, options: List[str], grid: Optional[GridExtract], allow_other: bool) -> Tuple[str, bool]:
    """
    Returns (type_guess, unsupported)
    """
    if kind == "text":
        return "text", False
    if kind in {"paragraph", "textarea"}:
        return "paragraph", False
    if kind == "radio":
        if options and all(re.fullmatch(r"\d+", (o or "").strip()) for o in options) and 2 <= len(options) <= 10:
            return "scale", False
        return "radio", False
    if kind == "dropdown":
        return "dropdown", False
    if kind == "checkbox":
        return "checkbox", False
    if kind == "date":
        return "date", False
    if kind == "time":
        return "time", False
    if kind == "mc_grid":
        return "mc_grid", False
    if kind == "checkbox_grid":
        return "checkbox_grid", False
    if kind == "file_upload":
        return "file_upload", True
    return "unknown", False


def extract_dropdown_options_for_wizard(solver: ConstraintSolver, block: Any) -> Tuple[List[str], bool]:
    """
    Returns (options, allow_other).
    """
    opts_out: List[str] = []
    allow_other = False
    try:
        dd = block.locator("[role='listbox'], [role='combobox']").first
        if dd.count() == 0:
            dd = block.locator("div[role='button'][aria-label*='Choose']").first
        if dd.count() == 0 or (not dd.is_visible()):
            return [], False
        dd.click(timeout=min(solver.timeout_ms, 1500))
        solver.page.wait_for_selector('div[role="option"]', timeout=1500)
        opts_texts = solver.page.locator('div[role="option"]').all_inner_texts()
        opts_texts = [normalize_button_label(t) for t in opts_texts if t.strip()]
        opts_out = list(set(opts_texts))  # dedupe
        placeholders = {"choose", "select", "choose an option", "select an option", "select one", "choose one"}
        opts_out = [o for o in opts_out if o.lower() not in placeholders]
        for o in opts_out:
            low = o.lower()
            if low in {"other", "other:"} or low.startswith("other"):
                allow_other = True
                opts_out.remove(o)
        try:
            solver.page.keyboard.press("Escape")
        except Exception:
            pass
        if not opts_out:
            time.sleep(1)  # retry delay
            dd.click(timeout=min(solver.timeout_ms, 1500))
            solver.page.wait_for_selector('div[role="option"]', timeout=1500)
            opts_texts = solver.page.locator('div[role="option"]').all_inner_texts()
            opts_texts = [normalize_button_label(t) for t in opts_texts if t.strip()]
            opts_out = list(set(opts_texts))  # dedupe
            opts_out = [o for o in opts_out if o.lower() not in placeholders]
            for o in opts_out:
                low = o.lower()
                if low in {"other", "other:"} or low.startswith("other"):
                    allow_other = True
                    opts_out.remove(o)
            try:
                solver.page.keyboard.press("Escape")
            except Exception:
                pass
    except Exception:
        try:
            solver.page.keyboard.press("Escape")
        except Exception:
            pass
    if not opts_out:
        return [], False  # mark as no options instead of unsupported
    return opts_out, allow_other


def crawl_form(page, timeout_ms: int) -> Tuple[List[QuestionExtract], Dict[str, Any]]:
    solver = ConstraintSolver(
        page=page,
        timeout_ms=timeout_ms,
        diagnostics_dir=Path("crawl_diagnostics"),
        learning_store={},
        signature_repeat_max=10,
        never_submit=True,
    )

    discovered: Dict[str, QuestionExtract] = {}
    discovered_order: List[str] = []
    visited_sections: set[str] = set()
    explored_edges: set[Tuple[str, str, str, str]] = set()  # (source_sig, question_label, option_text, target_sig)
    explored_nav_edges: set[Tuple[str, str]] = set()  # (source_sig, target_sig)
    probe_cache: Dict[Tuple[str, str, str], Optional[str]] = {}  # (source_sig, question_label, option_text) -> target_sig
    section_blocks_cache: Dict[str, List[BlockInfo]] = {}  # sig -> cached BlockInfo (without locators)
    default_forward_cache: Dict[str, str] = {}  # sig -> next_sig via default Next
    non_branching_sections: set[str] = set()
    section_visit_count: Dict[str, int] = {}

    def merge_question(
        label: str,
        help_text: str,
        required: bool,
        kind: str,
        options: List[str],
        allow_other: bool,
        grid: Optional[GridExtract],
        unsupported: bool,
        err: str,
        transitions: Optional[Dict[str, str]] = None,
        input_attrs: Dict[str, str] = {},
    ) -> None:
        key = normalize_question_identity(label)
        t_guess, unsupported_flag = kind_to_type_guess(kind, options, grid, allow_other)
        unsupported = bool(unsupported or unsupported_flag)

        if key not in discovered:
            discovered[key] = QuestionExtract(
                index=0,
                label_text=label,
                help_text=help_text or "",
                required=required,
                type_guess=t_guess,
                options=list(options),
                allow_other=bool(allow_other),
                grid=grid,
                transitions=dict(transitions or {}),
                unsupported=unsupported,
                error=err or None,
                input_attrs=input_attrs,
            )
            discovered_order.append(key)
            return True  # New

        dst = discovered[key]
        dst.required = bool(dst.required or required)
        if dst.type_guess == "unknown" and t_guess != "unknown":
            dst.type_guess = t_guess
            return True
        if not dst.help_text and help_text:
            dst.help_text = help_text
            return True
        dst.allow_other = bool(dst.allow_other or allow_other)
        dst.unsupported = bool(dst.unsupported or unsupported)

        s = set(dst.options)
        added = False
        for o in options:
            if o not in s:
                dst.options.append(o)
                s.add(o)
                added = True

        if grid and not dst.grid:
            dst.grid = grid
            return True

        added_trans = False
        for k, v in (transitions or {}).items():
            if k not in dst.transitions:
                dst.transitions[k] = v
                added_trans = True

        if not dst.input_attrs and input_attrs:
            dst.input_attrs = input_attrs
            return True

        return added or added_trans

    def capture_section_questions() -> List[BlockInfo]:
        sig = solver.section_signature()
        if sig in section_blocks_cache:
            cached = section_blocks_cache[sig]
            made_progress = False
            for b in cached:
                opts: List[str] = []
                allow_other = bool(b.allow_other)
                if b.kind in {"radio", "checkbox"}:
                    opts = list(b.options or [])
                elif b.kind == "dropdown":
                    opts, allow_other_dd = extract_dropdown_options_for_wizard(solver, b.block)
                    allow_other = allow_other or allow_other_dd
                if merge_question(
                    label=b.label_text,
                    help_text=b.help_text,
                    required=b.required,
                    kind=b.kind,
                    options=opts,
                    allow_other=allow_other,
                    grid=b.grid,
                    unsupported=(b.kind == "file_upload"),
                    err=b.error_text,
                    input_attrs=b.input_attrs,
                ):
                    made_progress = True
            if made_progress:
                return solver.visible_blocks(timeout_ms=min(timeout_ms, 2500))  # Refresh if progress
            return [BlockInfo(block=None, **vars(b)) for b in cached]  # Return copy without locators
        else:
            blocks = solver.visible_blocks(timeout_ms=min(timeout_ms, 2500))
            if not blocks:
                t0 = time.perf_counter()
                while (time.perf_counter() - t0) * 1000.0 < 2000:
                    try:
                        solver.page.wait_for_timeout(200)
                    except Exception:
                        break
                    blocks = solver.visible_blocks(timeout_ms=min(timeout_ms, 2500))
                    if blocks:
                        break
            # Cache copy without locators
            cached_blocks = []
            for b in blocks:
                bi_copy = BlockInfo(
                    block=None,  # No locator in cache
                    label_text=b.label_text,
                    help_text=b.help_text,
                    kind=b.kind,
                    required=b.required,
                    error_text=b.error_text,
                    options=b.options,
                    allow_other=b.allow_other,
                    grid=b.grid,
                    input_attrs=b.input_attrs,
                )
                cached_blocks.append(bi_copy)
                opts: List[str] = []
                allow_other = bool(b.allow_other)
                if b.kind in {"radio", "checkbox"}:
                    opts = list(b.options or [])
                elif b.kind == "dropdown":
                    opts, allow_other_dd = extract_dropdown_options_for_wizard(solver, b.block)
                    allow_other = allow_other or allow_other_dd
                merge_question(
                    label=b.label_text,
                    help_text=b.help_text,
                    required=b.required,
                    kind=b.kind,
                    options=opts,
                    allow_other=allow_other,
                    grid=b.grid,
                    unsupported=(b.kind == "file_upload"),
                    err=b.error_text,
                    input_attrs=b.input_attrs,
                )
            section_blocks_cache[sig] = cached_blocks
            return blocks

    def select_option(block: BlockInfo, opt: str) -> None:
        try:
            if block.kind == "radio":
                r = block.block.get_by_role("radio", name=opt, exact=True)
                if r.count() > 0 and r.first.is_visible():
                    r.first.click(timeout=min(timeout_ms, 1500))
                    return
                radios = block.block.locator("[role='radio']")
                if radios.count() > 0:
                    radios.first.click(timeout=min(timeout_ms, 1500))
                    return
            if block.kind == "dropdown":
                dd = block.block.locator("[role='listbox'], [role='combobox']").first
                if dd.count() > 0 and dd.is_visible():
                    dd.click(timeout=min(timeout_ms, 1500))
                    cand = page.get_by_role("option", name=opt, exact=True)
                    if cand.count() > 0 and cand.first.is_visible():
                        cand.first.click(timeout=min(timeout_ms, 1500))
                    else:
                        opts = page.locator("[role='option']")
                        if opts.count() > 0:
                            opts.first.click(timeout=min(timeout_ms, 1500))
                    try:
                        page.keyboard.press("Escape")
                    except:
                        pass
                    return
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return

    def probe_outcome(cand_label: str, cand_kind: str, opt: str) -> Optional[str]:
        solver.back_until_label_visible(cand_label, max_steps=0)
        sig_before = solver.require_non_empty_signature("pre_probe")
        cache_key = (sig_before, normalize_question_identity(cand_label), opt)
        if cache_key in probe_cache:
            return probe_cache[cache_key]

        blocks = solver.visible_blocks(timeout_ms=min(timeout_ms, 2500))
        cand = next((b for b in blocks if normalize_question_identity(b.label_text) == normalize_question_identity(cand_label)), None)
        if cand is None:
            probe_cache[cache_key] = None
            return None

        select_option(cand, opt)

        if not solver.is_next_visible():
            probe_cache[cache_key] = None
            return None
        next_sig = solver.click_next_with_solver(max_repairs=2)
        probe_cache[cache_key] = next_sig
        return next_sig

    def safe_back_to(label: str) -> None:
        try:
            if solver.find_nav_button("Back") or solver.find_nav_button("Previous"):
                solver.click_back_with_solver()
        except Exception:
            return
        solver.back_until_label_visible(label, max_steps=8)

    def dfs() -> None:
        sig = solver.require_non_empty_signature("crawl_entry", timeout_ms=8000)
        solver.note_signature(sig)
        section_visit_count[sig] = section_visit_count.get(sig, 0) + 1
        if section_visit_count[sig] > 3:
            return
        if sig in visited_sections:
            return
        visited_sections.add(sig)
        print(f"CRAWL: entered section {sig}")
        if sig == "__TERMINAL_SUBMIT__":
            return
        made_progress = False
        blocks = capture_section_questions()
        if blocks:  # If blocks captured, assume progress
            made_progress = True

        if sig in non_branching_sections:
            # Skip probing if known non-branching
            pass
        else:
            eligible: List[Tuple[BlockInfo, List[str]]] = []
            for b in blocks:
                if b.kind == "radio" or b.kind == "dropdown":
                    opts = list(b.options or [])
                    if b.kind == "dropdown":
                        opts, _allow_other = extract_dropdown_options_for_wizard(solver, b.block)
                    if 2 <= len(opts) <= 8:
                        eligible.append((b, opts))

            has_branching = False
            for cand, opts in eligible:
                if len(opts) < 2:
                    continue

                transitions: Dict[str, str] = {}
                probe_count = 0
                max_probes = len(opts) + 2  # Bounded

                sig_a = probe_outcome(cand.label_text, cand.kind, opts[0])
                if sig_a:
                    transitions[opts[0]] = sig_a
                    probe_count += 1
                    made_progress = True
                safe_back_to(cand.label_text)

                sig_b = probe_outcome(cand.label_text, cand.kind, opts[1])
                if sig_b:
                    transitions[opts[1]] = sig_b
                    probe_count += 1
                    made_progress = True
                safe_back_to(cand.label_text)

                if sig_a == sig_b and sig_a is not None:
                    # Converge; assume all
                    for opt in opts[2:]:
                        transitions[opt] = sig_a
                else:
                    unique = {x for x in transitions.values() if x}
                    if len(unique) > 1:
                        has_branching = True
                        for opt in opts[2:]:
                            if probe_count >= max_probes:
                                break
                            next_sig = probe_outcome(cand.label_text, cand.kind, opt)
                            if next_sig:
                                transitions[opt] = next_sig
                                probe_count += 1
                                made_progress = True
                            safe_back_to(cand.label_text)

                if merge_question(
                    label=cand.label_text,
                    help_text=cand.help_text,
                    required=cand.required,
                    kind=cand.kind,
                    options=opts,
                    allow_other=cand.allow_other,
                    grid=cand.grid,
                    unsupported=(cand.kind == "file_upload"),
                    err=cand.error_text,
                    transitions=transitions,
                    input_attrs=cand.input_attrs,
                ):
                    made_progress = True

                for opt, next_sig in transitions.items():
                    if not next_sig:
                        continue
                    if next_sig in visited_sections:
                        continue
                    edge = (sig, normalize_question_identity(cand.label_text), opt, next_sig)
                    if edge in explored_edges:
                        continue
                    explored_edges.add(edge)
                    explored_nav_edges.add((sig, next_sig))
                    solver.back_until_label_visible(cand.label_text, max_steps=0)
                    blocks_now = solver.visible_blocks(timeout_ms=min(timeout_ms, 2500))
                    cand_now = next(
                        (x for x in blocks_now if normalize_question_identity(x.label_text) == normalize_question_identity(cand.label_text)),
                        None,
                    )
                    if cand_now is None or (not solver.is_next_visible()):
                        continue
                    select_option(cand_now, opt)
                    moved_sig = solver.click_next_with_solver(max_repairs=2)
                    if moved_sig != next_sig:
                        next_sig = moved_sig
                    if next_sig == "__TERMINAL_SUBMIT__":
                        safe_back_to(cand.label_text)
                        continue
                    dfs()
                    safe_back_to(cand.label_text)

            if not has_branching:
                non_branching_sections.add(sig)

        if not made_progress and section_visit_count[sig] > 1:
            return  # No new info on revisit

        if solver.is_next_visible():
            if sig in default_forward_cache:
                next_sig = default_forward_cache[sig]
                if (sig, next_sig) in explored_nav_edges:
                    if next_sig not in visited_sections:
                        # Physical to explore new
                        solver.click_next_with_solver(max_repairs=2)
                        dfs()
                        solver.click_back_with_solver()
                    # Else already explored, skip
                else:
                    # Known but not explored edge? Physical
                    solver.click_next_with_solver(max_repairs=2)
                    dfs()
                    solver.click_back_with_solver()
                    explored_nav_edges.add((sig, next_sig))
            else:
                # Unknown forward, physical and cache
                next_sig = solver.click_next_with_solver(max_repairs=2)
                default_forward_cache[sig] = next_sig
                explored_nav_edges.add((sig, next_sig))
                dfs()
                solver.click_back_with_solver()

    dfs()

    out: List[QuestionExtract] = []
    for idx, key in enumerate(discovered_order, start=1):
        q = discovered[key]
        q.index = idx
        out.append(q)
    return out, solver.learning_store


# -----------------------------
# Semantic inference + default generation (Wizard V2)
# -----------------------------


def _contains_any(haystack: str, keywords: Sequence[str]) -> bool:
    low = (haystack or "").lower()
    return any(k in low for k in keywords)


def _options_look_like_numeric_ranges(options: Sequence[str]) -> bool:
    if not options:
        return False
    for opt in options:
        s = (opt or "").strip()
        if not s:
            continue
        low = s.lower()
        if re.search(r"\d+\s*[-–—]\s*\d+", s):
            return True
        if re.search(r"\d+\s*(?:to)\s*\d+", low):
            return True
        if re.search(r"\d+\s*\+\s*$", low) or low.endswith("+"):
            return True
        if re.search(r"\b(under|below|less than|upto|up to|over|above|more than|at least|at most)\b", low) and re.search(r"\d", low):
            return True
    return False


def infer_semantic_key(label: str, help_text: str, options: Sequence[str], field_type: str) -> str:
    text = normalize_button_label(label)
    help_t = normalize_button_label(help_text)
    low = f"{text} {help_t}".strip().lower()
    opts_low = " ".join((o or "").lower() for o in (options or []))

    if re.search(r"\b(first name)\b", low):
        return "person.name_first"
    if re.search(r"\b(last name|surname)\b", low):
        return "person.name_last"
    if re.search(r"\b(full name)\b", low) or re.fullmatch(r"name", low.strip()) or low.endswith(" name"):
        return "person.name_full"

    if ("confirm email" in low) or ("re-enter email" in low) or ("reenter email" in low) or ("verify email" in low):
        return "person.email_confirm"
    if "email" in low:
        return "person.email"

    if _contains_any(low, ["phone", "mobile", "whatsapp"]) and "code" not in low:
        return "person.phone"

    if _contains_any(low, ["pincode", "pin code", "zip", "zipcode"]):
        return "person.pincode"

    if ("gender" in low) or re.search(r"\bsex\b", low) or re.search(r"\b(male|female)\b", opts_low):
        return "person.gender"

    if ("age band" in low) or ("age group" in low) or (_options_look_like_numeric_ranges(options) and re.search(r"\b(18|21|24|25|30|35|40|45|50|55|60|65)\b", opts_low)):
        return "person.age_band"

    if "age" in low and field_type in {"text", "paragraph", "textarea"}:
        return "person.age"

    if ("city" in low) and ("state" in low):
        return "person.city_state"

    if _contains_any(low, ["company", "organisation", "organization", "org"]):
        return "org.company"

    if _contains_any(low, ["designation", "title"]):
        return "org.title"

    if _contains_any(low, ["budget", "amount", "spend", "income", "salary", "cost", "price", "monthly spent", "monthly spend", "spent"]):
        return "finance.amount"

    if _contains_any(low, ["message", "comments", "comment", "notes", "note"]):
        return "freeform.message"

    if _contains_any(low, ["quantity", "qty", "count", "units"]):
        return "commerce.quantity"

    if _contains_any(low, ["website", "url", "link", "http"]):
        return "web.url"

    if _contains_any(low, ["otp", "one-time", "verification code", "auth code"]):
        return "security.otp"

    return "unknown"


def semantic_key_to_legacy(semantic_key: str) -> str:
    """
    Legacy semantic_type mapping used by older runners/configs.
    """
    mapping = {
        "person.name_full": "name",
        "person.name_first": "name",
        "person.name_last": "name",
        "person.gender": "gender",
        "person.email": "email",
        "person.email_confirm": "email",
        "person.phone": "phone",
        "person.age": "age_band",
        "person.age_band": "age_band",
        "person.city_state": "city_state",
        "person.pincode": "unknown",
        "org.company": "unknown",
        "org.title": "occupation",
        "finance.amount": "budget_amount",
        "freeform.message": "unknown",
        "commerce.quantity": "unknown",
        "web.url": "unknown",
        "security.otp": "unknown",
        "unknown": "unknown",
    }
    return mapping.get(semantic_key, "unknown")


def default_generation_for_field(
    semantic_key: str,
    field_type: str,
    required: bool,
    options: Sequence[str],
    allow_other: bool,
    grid: Optional[GridExtract],
    unsupported: bool,
) -> Dict[str, Any]:
    """
    Wizard V2 default generation schema:
      generation: { mode: PERSONA|RANGE|PATTERN|STATIC|WEIGHTED|SKIP, spec: {...} }

    Notes:
      - For option questions, default is WEIGHTED with descending weights.
      - For checkboxes, defaults: min_select=1 if required else 0; and max_select=2 default.
      - For person.* and org.* fields, default is PERSONA(field=...).
      - For numeric-like semantics, default is RANGE(min,max,integer).
      - For paragraph/message, default is STATIC realistic placeholder.
    """
    if unsupported:
        return {"mode": "SKIP", "spec": {"reason": "unsupported"}}

    persona_map = {
        "person.name_full": "name_full",
        "person.name_first": "name_first",
        "person.name_last": "name_last",
        "person.gender": "gender",
        "person.email": "email",
        "person.email_confirm": "email",
        "person.phone": "phone",
        "person.location_city_state": "city_state",
        "org.company": "company",
        "org.title": "title",
    }

    if semantic_key == "person.gender":
        return {"mode": "PERSONA", "spec": {"field": "gender"}}

    if semantic_key in persona_map:
        return {"mode": "PERSONA", "spec": {"field": persona_map[semantic_key]}}

    if semantic_key == "person.age":
        return {"mode": "RANGE", "spec": {"min": 18, "max": 60, "integer": True}}
    if semantic_key == "person.pincode":
        return {"mode": "RANGE", "spec": {"min": 100000, "max": 999999, "integer": True, "allow_leading_zero": False}}
    if semantic_key == "finance.amount":
        return {"mode": "RANGE", "spec": {"min": 100, "max": 250000, "integer": True}}
    if semantic_key == "commerce.quantity":
        return {"mode": "RANGE", "spec": {"min": 1, "max": 99, "integer": True}}
    if semantic_key == "web.url":
        return {"mode": "PATTERN", "spec": {"template": "https://example.com/item/{rand:6}?ref={rand:4}"}}
    if semantic_key == "security.otp":
        return {"mode": "RANGE", "spec": {"digits": 6, "integer": True, "allow_leading_zero": True}}
    if semantic_key == "person.phone":
        return {"mode": "RANGE", "spec": {"digits": 10, "integer": True, "allow_leading_zero": False}}

    if field_type in {"paragraph", "textarea"} or semantic_key == "freeform.message":
        return {"mode": "STATIC", "spec": {"value": "Interested in learning more. Thank you!"}}

    if field_type in {"date"}:
        return {"mode": "STATIC", "spec": {"value": Gen.date_mmddyyyy()}}
    if field_type in {"time"}:
        return {"mode": "STATIC", "spec": {"value": Gen.time_hhmm()}}

    if field_type in {"mc_grid", "checkbox_grid"} and grid:
        col_weights = [{"value": c, "weight": float(len(grid.columns) - i)} for i, c in enumerate(grid.columns)]
        if grid.kind == "mc":
            return {
                "mode": "WEIGHTED",
                "spec": {
                    "grid": True,
                    "strategy": "per_row",
                    "choices": col_weights,
                    "required_per_row": bool(grid.required_per_row),
                },
            }
        max_sel = min(2, len(grid.columns)) if grid.columns else 2
        min_sel = 1 if grid.required_per_row else 0
        return {
            "mode": "WEIGHTED",
            "spec": {
                "grid": True,
                "strategy": "per_row_multi",
                "choices": col_weights,
                "required_per_row": bool(grid.required_per_row),
                "min_select": min_sel,
                "max_select": max_sel,
            },
        }

    if field_type in {"radio", "dropdown", "scale", "checkbox"} and options:
        weights = list(range(len(options), 0, -1))
        choices = [{"value": o, "weight": float(w)} for o, w in zip(options, weights)]
        if field_type == "checkbox":
            min_sel = 1 if required else 0
            max_sel = min(2, len(choices)) if choices else 2
            return {
                "mode": "WEIGHTED",
                "spec": {
                    "choices": choices,
                    "multi": True,
                    "min_select": min_sel,
                    "max_select": max_sel,
                    "allow_other": bool(allow_other),
                },
            }
        return {
            "mode": "WEIGHTED",
            "spec": {
                "choices": choices,
                "multi": False,
                "min_select": 1,
                "max_select": 1,
                "allow_other": bool(allow_other),
            },
        }

    if semantic_key == "unknown" and field_type in {"text", "paragraph", "textarea"}:
        return {"mode": "STATIC", "spec": {"value": Gen.text()}}

    return {"mode": "PERSONA", "spec": {"field": "name_full"}}


# -----------------------------
# Interactive config builder
# -----------------------------


def prompt_persona_tuning() -> Dict[str, Any]:
    print("\n=== Global persona tuning (used by PERSONA/PATTERN) ===")
    email_domain = prompt_text("Email domain", default="gmail.com")

    lp = prompt_choice(
        "Email local-part pattern",
        choices=[("1", "firstlast"), ("2", "first.last"), ("3", "first_last")],
        default_key="2",
    )
    lp_map = {"1": "firstlast", "2": "first.last", "3": "first_last"}
    local_part_pattern = lp_map[lp]

    suffix_digits = prompt_int("Email suffix digits count", default=3, min_value=0, max_value=12)

    print("\nAge band weights (sum ~1.0; exact sum not required)")
    age_weights = {
        "18-22": prompt_float("  18-22", default=0.20, min_value=0.0),
        "23-30": prompt_float("  23-30", default=0.35, min_value=0.0),
        "31-45": prompt_float("  31-45", default=0.30, min_value=0.0),
        "46-60": prompt_float("  46-60", default=0.15, min_value=0.0),
    }

    print("\nGender weights (sum ~1.0; exact sum not required)")
    gender_weights = {
        "Male": prompt_float("  Male", default=0.49, min_value=0.0),
        "Female": prompt_float("  Female", default=0.49, min_value=0.0),
        "Other": prompt_float("  Other", default=0.02, min_value=0.0),
    }

    default_cities = "Mumbai, Delhi, Bengaluru, Hyderabad, Chennai, Pune, Kolkata"
    city_pool_raw = prompt_text("City pool (comma-separated)", default=default_cities)
    city_pool = [c.strip() for c in city_pool_raw.split(",") if c.strip()] or [c.strip() for c in default_cities.split(",")]

    return {
        "email_domain": email_domain,
        "email_local_part_pattern": local_part_pattern,
        "email_suffix_digits": suffix_digits,
        "age_band_weights": age_weights,
        "gender_weights": gender_weights,
        "city_pool": city_pool,
    }


def prompt_success_settings() -> Dict[str, Any]:
    print("\n=== Success verification settings ===")
    mode = prompt_choice(
        "Choose success verification method",
        choices=[("1", "success_text_contains (recommended)"), ("2", "success_selector (advanced)")],
        default_key="1",
    )
    if mode == "2":
        sel = prompt_text("CSS/XPath selector that indicates success", default="")
        if sel:
            return {"success_selector": sel}
        print("  Empty selector; using success_text_contains.")
    txt = prompt_text("Success text must contain", default="Your response has been recorded")
    return {"success_text_contains": txt}


def prompt_navigation_settings() -> Dict[str, Any]:
    print("\n=== Navigation settings ===")
    next_text = prompt_text("Next button text", default="Next")
    submit_text = prompt_text("Submit button text", default="Submit")
    return {"next_button_text": next_text, "submit_button_text": submit_text}


def prompt_weight_setup(options: List[str]) -> List[Dict[str, Any]]:
    if not options:
        raw = prompt_text("  No options detected. Enter options (comma-separated)", default="")
        options = [x.strip() for x in raw.split(",") if x.strip()]
    if not options:
        return []

    method = prompt_choice(
        "Weighted setup method",
        choices=[("1", "mild skew (top options higher)"), ("2", "custom (enter weights)"), ("3", "uniform (warn)")],
        default_key="1",
    )

    if method == "1":
        weights = list(range(len(options), 0, -1))
    elif method == "3":
        print("  Warning: uniform weights reduce variability.")
        weights = [1] * len(options)
    else:
        weights = []
        for idx, opt in enumerate(options, start=1):
            w = prompt_float(f"  Weight for [{idx}] {opt}", default=max(1.0, float(len(options) - idx + 1)), min_value=0.0)
            weights.append(w)

    return [{"value": o, "weight": float(w)} for o, w in zip(options, weights)]


def prompt_generation_override(default_generation: Dict[str, Any], q: QuestionExtract, field_type: str) -> Dict[str, Any]:
    """
    Wizard V2 generation modes:
      PERSONA(field=...)
      RANGE(min,max,integer)
      PATTERN(template)
      STATIC(value)
      WEIGHTED(choices, ...)

    This prompt allows overriding the inferred default.
    """
    print(f"  default generation: {default_generation}")
    override = prompt_choice(
        "Override generation?",
        choices=[("1", "keep default"), ("2", "override")],
        default_key="1",
    )
    if override == "1":
        return default_generation

    mode_key = prompt_choice(
        "Choose generation mode",
        choices=[
            ("1", "PERSONA"),
            ("2", "WEIGHTED (options)"),
            ("3", "RANGE (numbers)"),
            ("4", "PATTERN (template string)"),
            ("5", "STATIC (constant)"),
            ("6", "SKIP (only if optional/unsupported)"),
        ],
        default_key={
            "PERSONA": "1",
            "WEIGHTED": "2",
            "RANGE": "3",
            "PATTERN": "4",
            "STATIC": "5",
            "SKIP": "6",
        }.get((default_generation or {}).get("mode", "PERSONA"), "1"),
    )
    mode = {"1": "PERSONA", "2": "WEIGHTED", "3": "RANGE", "4": "PATTERN", "5": "STATIC", "6": "SKIP"}[mode_key]

    if mode == "SKIP" and q.required and not q.unsupported:
        print("  Appears required; SKIP not recommended.")
        return default_generation

    if mode == "PERSONA":
        suggested = (default_generation.get("spec") or {}).get("field", "name_full")
        raw = prompt_text("Persona field", default=str(suggested))
        return {"mode": "PERSONA", "spec": {"field": raw}}

    if mode == "WEIGHTED":
        if field_type in ("mc_grid", "checkbox_grid") and q.grid and q.grid.columns:
            base_options = list(q.grid.columns)
        else:
            base_options = list(q.options or [])
        choices = prompt_weight_setup(base_options)
        spec: Dict[str, Any] = {"choices": choices}
        if field_type in ("mc_grid", "checkbox_grid"):
            spec["grid"] = True
            if field_type == "mc_grid":
                spec["strategy"] = "per_row"
                spec["required_per_row"] = bool(q.grid.required_per_row) if q.grid else False
                spec["multi"] = False
                spec["min_select"] = 1
                spec["max_select"] = 1
            elif field_type == "checkbox_grid":
                spec["strategy"] = "per_row_multi"
                spec["required_per_row"] = bool(q.grid.required_per_row) if q.grid else False
                min_sel = 1 if (q.grid and q.grid.required_per_row) else 0
                max_sel = min(2, len(choices)) if choices else 2
                spec["multi"] = True
                spec["min_select"] = min_sel
                spec["max_select"] = max_sel
            spec["allow_other"] = False
        else:
            if field_type == "checkbox":
                min_sel = prompt_int("Min selections", default=(1 if q.required else 0), min_value=0, max_value=max(0, len(choices)))
                max_sel = prompt_int("Max selections", default=min(2, max(1, min_sel + 1)), min_value=min_sel, max_value=max(1, len(choices)))
                spec.update({"multi": True, "min_select": min_sel, "max_select": max_sel, "allow_other": bool(q.allow_other)})
            else:
                spec.update({"multi": False, "min_select": 1, "max_select": 1, "allow_other": bool(q.allow_other)})
        return {"mode": "WEIGHTED", "spec": spec}

    if mode == "RANGE":
        dspec = default_generation.get("spec") or {}
        mn = prompt_int("Min", default=int(dspec.get("min", 0)))
        mx = prompt_int("Max", default=int(dspec.get("max", mn + 10)), min_value=mn)
        integer = prompt_choice("Integer?", choices=[("1", "true"), ("2", "false")], default_key="1") == "1"
        return {"mode": "RANGE", "spec": {"min": mn, "max": mx, "integer": integer}}

    if mode == "PATTERN":
        print("  Example placeholders: {persona.name_full} {persona.email} {persona.gender} {persona.city_state}")
        tpl = prompt_text("Template string", default="{persona.name_full}")
        return {"mode": "PATTERN", "spec": {"template": tpl}}

    if mode == "STATIC":
        lit = prompt_text("Literal value", default=(default_generation.get("spec") or {}).get("value", ""))
        return {"mode": "STATIC", "spec": {"value": lit}}

    return {"mode": "SKIP", "spec": {}}


def interactive_build_config(
    form_name: str,
    form_url: str,
    questions: List[QuestionExtract],
    learned_constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Schema notes (Wizard V2):
      - Each field now includes:
          field_key: stable, deterministic key derived from label (unique within config)
          label/help_text
          semantic_key (V2), semantic_type (legacy alias)
          generation: V2 generator spec (mode/spec)
          options (where applicable), allow_other
          grid {rows, columns, kind, required_per_row} for mc_grid/checkbox_grid
          transitions {option_text: section_signature} when discovered in crawl
          unsupported: true for file_upload (runner handles)
      - Legacy keys kept:
          key (alias), label_text (alias)
    """
    print("\n============================================================")
    print("Interactive config builder (Wizard V2)")
    print("Tips:")
    print("- Press Enter to accept defaults.")
    print("- Runner uses ConstraintSolver to repair many validation issues.")
    print("============================================================")

    persona_tuning = prompt_persona_tuning()
    success = prompt_success_settings()
    navigation = prompt_navigation_settings()

    used_field_keys: set[str] = set()
    used_alias_keys: set[str] = set()
    fields: List[Dict[str, Any]] = []

    for q in questions:
        print("\n------------------------------------------------------------")
        print(f"Q{q.index}: {q.label_text}")
        if q.help_text:
            print(f"  help: {preview(q.help_text, 100)}")
        print(f"  required: {q.required}")
        print(f"  type guess: {q.type_guess}")
        if q.options:
            print(f"  options: {', '.join(q.options[:10])}{' …' if len(q.options) > 10 else ''}")
        if q.grid:
            print(f"  grid: rows={len(q.grid.rows)} cols={len(q.grid.columns)} kind={q.grid.kind} required_per_row={q.grid.required_per_row}")
        if q.transitions:
            print(f"  transitions: {q.transitions}")
        if q.unsupported:
            print("  note: unsupported type (runner will handle)")

        default_type = q.type_guess if q.type_guess in FIELD_TYPES else "unknown"
        type_choice = prompt_text(f"Confirm/override type {FIELD_TYPES}", default=default_type)
        if type_choice not in FIELD_TYPES:
            print("  Unknown type; using 'unknown'.")
            type_choice = "unknown"
        if type_choice == "textarea":
            type_choice = "paragraph"

        base_field_key = normalize_label_to_key(q.label_text) or f"q{q.index}"
        field_key = ensure_unique_key(base_field_key, used_field_keys)

        suggested_alias = field_key
        raw_alias = prompt_text("Alias key (legacy key; you can rename)", default=suggested_alias)
        raw_alias = normalize_label_to_key(raw_alias) or raw_alias.strip() or suggested_alias
        alias_key = ensure_unique_key(raw_alias, used_alias_keys)

        semantic_key = infer_semantic_key(q.label_text, q.help_text, q.options, type_choice)
        legacy_semantic = semantic_key_to_legacy(semantic_key)

        constraints: Dict[str, Any] = {}
        raw_attrs = q.input_attrs
        low = f"{q.label_text} {q.help_text}".strip().lower()
        if raw_attrs:
            constraints["source"] = "dom"
            for attr in ["min", "max"]:
                if attr in raw_attrs and raw_attrs[attr].isdigit():
                    constraints[attr] = int(raw_attrs[attr])
            if "minlength" in raw_attrs and "maxlength" in raw_attrs and raw_attrs["minlength"] == raw_attrs["maxlength"]:
                try:
                    d = int(raw_attrs["minlength"])
                    if d > 0 and d <= 15:
                        constraints["digits"] = d
                except:
                    pass
            elif "maxlength" in raw_attrs:
                try:
                    d = int(raw_attrs["maxlength"])
                    if d > 0 and d <= 15 and _contains_any(low, ["pin", "pincode", "zip", "otp", "code", "verification", "phone", "digits"]):
                        constraints["digits"] = d
                except:
                    pass
            if "pattern" in raw_attrs:
                constraints["pattern_hint"] = raw_attrs["pattern"]
                pat_low = raw_attrs["pattern"].lower()
                if "." in pat_low or "decimal" in pat_low:
                    pass
                else:
                    constraints["integer"] = True
            if "inputmode" in raw_attrs and raw_attrs["inputmode"] == "numeric":
                constraints["integer"] = True
            if "step" in raw_attrs:
                step = raw_attrs["step"]
                if step != "any":
                    try:
                        s = float(step)
                        if not s.is_integer():
                            if "integer" in constraints:
                                del constraints["integer"]
                    except:
                        pass
            if "type" in raw_attrs and raw_attrs["type"] == "number":
                if "integer" not in constraints:
                    constraints["integer"] = True
        else:
            constraints["source"] = "inferred"

        generation_default = default_generation_for_field(
            semantic_key=semantic_key,
            field_type=type_choice,
            required=bool(q.required),
            options=q.options,
            allow_other=bool(q.allow_other),
            grid=q.grid,
            unsupported=bool(q.unsupported),
        )

        if generation_default["mode"] == "RANGE":
            spec = generation_default["spec"]
            for k in ["min", "max", "digits", "integer"]:
                if k in constraints:
                    spec[k] = constraints[k]
            if "digits" in spec:
                allow_lz = False
                if "otp" in semantic_key or "code" in semantic_key:
                    allow_lz = True
                spec["allow_leading_zero"] = allow_lz
                if semantic_key == "person.pincode" and "min" not in spec and "max" not in spec:
                    if spec["digits"] == 6:
                        spec["min"] = 100000
                        spec["max"] = 999999
                elif semantic_key == "person.phone" and "min" not in spec and "max" not in spec:
                    if spec["digits"] == 10:
                        spec["min"] = 1000000000
                        spec["max"] = 9999999999
            if "min" not in spec and "max" in spec:
                spec["min"] = 1 if semantic_key in ["commerce.quantity", "finance.amount"] else 0
            if "max" not in spec and "min" in spec:
                spec["max"] = spec["min"] + 1000
                constraints["source"] = "partial"
            if "integer" not in spec:
                spec["integer"] = True if "digits" in spec or semantic_key in ["person.age", "commerce.quantity"] else False

        print(f"  inferred constraints: {constraints}")
        generation = prompt_generation_override(generation_default, q=q, field_type=type_choice)

        multi_select: Optional[Dict[str, Any]] = None
        if type_choice == "checkbox":
            min_sel = 1 if q.required else 0
            max_sel = min(2, len(q.options)) if q.options else 2
            multi_select = {"min_select": min_sel, "max_select": max_sel}

        field_obj: Dict[str, Any] = {
            "field_key": field_key,
            "label": q.label_text,
            "help_text": q.help_text or "",
            "type": type_choice,
            "required": bool(q.required),
            "semantic_key": semantic_key,
            "generation": generation,
            "options": list(q.options) if q.options else [],
            "allow_other": bool(q.allow_other),
            "transitions": dict(q.transitions) if q.transitions else {},
            "unsupported": bool(q.unsupported),
            "constraints": constraints,
            "key": alias_key,
            "label_text": q.label_text,
            "semantic_type": legacy_semantic,
        }

        if q.grid:
            field_obj["grid"] = {
                "rows": list(q.grid.rows),
                "columns": list(q.grid.columns),
                "kind": q.grid.kind,
                "required_per_row": bool(q.grid.required_per_row),
                "required_mode": q.grid.required_mode,
                "row_required": q.grid.row_required,
            }
        if multi_select:
            field_obj["multi_select"] = multi_select

        fields.append(field_obj)

    return {
        "form_name": form_name,
        "form_url": form_url,
        "success": success,
        "navigation": navigation,
        "persona_tuning": persona_tuning,
        "learned_constraints": learned_constraints or {},
        "fields": fields,
        "wizard_version": 2,
    }


# -----------------------------
# Main build flow
# -----------------------------


def print_summary(questions: List[QuestionExtract]) -> None:
    print("\n=== Extracted questions summary ===")
    for q in questions:
        opts = f" | options: {preview(', '.join(q.options), 60)}" if q.options else ""
        note = f" | note: {preview(q.error, 60)}" if q.error else ""
        req = "required" if q.required else "optional"
        extra = ""
        if q.grid:
            extra = f" | grid: {len(q.grid.rows)}x{len(q.grid.columns)} {q.grid.kind}"
        if q.transitions:
            extra += " | transitions"
        if q.unsupported:
            extra += " | unsupported"
        print(f"[{q.index:02d}] ({req}) {q.type_guess:12s} | {preview(q.label_text, 70)}{opts}{extra}{note}")


def build(args: argparse.Namespace) -> int:
    form_url: str = args.url
    out_path = Path(args.out)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(args.timeout)

        try:
            print(f"Opening: {form_url}")
            try:
                page.goto(form_url, wait_until="domcontentloaded", timeout=args.timeout)
            except PlaywrightTimeoutError:
                print("Warning: navigation timed out at domcontentloaded; continuing with best effort.")

            try:
                page.locator("div[role='listitem']").first.wait_for(state="visible", timeout=min(args.timeout, 5000))
            except Exception:
                pass

            try:
                form_name = normalize_button_label(page.locator("[role='heading'][aria-level='1']").first.inner_text(timeout=2000))
            except Exception:
                form_name = (page.title() or "").replace(" - Google Forms", "").strip() or "Google Form"

            learned_constraints: Dict[str, Any] = {}
            if args.crawl:
                print("\n=== Crawl mode enabled (multi-section + branching discovery) ===")
                try:
                    questions, learned_constraints = crawl_form(page, timeout_ms=args.timeout)
                except Exception:
                    traceback.print_exc()
                    print("\nCrawl crashed. Browser will remain open for inspection.")
                    input("Press Enter to close browser and exit...")
                    return 1
            else:
                solver = ConstraintSolver(page=page, timeout_ms=args.timeout, never_submit=True)
                blocks = solver.visible_blocks(timeout_ms=min(args.timeout, 3000))
                questions: List[QuestionExtract] = []
                for i, b in enumerate(blocks, start=1):
                    options: List[str] = []
                    allow_other = bool(b.allow_other)
                    if b.kind in {"radio", "checkbox"}:
                        options = list(b.options or [])
                    elif b.kind == "dropdown":
                        options, allow_other_dd = extract_dropdown_options_for_wizard(solver, b.block)
                        allow_other = allow_other or allow_other_dd
                    t_guess, unsupported = kind_to_type_guess(b.kind, options, b.grid, allow_other)
                    questions.append(
                        QuestionExtract(
                            index=i,
                            label_text=b.label_text,
                            help_text=b.help_text or "",
                            required=b.required,
                            type_guess=t_guess,
                            options=options,
                            allow_other=allow_other,
                            grid=b.grid,
                            transitions={},
                            unsupported=unsupported,
                            error=b.error_text or None,
                            input_attrs=b.input_attrs,
                        )
                    )
                learned_constraints = solver.learning_store

        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    print(f"\nForm name: {form_name}")
    print_summary(questions)

    config = interactive_build_config(
        form_name=form_name,
        form_url=form_url,
        questions=questions,
        learned_constraints=learned_constraints,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved config: {out_path}")
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Forms config wizard (Playwright, sync).")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Open form, extract questions, interactively build config JSON.")
    b.add_argument("--url", required=True, help="Google Form URL.")
    b.add_argument("--out", required=True, help="Output JSON config path.")
    b.add_argument("--headless", type=str_to_bool, default=False, help="Run browser headless (true/false). Default false.")
    b.add_argument("--timeout", type=int, default=30000, help="Playwright timeout in ms. Default 30000.")
    b.add_argument("--crawl", type=str_to_bool, default=False, help="Crawl multi-section + branching (true/false). Default false.")
    b.set_defaults(func=build)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())