#!/usr/bin/env python3
"""Regenerate wiki/INDEX.md from the frontmatter of every record in this directory.

Record types (by ID prefix, or explicit `type:` frontmatter):
    CON-XXXX  concept   — canonical mental models and definitions
    RUL-XXXX  rule      — binding constraints an agent must not violate
    DEC-XXXX  decision  — choices made; status Rejected = a rejected idea

Run after adding or changing any record:
    python3 wiki/build_index.py
Run the built-in regression tests:
    python3 wiki/build_index.py --test

No external dependencies. Parses the simple `key: value` frontmatter block
(between the first pair of `---` lines) of every record. Files starting with
`_` (templates) and INDEX.md itself are skipped.
"""
import datetime
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX_TYPE = {"CON": "concept", "RUL": "rule", "DEC": "decision"}


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in text[3:end].strip("\n").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        # Strip inline comments: only a `#` preceded by whitespace starts one,
        # so values like "C#" or "issue#42" survive intact.
        val = re.split(r"\s#", val, maxsplit=1)[0].strip()
        meta[key.strip()] = val
    return meta


def esc(cell: str) -> str:
    """Escape characters that would break a Markdown table cell."""
    return cell.replace("|", "\\|")


def record_type(meta: dict) -> str:
    """Explicit `type:` wins; otherwise infer from the ID prefix."""
    t = meta.get("type", "").lower()
    if t in PREFIX_TYPE.values():
        return t
    return PREFIX_TYPE.get(meta.get("id", "")[:3].upper(), "")


def group(rows: list) -> dict:
    """Split records into the four index sections."""
    g = {"concept": [], "rule": [], "decision": [], "rejected": []}
    for r in rows:
        if r["type"] == "decision" and r["status"].lower() == "rejected":
            g["rejected"].append(r)
        elif r["type"] in g:
            g[r["type"]].append(r)
    return g


def _selftest() -> None:
    """Regression tests for parsing, escaping, typing, grouping (run: --test)."""
    fm = parse_frontmatter(
        "---\n"
        "id: DEC-0042\n"
        "title: Use C# bindings | not FFI   # inline comment\n"
        "tags: [lang, interop]  # e.g. comment\n"
        "---\nbody\n"
    )
    assert fm["id"] == "DEC-0042", fm
    # '#' inside a token survives; ' #' starts a comment and is stripped.
    assert fm["title"] == "Use C# bindings | not FFI", fm
    assert fm["tags"].strip("[]") == "lang, interop", fm
    # '|' is escaped so it cannot break the Markdown table.
    assert esc(fm["title"]) == "Use C# bindings \\| not FFI"
    assert parse_frontmatter("no frontmatter here") == {}
    # Type comes from `type:` or falls back to the ID prefix.
    assert record_type({"id": "CON-0001"}) == "concept"
    assert record_type({"id": "RUL-0002", "type": "rule"}) == "rule"
    assert record_type({"id": "XYZ-0001"}) == ""
    # A Rejected decision lands in the Rejected Ideas section.
    rows = [
        {"id": "DEC-0001", "type": "decision", "status": "Accepted"},
        {"id": "DEC-0002", "type": "decision", "status": "Rejected"},
        {"id": "CON-0001", "type": "concept", "status": ""},
        {"id": "RUL-0001", "type": "rule", "status": ""},
    ]
    g = group(rows)
    assert [r["id"] for r in g["rejected"]] == ["DEC-0002"]
    assert [r["id"] for r in g["decision"]] == ["DEC-0001"]
    assert len(g["concept"]) == len(g["rule"]) == 1
    print("self-test OK")


def main() -> None:
    rows = []
    for path in sorted(glob.glob(os.path.join(HERE, "*.md"))):
        name = os.path.basename(path)
        if name == "INDEX.md" or name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as f:
            meta = parse_frontmatter(f.read())
        if not meta.get("id"):
            continue
        rows.append(
            {
                "id": meta.get("id", ""),
                "type": record_type(meta),
                "title": meta.get("title", ""),
                "status": meta.get("status", ""),
                "tags": meta.get("tags", "").strip("[]"),
                "date": meta.get("date", ""),
                "summary": meta.get("summary", ""),
                "file": name,
            }
        )
    rows.sort(key=lambda r: r["id"])
    g = group(rows)

    def table(records, with_status=False):
        if not records:
            return ["*(none yet)*", ""]
        head = "| ID | Title | Status | Tags | Date | Summary |" if with_status \
            else "| ID | Title | Tags | Date | Summary |"
        sep = "|----|-------|--------|------|------|---------|" if with_status \
            else "|----|-------|------|------|---------|"
        out = [head, sep]
        for r in records:
            cells = [f"[{esc(r['id'])}]({r['file']})", esc(r["title"])]
            if with_status:
                cells.append(esc(r["status"]))
            cells += [esc(r["tags"]), esc(r["date"]), esc(r["summary"])]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
        return out

    lines = [
        "# WIKI INDEX — Concepts · Rules · Decisions · Rejected Ideas",
        "",
        "> AUTO-GENERATED by `build_index.py` — do not edit by hand.",
        f"> Last generated: {datetime.date.today().isoformat()}  ·  "
        f"{len(g['concept'])} concepts · {len(g['rule'])} rules · "
        f"{len(g['decision'])} decisions · {len(g['rejected'])} rejected ideas.",
        "",
        "Read this index first; then open only the single record you need.",
        "**Check Rejected Ideas before proposing any approach.**",
        "",
        "## Concepts",
        "",
        *table(g["concept"]),
        "## Rules (binding — do not violate)",
        "",
        *table(g["rule"]),
        "## Decisions",
        "",
        *table(g["decision"], with_status=True),
        "## Rejected Ideas — do not re-propose",
        "",
        *table(g["rejected"]),
    ]

    out = os.path.join(HERE, "INDEX.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    print(
        f"Wrote {out} ({len(rows)} records: {len(g['concept'])} concepts, "
        f"{len(g['rule'])} rules, {len(g['decision'])} decisions, "
        f"{len(g['rejected'])} rejected)"
    )


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        _selftest()
    else:
        main()
