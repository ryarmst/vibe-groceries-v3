"""Notion blocks ⇄ a small Markdown dialect (what the agent reads and writes for page bodies).

Supported: # / ## / ### headings, - bullets, 1. numbered items, - [ ] to-dos, > quotes, a leading
"📌 " (or "> 📌 ") line as a grey callout (the Cooking page style), --- dividers, and **bold** inline.
"""

from __future__ import annotations

import re

from shopping.notion.props import plain_text

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> list[dict]:
    """Text with **bold** → rich_text objects (2000-char chunks)."""
    out, pos = [], 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    rich = []
    for chunk, bold in out:
        for i in range(0, len(chunk), 2000):
            rich.append({"type": "text", "text": {"content": chunk[i:i + 2000]}, "annotations": {"bold": bold}})
    return rich


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _inline(text), **extra}}


def text_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    para: list[str] = []

    def flush():
        if para:
            blocks.append(_block("paragraph", " ".join(para).strip()))
            para.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush()
            continue
        if s.startswith("📌") or s.startswith("> 📌"):
            flush()
            blocks.append({"object": "block", "type": "callout", "callout": {
                "rich_text": _inline(s.lstrip("> ").removeprefix("📌").strip()), "icon": {"type": "emoji", "emoji": "📌"},
                "color": "gray_background"}})
        elif s.startswith("### "):
            flush(); blocks.append(_block("heading_3", s[4:]))
        elif s.startswith("## "):
            flush(); blocks.append(_block("heading_2", s[3:]))
        elif s.startswith("# "):
            flush(); blocks.append(_block("heading_1", s[2:]))
        elif s in ("---", "***"):
            flush(); blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif re.match(r"^- \[( |x|X)\] ", s):
            flush(); blocks.append(_block("to_do", s[6:], checked=s[3] in "xX"))
        elif s.startswith(("- ", "* ")):
            flush(); blocks.append(_block("bulleted_list_item", s[2:]))
        elif re.match(r"^\d+[.)] ", s):
            flush(); blocks.append(_block("numbered_list_item", re.sub(r"^\d+[.)] ", "", s)))
        elif s.startswith("> "):
            flush(); blocks.append(_block("quote", s[2:]))
        else:
            para.append(s)
    flush()
    return blocks


def _rich_md(rich: list | None) -> str:
    out = []
    for r in rich or []:
        t = r.get("plain_text") or (r.get("text") or {}).get("content") or ""
        out.append(f"**{t}**" if (r.get("annotations") or {}).get("bold") and t.strip() else t)
    return "".join(out)


def blocks_to_text(blocks: list[dict]) -> str:
    lines, n = [], 0
    for b in blocks:
        t = b.get("type")
        data = b.get(t) or {}
        text = _rich_md(data.get("rich_text"))
        if t != "numbered_list_item":
            n = 0
        if t == "paragraph":
            lines.append(text)
        elif t in ("heading_1", "heading_2", "heading_3"):
            lines.append("#" * int(t[-1]) + " " + text)
        elif t == "bulleted_list_item":
            lines.append("- " + text)
        elif t == "numbered_list_item":
            n += 1
            lines.append(f"{n}. {text}")
        elif t == "to_do":
            lines.append(f"- [{'x' if data.get('checked') else ' '}] {text}")
        elif t == "callout":
            lines.append("📌 " + text)
        elif t == "quote":
            lines.append("> " + text)
        elif t == "divider":
            lines.append("---")
        elif t == "code":
            lines.append("```\n" + plain_text(data.get("rich_text")) + "\n```")
        elif t in ("toggle",):
            lines.append(text)
        elif t == "child_page":
            lines.append(f"[page: {data.get('title')}]")
    return "\n".join(lines).strip()
