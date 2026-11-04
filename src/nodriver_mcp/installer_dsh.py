"""Installer support for DeepSeek Harness profiles.

DeepSeek Harness (``dsh``) keeps its user configuration in a YAML *patch layer*
at ``$DSH_HOME/profiles/<profile>/cordis.patch.yml``. That file is a hand-edited
list of loader patches, not a JSON/TOML settings document: it may contain
comments, ``!!js`` expressions and unrelated rows. Parsing and re-dumping it
would rewrite everything the user wrote, so this module edits only the
``mcp-nodriver`` row and leaves the rest of the file byte-for-byte intact.

The row inserted here matches the registration used by the harness itself::

    - insert:
        - id: mcp-nodriver
          name: '@deepseek-ai/dsh-mcp-client'
          config:
            serverName: nodriver
            transport: stdio
            command: "/path/to/python"
            args: ["-m", "nodriver_mcp"]

``@deepseek-ai/dsh-mcp-client`` is shipped with the harness, so the row needs no
extra package installation; the harness hot-reloads the patch layer and exposes
the tools as ``mcp__nodriver__<tool>``.
"""

from __future__ import annotations

import json
import re

from .installer_data import MCP_SERVER_NAME

#: Loader row id owned by this installer, also used as the uninstall marker.
DSH_ROW_ID = "mcp-nodriver"

#: Harness plugin that bridges external MCP servers into the tool registry.
DSH_PLUGIN_NAME = "@deepseek-ai/dsh-mcp-client"

_ROW_PATTERN = re.compile(
    r"""^[ \t]*-[ \t]+id:[ \t]*["']?%s["']?[ \t]*(?:\#.*)?$""" % DSH_ROW_ID
)
_INSERT_PATTERN = re.compile(r"""^[ \t]*-[ \t]+insert:[ \t]*(?:\#.*)?$""")
_PLACEHOLDER_PATTERN = re.compile(r"""^\[\][ \t]*(?:\#.*)?$""")


def render_block(command: str, args: list[str] | tuple[str, ...] = ()) -> str:
    """Render the patch entry that registers this server.

    ``command`` and ``args`` are JSON-encoded: JSON strings and arrays are valid
    YAML flow scalars/sequences, so paths containing spaces, quotes or Windows
    backslashes survive without hand-written escaping.
    """
    return "\n".join(
        (
            "- insert:",
            f"    - id: {DSH_ROW_ID}",
            f"      name: '{DSH_PLUGIN_NAME}'",
            "      config:",
            f"        serverName: {MCP_SERVER_NAME}",
            "        transport: stdio",
            f"        command: {json.dumps(command)}",
            f"        args: {json.dumps(list(args))}",
            "",
        )
    )


def _split_bom(text: str) -> tuple[str, str]:
    """Separate a UTF-8 byte-order mark so the line patterns still match."""
    if text.startswith("\ufeff"):
        return "\ufeff", text[1:]
    return "", text


def has_row(text: str) -> bool:
    """Whether the patch layer already registers the nodriver row."""
    _, text = _split_bom(text)
    return _row_count(text) > 0


def _row_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if _ROW_PATTERN.match(line))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_ignorable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _row_block_bounds(lines: list[str], start: int) -> int:
    """Return the exclusive end of the row block that begins at ``start``.

    Deeper-indented lines belong to the row. A comment at the row's own
    indentation is kept when the mapping continues below it (interleaved
    comment) and otherwise belongs to the surrounding document, so it survives
    an uninstall.
    """
    row_indent = _indent(lines[start])
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if not line.strip():
            end += 1
            continue
        if line.lstrip().startswith("#"):
            following = _next_entry_indent(lines, end)
            if _indent(line) <= row_indent and (following is None or following <= row_indent):
                break
            end += 1
            continue
        if _indent(line) > row_indent:
            end += 1
            continue
        break
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return end


def _next_entry_indent(lines: list[str], index: int) -> int | None:
    """Indent of the next sibling entry after ``index``, if one exists."""
    for line in lines[index:]:
        if _is_ignorable(line) or _PLACEHOLDER_PATTERN.match(line):
            continue
        return _indent(line)
    return None


def _wrapper_index(lines: list[str], row: int) -> int | None:
    """Index of the ``- insert:`` wrapper that owns the row, when there is one."""
    index = row - 1
    while index >= 0 and _is_ignorable(lines[index]):
        index -= 1
    if index < 0 or not _INSERT_PATTERN.match(lines[index]):
        return None
    if _indent(lines[index]) >= _indent(lines[row]):
        return None
    return index


def _remove_row(text: str) -> str:
    """Remove every nodriver row; a repeated row would fail to load."""
    while True:
        lines = text.splitlines()
        index = next(
            (i for i, line in enumerate(lines) if _ROW_PATTERN.match(line)), None
        )
        if index is None:
            return text
        end = _row_block_bounds(lines, index)
        start = index
        wrapper = _wrapper_index(lines, index)
        if wrapper is not None:
            sibling = _next_entry_indent(lines, end)
            if sibling is None or sibling < _indent(lines[index]):
                start = wrapper
        wrapper_indent = _indent(lines[start]) if start != index else None
        del lines[start:end]
        if wrapper_indent is not None:
            _keep_parent_mapping(lines, start, wrapper_indent)
        while lines and not lines[-1].strip():
            lines.pop()
        body = "\n".join(lines)
        text = body + "\n" if body else ""


def _keep_parent_mapping(lines: list[str], position: int, indent: int) -> None:
    """Leave ``[]`` behind when a removed wrapper was its parent's only value.

    A row can sit inside a group's ``config:`` mapping. Removing the wrapper
    then leaves a key without a value, which DeepSeek Harness rejects; an empty
    list keeps the group valid.
    """
    previous = position - 1
    while previous >= 0 and _is_ignorable(lines[previous]):
        previous -= 1
    if previous < 0:
        return
    line = lines[previous]
    if line.lstrip().startswith("-") or _indent(line) >= indent:
        return
    if not re.match(r"""^[^#\s][^:]*:[ \t]*(?:#.*)?$""", line.strip()):
        return
    lines.insert(position, " " * indent + "[]")


def _without_placeholder(text: str) -> str:
    """Drop a standalone ``[]`` root placeholder so entries can be appended."""
    lines = [line for line in text.splitlines() if not _PLACEHOLDER_PATTERN.match(line)]
    body = "\n".join(lines).rstrip("\n")
    return body + "\n" if body else ""


def _ensure_root_list(text: str) -> str:
    """Keep the patch layer a valid YAML list when it holds no entries.

    DeepSeek Harness rejects an empty or comments-only patch file, so an
    uninstall that removes the last row leaves the ``[]`` placeholder behind.
    """
    entries = [line for line in text.splitlines() if not _is_ignorable(line)]
    if entries:
        return text
    body = text.rstrip("\n")
    return body + "\n[]\n" if body else "[]\n"


def _newline_style(text: str) -> str:
    """Line ending the document already uses, so edits keep it."""
    return "\r\n" if "\r\n" in text else "\n"


def _with_newline(text: str, newline: str) -> str:
    return text if newline == "\n" else text.replace("\n", "\r\n")


def upsert(text: str, command: str, args: list[str] | tuple[str, ...] = ()) -> str:
    """Insert or replace the nodriver row, preserving every other byte."""
    newline = _newline_style(text)
    bom, text = _split_bom(text)
    text = text.replace("\r\n", "\n")
    block = render_block(command, args)
    if _row_count(text) == 1 and block in text:
        return bom + _with_newline(text, newline)
    if has_row(text):
        text = _remove_row(text)
    text = _without_placeholder(text)
    body = text.rstrip("\n")
    if not body:
        return bom + _with_newline(block, newline)
    return bom + _with_newline(body + "\n\n" + block, newline)


def remove(text: str) -> str:
    """Remove the nodriver row and keep the patch layer bootable."""
    if not has_row(text):
        return text
    newline = _newline_style(text)
    bom, body = _split_bom(text)
    body = _ensure_root_list(_remove_row(body.replace("\r\n", "\n")))
    return bom + _with_newline(body, newline)
