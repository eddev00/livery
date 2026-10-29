"""Import mapping: the settings and rules that decide how Word blocks become headings,
paragraphs and tables (docs/MAPPING.md).

A mapping is data from a closed schema. It reaches the conversion as part of the theme:
`[source]` and `[style_map]` take the settings, `[[rule]]` the rules. Page rules are applied
to the Word file before pandoc (docx_fixups.drop_pages); every other rule is applied by the
Lua filter's first pass.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import tomllib
from typing import Any

KINDS = ("paragraph", "heading", "table", "list", "figure")
NESTED = ("unwrap_outer", "unwrap_inner")
# [settings] key -> (theme section, theme key)
SETTINGS = {
    "headings": ("style_map", None),
    "drop_styles": ("source", "drop_styles"),
    "drop_headings": ("source", "drop_headings"),
    "heading_numbers": ("source", "heading_numbers"),
    "chapter_words": ("source", "chapter_words"),
    "block_quotes": ("source", "block_quotes"),
    "outline_level_headings": ("source", "outline_level_headings"),
}
MATCH_KEYS = {"page", "kind", "style", "level", "bold", "numbered", "upper", "in_table", "contains_table",
              "before_first_heading", "text", "text_starts"}
MAX_RULES = 60
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,()'-]{0,59}$")
_EXPECT_RE = re.compile(r"^(\d{1,4})(?:(\+)|-(\d{1,4}))?$")
_PAGES_RE = re.compile(r"^(\d{1,4})-(\d{1,4})$")


class MappingError(ValueError):
    """The mapping is invalid. `problems` lists every issue found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_expect(value: str) -> tuple[int, int | None]:
    """"1" -> (1, 1), "0-2" -> (0, 2), "1+" -> (1, None)."""
    m = _EXPECT_RE.match(value)
    if not m:
        raise ValueError(f"expect: {value!r} is not like \"1\", \"0-2\" or \"1+\"")
    lo = int(m.group(1))
    if m.group(2):
        return lo, None
    return lo, int(m.group(3)) if m.group(3) else lo


def pages_of(value: Any) -> set[int]:
    """The pages a `page` match names: 2, [2, 3] or "2-4"."""
    if isinstance(value, bool):
        raise ValueError("page: a page number, a list of page numbers or a range like \"2-4\"")
    if isinstance(value, int):
        return {value}
    if isinstance(value, list) and value and all(isinstance(p, int) and not isinstance(p, bool) for p in value):
        return set(value)
    if isinstance(value, str) and (m := _PAGES_RE.match(value)) and int(m.group(1)) <= int(m.group(2)):
        return set(range(int(m.group(1)), int(m.group(2)) + 1))
    raise ValueError("page: a page number, a list of page numbers or a range like \"2-4\"")


def _strings(v: Any, max_len: int = 120) -> bool:
    items = v if isinstance(v, list) else [v]
    return bool(items) and len(items) <= 50 and all(isinstance(s, str) and 0 < len(s) <= max_len for s in items)


def _check_settings(settings: Any) -> list[str]:
    if not isinstance(settings, dict):
        return ["[settings] must be a table"]
    problems = []
    for key, v in settings.items():
        where = f"settings.{key}"
        if key not in SETTINGS:
            problems.append(f"{where}: unknown setting (allowed: {', '.join(SETTINGS)})")
        elif key == "headings":
            if not isinstance(v, dict) or not all(
                    isinstance(k, str) and 0 < len(k) <= 120 and isinstance(n, int) and not isinstance(n, bool)
                    and 0 <= n <= 6 for k, n in v.items()):
                problems.append(f"{where}: a table of Word style name = heading level (1-6, or 0 for not a heading)")
        elif key in ("drop_styles", "drop_headings", "chapter_words"):
            if not isinstance(v, list) or (v and not _strings(v)):
                problems.append(f"{where}: a list of names")
        elif key == "heading_numbers":
            if v not in ("parse", "keep"):
                problems.append(f"{where}: \"parse\" or \"keep\"")
        elif key == "block_quotes":
            if v not in ("unwrap", "keep"):
                problems.append(f"{where}: \"unwrap\" or \"keep\"")
        elif key == "outline_level_headings":
            if not isinstance(v, bool):
                problems.append(f"{where}: true or false")
    return problems


def _check_action(action: Any, match: dict[str, Any], where: str) -> list[str]:
    kind = match.get("kind")
    if action in ("drop", "keep"):
        return []
    if not isinstance(action, dict) or len(action) != 1:
        return [f"{where}.action: \"drop\", \"keep\", {{ heading = N }}, {{ heading = \"from_number\" }}, "
                "{ paragraph = true } or { nested_table = \"unwrap_outer\" | \"unwrap_inner\" }"]
    key, v = next(iter(action.items()))
    if key == "heading":
        if not (v == "from_number" or (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 6)):
            return [f"{where}.action.heading: a level 1-6 or \"from_number\""]
        if kind not in (None, "paragraph", "heading"):
            return [f"{where}.action: only paragraphs and headings can become headings"]
    elif key == "paragraph":
        if v is not True:
            return [f"{where}.action.paragraph: true"]
        if kind not in (None, "heading"):
            return [f"{where}.action: only headings can become paragraphs (add kind = \"heading\")"]
    elif key == "nested_table":
        if v not in NESTED:
            return [f"{where}.action.nested_table: {' or '.join(NESTED)}"]
        if kind != "table":
            return [f"{where}.action: nested_table needs kind = \"table\" in match"]
    else:
        return [f"{where}.action: unknown action {key!r}"]
    return []


def validate_rules(rules: Any) -> list[str]:
    if not isinstance(rules, list):
        return ["rule: must be a list of [[rule]] tables"]
    if len(rules) > MAX_RULES:
        return [f"rule: at most {MAX_RULES} rules"]
    problems: list[str] = []
    names: set[str] = set()
    for i, rule in enumerate(rules, 1):
        where = f"rule {i}"
        if not isinstance(rule, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown = set(rule) - {"name", "match", "action", "expect", "description"}
        if unknown:
            problems.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        name = rule.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            problems.append(f"{where}: needs a name (1-60 letters, digits, spaces and simple punctuation)")
        elif name in names:
            problems.append(f"{where}: the name {name!r} is used twice")
        else:
            names.add(name)
            where = f"rule {name!r}"
        match = rule.get("match")
        if not isinstance(match, dict) or not match:
            problems.append(f"{where}: needs a match table")
            continue
        for key, v in match.items():
            bad = f"{where}.match.{key}"
            if key not in MATCH_KEYS:
                problems.append(f"{bad}: unknown (allowed: {', '.join(sorted(MATCH_KEYS))})")
            elif key == "page":
                try:
                    pages = pages_of(v)
                    if not all(1 <= p <= 9999 for p in pages):
                        raise ValueError("page: pages start at 1")
                except ValueError as e:
                    problems.append(f"{where}.match.{e}")
            elif key == "kind" and v not in KINDS:
                problems.append(f"{bad}: one of {', '.join(KINDS)}")
            elif key == "level" and not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 6):
                problems.append(f"{bad}: 1-6")
            elif key in ("bold", "numbered", "upper", "in_table", "contains_table", "before_first_heading") \
                    and not isinstance(v, bool):
                problems.append(f"{bad}: true or false")
            elif key in ("style", "text", "text_starts") and not _strings(v, 200):
                problems.append(f"{bad}: a string or a list of strings")
        if "page" in match:
            if len(match) > 1:
                problems.append(f"{where}: a page rule matches on the page only (no other match keys)")
            if rule.get("action") != "drop":
                problems.append(f"{where}: a page rule can only drop (action = \"drop\")")
        else:
            problems += _check_action(rule.get("action"), match, where)
        if "expect" in rule:
            try:
                if not isinstance(rule["expect"], str):
                    raise ValueError("expect: a string like \"1\", \"0-2\" or \"1+\"")
                parse_expect(rule["expect"])
            except ValueError as e:
                problems.append(f"{where}.{e}")
    return problems


def parse(text: str) -> dict[str, Any]:
    """Parse and validate a mapping.toml; raises MappingError with every problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise MappingError([f"mapping.toml: not valid TOML: {e}"]) from e
    problems = []
    unknown = set(data) - {"version", "settings", "rule"}
    if unknown:
        problems.append(f"mapping.toml: unknown key(s) {', '.join(sorted(unknown))} (allowed: version, [settings], [[rule]])")
    if data.get("version", 1) != 1:
        problems.append("mapping.toml: version must be 1")
    problems += _check_settings(data.get("settings", {}))
    problems += validate_rules(data.get("rule", []))
    if problems:
        raise MappingError([p if p.startswith("mapping.toml") else f"mapping.toml: {p}" for p in problems])
    return data


def apply_to_theme(theme: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    """The theme the conversion reads: the mapping's settings over [source] and [style_map],
    its rules as [[rule]]. `theme` is not modified."""
    out = copy.deepcopy(theme)
    for key, value in mapping.get("settings", {}).items():
        section, theme_key = SETTINGS[key]
        if section == "style_map":
            style_map = dict(out.get("style_map", {}))
            for style, level in value.items():
                if level == 0:
                    style_map.pop(style, None)
                else:
                    style_map[style] = level
            out["style_map"] = style_map
        else:
            out.setdefault(section, {})[theme_key] = copy.deepcopy(value)
    if mapping.get("rule"):
        out["rule"] = copy.deepcopy(mapping["rule"])
    return out


def fingerprint(mapping: dict[str, Any]) -> str:
    """Identifies what a mapping does: "" for the default mapping (no settings, no rules)."""
    effective = {"settings": mapping.get("settings", {}), "rule": mapping.get("rule", [])}
    if not effective["settings"] and not effective["rule"]:
        return ""
    return hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()[:16]


def split_rules(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(page rules, block rules): page rules run on the Word file, block rules in the filter."""
    page = [r for r in rules if "page" in r.get("match", {})]
    return page, [r for r in rules if "page" not in r.get("match", {})]
