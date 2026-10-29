"""The design layer: validation, per-document resolution and the scope of a change.

A design revision is four files:

    theme.toml   the project theme (level 1)
    design.json  {"treatments": {name: {"kind", "style"}},     reusable named treatments (level 2)
                  "assignments": {"<doc>/<element>": name},
                  "overrides":   {"<doc>/<element>": style}}  element-specific overrides (level 3)
    custom.typ   optional show/set rules
    mapping.toml how Word documents are read into elements (docs/MAPPING.md); revisions made
                 before it existed have the default mapping

Everything here is data an agent may have written, so every value is checked against a
closed schema before it reaches Typst: lengths and colours by pattern, enumerations by
value, fonts against the fonts the app ships. Nothing in the design can change content; the mapping can remove blocks only on the record
(the engine's mapping_rules and mapping_text checks).
"""
from __future__ import annotations

import dataclasses
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..mapping import MappingError, fingerprint
from ..mapping import parse as parse_mapping
from .paths import NEUTRAL_DIR

DEFAULT_MAPPING = (NEUTRAL_DIR / "mapping.toml").read_text(encoding="utf-8")

KINDS = ("heading", "paragraph", "table")
KIND_PREFIX = {"heading": "h", "paragraph": "p", "table": "t"}
FONTS = ("Source Sans 3", "Libertinus Serif", "New Computer Modern", "DejaVu Sans Mono")
WEIGHTS = ("light", "regular", "medium", "semibold", "bold")
ALIGNS = ("left", "center", "right", "justify")
RULES = ("grid", "horizontal", "header", "none")

_LEN_RE = re.compile(r"^\d{1,3}(\.\d{1,3})?(pt|mm|cm|in|em)$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,47}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}/[hptlfx]\d{4,}$")

MAX_CUSTOM_BYTES = 20_000
MAX_MAPPING_BYTES = 40_000
# custom.typ may restyle; it may not read files, load code, hide content or reach inputs.
_CUSTOM_FORBIDDEN = [
    (re.compile(r"\b(read|json|toml|yaml|csv|xml|cbor|plugin|image|eval|hide)\s*\("), "file access, code loading or hiding content"),
    (re.compile(r"(^|[^\w-])(include|import)\b"), "include/import"),
    (re.compile(r"sys\s*\.\s*inputs"), "sys.inputs"),
    (re.compile(r"\bmetadata\b|<we-"), "element markers"),
    (re.compile(r"\bsize\s*:\s*0(\.\d+)?\s*(pt|em|mm|cm|in)?\b"), "zero-size text"),
]


class DesignError(ValueError):
    """A design file is invalid. `problems` lists every issue found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ------------------------------------------------------------------ value checks
def _is_len(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 400) or (
        isinstance(v, str) and bool(_LEN_RE.match(v)))


def _len_pt(v: Any, em: float = 10.5) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    num, unit = float(re.match(r"[\d.]+", v).group()), re.sub(r"[\d.]", "", v)
    return num * {"pt": 1, "mm": 2.8346, "cm": 28.346, "in": 72, "em": em}[unit]


def _is_color(v: Any, palette: dict[str, Any], allow_empty: bool = False) -> bool:
    if v == "" and allow_empty:
        return True
    return isinstance(v, str) and (bool(_HEX_RE.match(v)) or v in palette)


def _check_style(kind: str, style: Any, palette: dict[str, Any], where: str) -> list[str]:
    if not isinstance(style, dict):
        return [f"{where}: style must be an object"]
    problems: list[str] = []
    common = {"font", "size", "weight", "color", "italic", "align", "space_before", "space_after"}
    allowed = {
        "heading": common | {"upper"},
        "paragraph": common,
        "table": {"font_size", "inset", "header_fill", "header_color", "header_weight", "body_fill",
                  "stripe_fill", "stroke_color", "stroke_width", "rules", "columns", "align", "header_align"},
    }[kind]
    for key, v in style.items():
        bad = f"{where}.{key}: invalid value {v!r}"
        if key not in allowed:
            problems.append(f"{where}: unknown {kind} property {key!r} (allowed: {', '.join(sorted(allowed))})")
        elif key in ("size", "font_size"):
            if not _is_len(v) or not 6 <= _len_pt(v) <= 96:
                problems.append(f"{where}.{key}: text size must be a length between 6pt and 96pt, got {v!r}")
        elif key in ("space_before", "space_after", "inset", "stroke_width"):
            if not _is_len(v) or _len_pt(v) > 144:
                problems.append(bad)
        elif key in ("color", "header_color", "stroke_color"):
            if not _is_color(v, palette):
                problems.append(bad + " (hex #RRGGBB or a [colors] name)")
        elif key in ("header_fill", "body_fill", "stripe_fill"):
            if not _is_color(v, palette, allow_empty=True):
                problems.append(bad + " (hex #RRGGBB, a [colors] name, or \"\")")
        elif key == "font":
            if v not in FONTS:
                problems.append(f"{where}.font: {v!r} is not an available font ({', '.join(FONTS)})")
        elif key in ("weight", "header_weight"):
            if v not in WEIGHTS:
                problems.append(bad + f" ({', '.join(WEIGHTS)})")
        elif key in ("align", "header_align"):
            if v not in ALIGNS:
                problems.append(bad + f" ({', '.join(ALIGNS)})")
        elif key in ("italic", "upper"):
            if not isinstance(v, bool):
                problems.append(bad + " (true or false)")
        elif key == "rules":
            if v not in RULES:
                problems.append(bad + f" ({', '.join(RULES)})")
        elif key == "columns":
            if (not isinstance(v, list) or not 1 <= len(v) <= 40
                    or not all(isinstance(c, (int, float)) and not isinstance(c, bool) and 0 < c <= 100 for c in v)):
                problems.append(f"{where}.columns: a list of positive proportions, one per column, e.g. [2, 1, 1]")
    return problems


# ------------------------------------------------------------------ files
@dataclass
class Design:
    theme_text: str
    design: dict[str, Any]
    custom_text: str
    mapping_text: str = DEFAULT_MAPPING
    theme: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, folder: Path) -> "Design":
        mapping = folder / "mapping.toml"
        d = cls(theme_text=(folder / "theme.toml").read_text(encoding="utf-8"),
                design=json.loads((folder / "design.json").read_text(encoding="utf-8")),
                custom_text=(folder / "custom.typ").read_text(encoding="utf-8"),
                mapping_text=mapping.read_text(encoding="utf-8") if mapping.exists() else DEFAULT_MAPPING)
        d.theme = tomllib.loads(d.theme_text)
        return d

    def write(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in self.files().items():
            (folder / name).write_text(text, encoding="utf-8")

    def files(self) -> dict[str, str]:
        return {"theme.toml": self.theme_text,
                "design.json": json.dumps(self.design, indent=2, ensure_ascii=False) + "\n",
                "custom.typ": self.custom_text,
                "mapping.toml": self.mapping_text}

    def with_design(self, design: dict[str, Any]) -> "Design":
        """The same revision files with another design.json."""
        return dataclasses.replace(self, design=design)

    def mapping(self) -> dict[str, Any]:
        """The parsed mapping; raises MappingError when it is invalid."""
        return parse_mapping(self.mapping_text)

    def mapping_key(self) -> str:
        """Identifies the conversion this revision needs: "" for the default mapping."""
        return fingerprint(self.mapping())


def neutral_design() -> Design:
    return Design.load(NEUTRAL_DIR)


def _key_paths(d: dict[str, Any], prefix: str = "") -> set[str]:
    out: set[str] = set()
    for k, v in d.items():
        p = f"{prefix}{k}"
        out.add(p)
        if isinstance(v, dict):
            out |= _key_paths(v, p + ".")
    return out


def validate_theme(text: str) -> tuple[dict[str, Any], list[str]]:
    try:
        theme = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {}, [f"theme.toml: not valid TOML: {e}"]
    base = neutral_design().theme
    problems = [f"theme.toml: missing key {k}" for k in sorted(_key_paths(base) - _key_paths(theme))
                if not k.startswith(("style_map.", "cover.variants"))]
    # Conversion settings belong to mapping.toml, where they are checked and accounted for.
    for section in ("source", "style_map", "desktop"):
        if theme.get(section) != base.get(section):
            problems.append(f"theme.toml: [{section}] is fixed by the app; change how documents are read in "
                            "with design/mapping.toml instead")
    palette = theme.get("colors", {})
    for name, v in palette.items():
        if not _is_color(v, {}):
            problems.append(f"theme.toml: [colors] {name} must be a hex colour #RRGGBB")

    def walk(node: dict[str, Any], path: str) -> None:
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if p.startswith(("source", "style_map", "desktop", "document", "cover")):
                continue
            if isinstance(v, dict):
                walk(v, p)
            elif k in ("size", "font_size") or k.startswith(("margin_",)) or k in (
                    "space_before", "space_after", "inset", "stroke_width", "leading", "first_line_indent"):
                if not _is_len(v):
                    problems.append(f"theme.toml: {p} must be a length like \"10pt\", got {v!r}")
                elif k in ("size", "font_size") and not 6 <= _len_pt(v) <= 96:
                    problems.append(f"theme.toml: {p} must be between 6pt and 96pt")
            elif k == "color" or k.endswith(("_color", "_fill")):
                if not _is_color(v, palette, allow_empty=k.endswith("_fill")):
                    problems.append(f"theme.toml: {p} must be a hex colour or a [colors] name, got {v!r}")
            elif k in ("weight", "header_weight") and v not in WEIGHTS:
                problems.append(f"theme.toml: {p} must be one of {', '.join(WEIGHTS)}")
            elif k == "align" and v not in ALIGNS:
                problems.append(f"theme.toml: {p} must be one of {', '.join(ALIGNS)}")
            elif k == "rules" and v not in RULES:
                problems.append(f"theme.toml: {p} must be one of {', '.join(RULES)}")
            elif k == "text" and not (isinstance(v, str) and len(v) <= 120):
                problems.append(f"theme.toml: {p} must be a short string")
    walk(theme, "")
    fonts = theme.get("fonts", {})
    for k in ("body", "heading", "mono"):
        if fonts.get(k) not in FONTS:
            problems.append(f"theme.toml: fonts.{k} must be one of {', '.join(FONTS)}")
    if theme.get("page", {}).get("paper") not in ("a4", "us-letter", "a5", "us-legal"):
        problems.append("theme.toml: page.paper must be a4, us-letter, a5 or us-legal")
    return theme, problems


def validate_custom(text: str) -> list[str]:
    if len(text.encode("utf-8")) > MAX_CUSTOM_BYTES:
        return [f"custom.typ: larger than {MAX_CUSTOM_BYTES} bytes"]
    problems = []
    code = re.sub(r"//[^\n]*", "", text)  # comments may mention anything
    for pattern, what in _CUSTOM_FORBIDDEN:
        if pattern.search(code):
            problems.append(f"custom.typ: {what} is not allowed")
    if not re.search(r"#let\s+custom-rules\s*\(", code):
        problems.append("custom.typ: must define #let custom-rules(body) = ...")
    return problems


def validate_design(design: Any, palette: dict[str, Any], elements: dict[str, str] | None) -> list[str]:
    """`elements` maps every known "<doc>/<element>" reference to its kind (heading, paragraph,
    table, ...); None skips the reference checks."""
    if not isinstance(design, dict):
        return ["design.json: must be an object"]
    problems: list[str] = []
    unknown = set(design) - {"version", "treatments", "assignments", "overrides"}
    if unknown:
        problems.append(f"design.json: unknown key(s) {', '.join(sorted(unknown))}")
    treatments = design.get("treatments", {})
    if not isinstance(treatments, dict):
        return problems + ["design.json: treatments must be an object"]
    for name, tr in treatments.items():
        where = f"design.json treatments.{name}"
        if not _NAME_RE.match(name):
            problems.append(f"{where}: names are 1-48 letters, digits, spaces, '.', '_' or '-'")
        if not isinstance(tr, dict) or tr.get("kind") not in KINDS:
            problems.append(f"{where}: needs \"kind\": one of {', '.join(KINDS)}")
            continue
        unknown = set(tr) - {"kind", "style", "description"}
        if unknown:
            problems.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        problems += _check_style(tr["kind"], tr.get("style", {}), palette, where)

    def kind_of(ref: str, where: str) -> str | None:
        if not isinstance(ref, str) or not _REF_RE.match(ref):
            problems.append(f"{where}: {ref!r} is not an element reference like \"doc-01/t0008\"")
            return None
        if elements is not None and ref not in elements:
            problems.append(f"{where}: element {ref} does not exist")
            return None
        if elements is not None:
            kind = elements[ref]
        else:
            kind = {p: k for k, p in KIND_PREFIX.items()}.get(ref.split("/")[1][0])
        if kind not in KINDS:
            problems.append(f"{where}: {ref} is a {kind or 'unsupported'} element; only headings, paragraphs and tables can be styled")
            return None
        return kind

    assignments = design.get("assignments", {})
    if not isinstance(assignments, dict):
        problems.append("design.json: assignments must be an object")
        assignments = {}
    for ref, name in assignments.items():
        kind = kind_of(ref, f"design.json assignments.{ref}")
        tr = treatments.get(name) if isinstance(name, str) else None
        if tr is None:
            problems.append(f"design.json assignments.{ref}: unknown treatment {name!r}")
        elif kind and isinstance(tr, dict) and tr.get("kind") != kind:
            problems.append(f"design.json assignments.{ref}: treatment {name!r} is for {tr.get('kind')}s, {ref} is a {kind}")
    overrides = design.get("overrides", {})
    if not isinstance(overrides, dict):
        problems.append("design.json: overrides must be an object")
        overrides = {}
    for ref, style in overrides.items():
        kind = kind_of(ref, f"design.json overrides.{ref}")
        if kind:
            problems += _check_style(kind, style, palette, f"design.json overrides.{ref}")
    return problems


def validate_mapping(text: str) -> list[str]:
    if len(text.encode("utf-8")) > MAX_MAPPING_BYTES:
        return [f"mapping.toml: larger than {MAX_MAPPING_BYTES} bytes"]
    try:
        parse_mapping(text)
    except MappingError as e:
        return e.problems
    return []


def validate(d: Design, elements: dict[str, str] | None) -> dict[str, Any]:
    """Validate all four files; raises DesignError with every problem. Returns the parsed theme.
    `elements` must be the elements of the conversion `d`'s mapping makes (None skips the
    reference checks)."""
    theme, problems = validate_theme(d.theme_text)
    problems += validate_design(d.design, theme.get("colors", {}), elements)
    problems += validate_custom(d.custom_text)
    problems += validate_mapping(d.mapping_text)
    if problems:
        raise DesignError(problems)
    d.theme = theme
    return theme


# ------------------------------------------------------------------ resolution
def resolve_for_document(design: dict[str, Any], doc_key: str, inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """The design.json the template reads for one document: treatments, and the assignments
    and overrides of this document keyed by bare element ID, plus every table's shape."""
    prefix = doc_key + "/"
    return {
        "treatments": design.get("treatments", {}),
        "assign": {k[len(prefix):]: v for k, v in design.get("assignments", {}).items() if k.startswith(prefix)},
        "overrides": {k[len(prefix):]: v for k, v in design.get("overrides", {}).items() if k.startswith(prefix)},
        "tables": {e["id"]: {"header_rows": e["table"]["header_rows"], "columns": e["table"]["columns"]}
                   for e in inventory if e["kind"] == "table" and "table" in e},
    }


def rekey(design: dict[str, Any], mapping: dict[str, str], keep_unmapped: bool) -> dict[str, Any]:
    """Rename the document part of every element reference ("<doc>/<el>") through `mapping`.
    References to documents outside the mapping are kept or dropped."""
    def move(entries: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for ref, v in entries.items():
            doc, _, el = ref.partition("/")
            if doc in mapping:
                out[f"{mapping[doc]}/{el}"] = v
            elif keep_unmapped:
                out[ref] = v
        return out
    return {**design, "assignments": move(design.get("assignments", {})), "overrides": move(design.get("overrides", {}))}


def merge_workspace(base: dict[str, Any], workspace: dict[str, Any], workspace_docs: set[str]) -> dict[str, Any]:
    """A workspace design (already re-keyed to real documents) applied on top of the base:
    treatments come from the workspace; assignments and overrides of the documents that were
    in the workspace come from it, those of every other document stay as they were."""
    def merge(key: str) -> dict[str, Any]:
        kept = {r: v for r, v in base.get(key, {}).items() if r.split("/")[0] not in workspace_docs}
        return {**kept, **workspace.get(key, {})}
    return {"version": 1, "treatments": workspace.get("treatments", {}),
            "assignments": merge("assignments"), "overrides": merge("overrides")}


def _safe_key(d: Design) -> str | None:
    try:
        return d.mapping_key()
    except MappingError:
        return None


def change_scope(old: Design, new: Design) -> dict[str, Any]:
    """What a change touches. "global" when the theme, the custom rules, the mapping or a
    treatment in use changed (every document may look different); otherwise the element
    references whose assignment or override changed. "mapping" says whether the documents
    must be converted again."""
    od, nd = old.design, new.design
    changed_treatments = sorted(n for n in set(od.get("treatments", {})) | set(nd.get("treatments", {}))
                                if od.get("treatments", {}).get(n) != nd.get("treatments", {}).get(n))
    refs = set()
    for key in ("assignments", "overrides"):
        o, n = od.get(key, {}), nd.get(key, {})
        refs |= {r for r in set(o) | set(n) if o.get(r) != n.get(r)}
    # elements using a changed treatment change too
    for r, name in {**od.get("assignments", {}), **nd.get("assignments", {})}.items():
        if name in changed_treatments:
            refs.add(r)
    files = [f for f, a, b in (("theme.toml", old.theme_text, new.theme_text),
                               ("design.json", json.dumps(od, sort_keys=True), json.dumps(nd, sort_keys=True)),
                               ("custom.typ", old.custom_text, new.custom_text),
                               ("mapping.toml", old.mapping_text, new.mapping_text)) if a != b]
    mapping = _safe_key(old) != _safe_key(new)
    is_global = old.theme_text != new.theme_text or old.custom_text != new.custom_text or mapping
    return {"scope": "global" if is_global else "elements", "files": files, "mapping": mapping,
            "treatments": changed_treatments, "refs": sorted(refs),
            "documents": sorted({r.split("/")[0] for r in refs})}


def revert(parent: Design, change: Design, current: Design) -> Design:
    """`current` without what changed from `parent` to `change`: every treatment, assignment
    and override that change set goes back to its value in `parent`, and so does every other
    file it changed. Raises DesignError when a later change touched the same entry or file,
    or when the change (or a later one) re-imported the documents with another mapping."""
    if _safe_key(parent) != _safe_key(change):
        raise DesignError(["it changed how the Word files are read in; remove the later changes first"])
    if _safe_key(change) != _safe_key(current):
        raise DesignError(["a later change re-imported the documents with another mapping"])
    problems: list[str] = []
    out = {k: dict(current.design.get(k, {})) for k in ("treatments", "assignments", "overrides")}
    for key, label in (("treatments", "treatment"), ("assignments", "assignment of"), ("overrides", "override of")):
        p, c = parent.design.get(key, {}), change.design.get(key, {})
        for name in sorted(set(p) | set(c)):
            if p.get(name) == c.get(name):
                continue
            if out[key].get(name) != c.get(name):
                problems.append(f"a later change also changed the {label} {name}")
            elif name in p:
                out[key][name] = p[name]
            else:
                out[key].pop(name, None)
    texts = {}
    for attr, fname in (("theme_text", "theme.toml"), ("custom_text", "custom.typ"),
                        ("mapping_text", "mapping.toml")):
        a, b, now_ = getattr(parent, attr), getattr(change, attr), getattr(current, attr)
        if a != b and now_ != b:
            problems.append(f"a later change also changed {fname}")
        texts[attr] = a if a != b else now_
    if problems:
        raise DesignError(problems)
    return dataclasses.replace(current, design={**current.design, **out}, **texts)
