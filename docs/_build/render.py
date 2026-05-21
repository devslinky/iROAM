"""Render the static iROAM doc site.

Inputs:
  - docs/_build/modules.json (run extract.py first)
  - docs/_build/content/*.md (hand-authored prose)

Outputs:
  - docs/{index, architecture, data-model, examples, api}.html
  - docs/modules/*.html
  - docs/search-index.json

Stdlib only.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
BUILD = DOCS / "_build"
CONTENT = BUILD / "content"


# ---------------------------------------------------------------------------
# Tiny Markdown → HTML
# ---------------------------------------------------------------------------

DOUBLE_BACKTICK = re.compile(r"``([^`]+)``")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")


def inline(text: str) -> str:
    """Apply inline markdown to a single line (already HTML-escaped)."""
    out = html.escape(text)

    def link_sub(m: re.Match[str]) -> str:
        label = m.group(1)
        url = m.group(2)
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_sub, out)
    # Double-backticks first (reST style) so single-backtick regex doesn't
    # carve them up into half-spans.
    out = DOUBLE_BACKTICK.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def slugify(text: str) -> str:
    s = text.lower()
    s = s.replace("/", "-").replace(".", "-")
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s or "section"


@dataclass
class Heading:
    level: int
    text: str
    slug: str


def render_markdown(md: str) -> tuple[str, list[Heading]]:
    """Convert a small subset of Markdown to HTML.

    Supports: # headings, paragraphs, lists, code fences (with mermaid as a
    special language), tables, inline code/links/bold/italic, blockquotes,
    ::: callout ::: blocks.
    """
    lines = md.splitlines()
    out: list[str] = []
    headings: list[Heading] = []
    i = 0
    n = len(lines)
    used_slugs: dict[str, int] = {}

    def push_heading(level: int, text: str) -> None:
        base = slugify(text)
        used_slugs[base] = used_slugs.get(base, 0) + 1
        slug = base if used_slugs[base] == 1 else f"{base}-{used_slugs[base]}"
        headings.append(Heading(level=level, text=text, slug=slug))
        out.append(f'<h{level} id="{slug}">'
                   f'<a class="anchor" href="#{slug}" aria-hidden="true">#</a>'
                   f"{inline(text)}</h{level}>")

    while i < n:
        line = lines[i]

        # Code fence
        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            body = "\n".join(buf)
            if lang == "mermaid":
                out.append(f'<div class="mermaid">{html.escape(body)}</div>')
            else:
                cls = f' class="language-{html.escape(lang)}"' if lang else ""
                out.append(f'<pre><code{cls}>{html.escape(body)}</code></pre>')
            continue

        # Callout block: ::: type Title ... :::
        if line.startswith(":::"):
            m = re.match(r":::\s*(\w+)\s*(.*)", line)
            kind = m.group(1) if m else "note"
            title = m.group(2).strip() if m else ""
            i += 1
            buf2: list[str] = []
            while i < n and not lines[i].startswith(":::"):
                buf2.append(lines[i])
                i += 1
            i += 1
            inner_html, _ = render_markdown("\n".join(buf2))
            head_html = f'<div class="callout-title">{html.escape(title)}</div>' if title else ""
            out.append(f'<div class="callout callout-{html.escape(kind)}">{head_html}{inner_html}</div>')
            continue

        # Heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            push_heading(len(m.group(1)), m.group(2).strip())
            i += 1
            continue

        # Table (very small): header | header / --- | --- / row | row
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s\-|:]+\|[\s\-|:]+\|?\s*$", lines[i + 1]):
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip separator
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            thead = "".join(f"<th>{inline(h)}</th>" for h in headers)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue

        # Unordered list
        if re.match(r"^\s*[-*]\s+", line):
            items: list[str] = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                m_li = re.match(r"^\s*[-*]\s+(.*)$", lines[i])
                items.append(m_li.group(1))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(it)}</li>" for it in items) + "</ul>")
            continue

        # Ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                m_li = re.match(r"^\s*\d+\.\s+(.*)$", lines[i])
                items.append(m_li.group(1))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(it)}</li>" for it in items) + "</ol>")
            continue

        # Blockquote
        if line.startswith(">"):
            buf3: list[str] = []
            while i < n and lines[i].startswith(">"):
                buf3.append(lines[i].lstrip("> ").rstrip())
                i += 1
            inner, _ = render_markdown("\n".join(buf3))
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # Blank line
        if not line.strip():
            i += 1
            continue

        # Paragraph (consume until blank or special)
        para: list[str] = [line]
        i += 1
        while i < n and lines[i].strip() and not lines[i].startswith(("#", "```", ":::", ">"))\
              and not re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")

    return "\n".join(out), headings


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

@dataclass
class NavItem:
    label: str
    href: str
    section: str = "Pages"
    children: list["NavItem"] = field(default_factory=list)


def build_nav() -> list[NavItem]:
    return [
        NavItem("Overview", "index.html", "Start here"),
        NavItem("Architecture", "architecture.html", "Start here"),
        NavItem("Data model", "data-model.html", "Start here"),
        NavItem("Database dataflow", "database-dataflow.html", "Start here"),
        NavItem("Frontend", "frontend.html", "Start here"),
        NavItem("Operations", "operations.html", "Start here"),
        NavItem("Examples", "examples.html", "Start here"),
        NavItem("API reference", "api.html", "Reference"),
        NavItem("Collector", "modules/apps-collector.html", "Modules"),
        NavItem("Analytics", "modules/apps-analytics.html", "Modules"),
        NavItem("API", "modules/apps-api.html", "Modules"),
        NavItem("Dashboard", "modules/apps-dashboard.html", "Modules"),
        NavItem("Core", "modules/core.html", "Modules"),
        NavItem("Database", "modules/db.html", "Modules"),
        NavItem("Scripts", "modules/scripts.html", "Modules"),
        NavItem("Bunching predictor", "modules/deployment-bunching.html", "Modules"),
        NavItem("Legacy", "modules/legacy.html", "Modules"),
    ]


def relpath(base: str, href: str) -> str:
    """Resolve href relative to the page's location."""
    if href.startswith(("http:", "https:", "#")):
        return href
    if base == "":
        return href
    # base is one directory deep (modules/)
    return "../" + href


def render_sidebar(nav: list[NavItem], current: str, base: str) -> str:
    sections: dict[str, list[NavItem]] = {}
    for item in nav:
        sections.setdefault(item.section, []).append(item)

    parts: list[str] = []
    for section, items in sections.items():
        parts.append(f'<div class="nav-section"><h4>{html.escape(section)}</h4><ul>')
        for it in items:
            cls = ' class="active"' if it.href == current else ""
            href = relpath(base, it.href)
            parts.append(f'<li{cls}><a href="{href}">{html.escape(it.label)}</a></li>')
        parts.append("</ul></div>")
    return "\n".join(parts)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title} — iROAM docs</title>
<link rel="stylesheet" href="{base}assets/styles.css" />
<link rel="stylesheet" href="{base}assets/highlight-github.css" />
</head>
<body>
<button class="nav-toggle" aria-label="Toggle navigation">☰</button>
<aside class="sidebar">
  <div class="sidebar-header">
    <a class="brand" href="{base}index.html">iROAM <span>docs</span></a>
  </div>
  <div class="search-box">
    <input id="search-input" type="search" placeholder="Search docs…" autocomplete="off" />
    <div id="search-results" class="search-results" hidden></div>
  </div>
  <nav class="nav">{sidebar}</nav>
  <div class="sidebar-footer">Built {built}<br/>{nfiles} files · {nsymbols} symbols</div>
</aside>
<main class="content">
  <div class="breadcrumb">{breadcrumb}</div>
  <article>{body}</article>
  <div class="content-toc">{toc}</div>
</main>
<script src="{base}assets/lunr.min.js"></script>
<script src="{base}assets/highlight.min.js"></script>
<script>hljs.highlightAll();</script>
<script src="{base}assets/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true, theme:'neutral', securityLevel:'loose', flowchart:{{curve:'basis'}}}});</script>
<script>window.SEARCH_INDEX_URL = "{base}search-index.json";</script>
<script src="{base}assets/app.js"></script>
</body>
</html>
"""


def render_toc(headings: list[Heading]) -> str:
    items = [h for h in headings if 2 <= h.level <= 3]
    if not items:
        return ""
    parts = ["<h4>On this page</h4><ul>"]
    for h in items:
        cls = " sub" if h.level == 3 else ""
        parts.append(f'<li class="toc-h{h.level}{cls}"><a href="#{h.slug}">{html.escape(h.text)}</a></li>')
    parts.append("</ul>")
    return "".join(parts)


def write_page(
    *,
    out_path: Path,
    base: str,
    nav: list[NavItem],
    current: str,
    title: str,
    breadcrumb: str,
    body_html: str,
    headings: list[Heading],
    stats: dict[str, int],
    built: str,
) -> None:
    page = PAGE.format(
        title=html.escape(title),
        sidebar=render_sidebar(nav, current, base),
        breadcrumb=breadcrumb,
        body=body_html,
        toc=render_toc(headings),
        base=base,
        built=built,
        nfiles=stats["files"],
        nsymbols=stats["symbols"],
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------
# Module pages
# ---------------------------------------------------------------------------

def format_docstring(text: str) -> str:
    """Render a plain-prose docstring: blank-line-separated paragraphs.

    Code-like indented blocks become <pre>. Lines starting with '- ' become
    a single bullet list.
    """
    if not text:
        return ""
    text = text.strip()
    blocks: list[str] = re.split(r"\n\s*\n", text)
    out: list[str] = []
    for blk in blocks:
        lines = blk.splitlines()
        if all(ln.startswith("    ") or not ln.strip() for ln in lines):
            stripped = "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in lines)
            out.append(f"<pre><code>{html.escape(stripped)}</code></pre>")
            continue
        if all(re.match(r"^\s*[-*]\s+", ln) or not ln.strip() for ln in lines):
            items = [re.sub(r"^\s*[-*]\s+", "", ln) for ln in lines if ln.strip()]
            out.append("<ul>" + "".join(f"<li>{inline(it)}</li>" for it in items) + "</ul>")
            continue
        out.append("<p>" + inline(" ".join(ln.strip() for ln in lines)) + "</p>")
    return "\n".join(out)


def render_symbol(sym: dict[str, Any], file_anchor: str) -> tuple[str, list[Heading]]:
    name = sym["name"]
    sig = sym["signature"]
    kind = sym["kind"]
    anchor = f"{file_anchor}-{slugify(name)}"
    parts: list[str] = []
    headings: list[Heading] = []
    parts.append(f'<div class="symbol symbol-{kind}" id="{anchor}">')
    parts.append(f'<div class="symbol-head">')
    parts.append(f'<span class="symbol-kind">{kind}</span>')
    parts.append(f'<a class="symbol-name" href="#{anchor}">{html.escape(name)}</a>')
    parts.append("</div>")
    parts.append(f'<pre class="symbol-sig"><code class="language-python">{html.escape(sig)}</code></pre>')
    if sym["docstring"]:
        parts.append(f'<div class="symbol-doc">{format_docstring(sym["docstring"])}</div>')
    if kind == "class" and sym.get("methods"):
        parts.append('<details class="methods"><summary>Methods</summary>')
        for m in sym["methods"]:
            method_anchor = f"{anchor}-{slugify(m['name'])}"
            parts.append(f'<div class="method" id="{method_anchor}">')
            parts.append(f'<pre><code class="language-python">{html.escape(m["signature"])}</code></pre>')
            if m["docstring"]:
                parts.append(f'<div class="symbol-doc">{format_docstring(m["docstring"])}</div>')
            parts.append("</div>")
        parts.append("</details>")
    parts.append("</div>")
    return "\n".join(parts), headings


def render_file(file_rec: dict[str, Any], imports_index: dict[str, list[str]]) -> tuple[str, list[Heading]]:
    path = file_rec["path"]
    file_anchor = slugify(path)
    parts: list[str] = []
    headings: list[Heading] = []
    parts.append(f'<section class="file" id="{file_anchor}">')
    parts.append(f'<h3 class="file-heading">'
                 f'<a class="anchor" href="#{file_anchor}" aria-hidden="true">#</a>'
                 f'<code>{html.escape(path)}</code></h3>')
    headings.append(Heading(level=3, text=path, slug=file_anchor))

    if file_rec["summary"]:
        parts.append(f'<p class="file-summary">{html.escape(file_rec["summary"])}</p>')
    if file_rec["docstring"] and file_rec["docstring"].strip() != file_rec["summary"]:
        parts.append(f'<div class="file-doc">{format_docstring(file_rec["docstring"])}</div>')

    if file_rec["symbols"]:
        public = [s["name"] for s in file_rec["symbols"]]
        parts.append('<div class="symbol-list"><strong>Public:</strong> ' +
                     ", ".join(f'<a href="#{file_anchor}-{slugify(n)}"><code>{html.escape(n)}</code></a>'
                               for n in public) +
                     "</div>")
    else:
        parts.append('<p class="file-no-symbols">No public symbols.</p>')

    for sym in file_rec["symbols"]:
        s_html, _ = render_symbol(sym, file_anchor)
        parts.append(s_html)

    # Used-by
    module_id = path.removesuffix(".py").replace("/", ".")
    callers: set[str] = set()
    for sym in file_rec["symbols"]:
        key = f"{module_id}.{sym['name']}"
        for caller in imports_index.get(key, []):
            if caller != path:
                callers.add(caller)
    if callers:
        items = "".join(f"<li><code>{html.escape(c)}</code></li>" for c in sorted(callers))
        parts.append(f'<div class="used-by"><strong>Used by</strong><ul>{items}</ul></div>')

    parts.append("</section>")
    return "\n".join(parts), headings


def render_package_page(pkg: dict[str, Any], imports_index: dict[str, list[str]]) -> tuple[str, list[Heading]]:
    parts: list[str] = []
    headings: list[Heading] = []

    title = pkg["name"]
    parts.append(f'<h1>{html.escape(title)}</h1>')
    headings.append(Heading(level=1, text=title, slug=slugify(title)))

    parts.append(f'<p class="lead">{html.escape(pkg["summary"])}</p>')

    if pkg.get("archived"):
        parts.append('<div class="callout callout-warning"><div class="callout-title">Archived</div>'
                     '<p>This package is not used by the live system. Kept for historical reference. '
                     'See <a href="../modules/apps-analytics.html">apps/analytics</a> for the live successor.</p></div>')

    meta_parts = []
    if pkg.get("entry_points"):
        meta_parts.append("<dt>Entry points</dt><dd><ul>" +
                          "".join(f'<li><code>{html.escape(ep)}</code></li>' for ep in pkg["entry_points"]) +
                          "</ul></dd>")
    if pkg.get("owns_tables"):
        meta_parts.append("<dt>Owns tables</dt><dd>" +
                          ", ".join(f'<code>{html.escape(t)}</code>' for t in pkg["owns_tables"]) +
                          "</dd>")
    meta_parts.append(f"<dt>Files</dt><dd>{len(pkg['files'])}</dd>")
    npub = sum(len(f["symbols"]) for f in pkg["files"])
    meta_parts.append(f"<dt>Public symbols</dt><dd>{npub}</dd>")
    parts.append("<dl class=\"package-meta\">" + "".join(meta_parts) + "</dl>")

    # Quick file list at top
    parts.append('<h2 id="files">Files</h2>')
    headings.append(Heading(level=2, text="Files", slug="files"))
    if pkg["files"]:
        parts.append('<ul class="file-index">')
        for f in pkg["files"]:
            anchor = slugify(f["path"])
            summary = f["summary"] or "—"
            parts.append(f'<li><a href="#{anchor}"><code>{html.escape(f["path"])}</code></a> — '
                         f'{html.escape(summary)}</li>')
        parts.append("</ul>")

    for f in pkg["files"]:
        # Archived files: list only, no per-symbol output
        if pkg.get("archived"):
            anchor = slugify(f["path"])
            parts.append(f'<section class="file" id="{anchor}">')
            parts.append(f'<h3 class="file-heading"><a class="anchor" href="#{anchor}" aria-hidden="true">#</a>'
                         f'<code>{html.escape(f["path"])}</code></h3>')
            if f["summary"]:
                parts.append(f'<p class="file-summary">{html.escape(f["summary"])}</p>')
            else:
                parts.append('<p class="file-summary">Legacy reference — see <a href="../modules/apps-analytics.html">apps/analytics</a>.</p>')
            parts.append("</section>")
            headings.append(Heading(level=3, text=f["path"], slug=anchor))
            continue
        fhtml, fhs = render_file(f, imports_index)
        parts.append(fhtml)
        headings.extend(fhs)

    return "\n".join(parts), headings


# ---------------------------------------------------------------------------
# API page (global)
# ---------------------------------------------------------------------------

def render_api_page(packages: list[dict[str, Any]]) -> tuple[str, list[Heading]]:
    parts: list[str] = []
    headings: list[Heading] = []
    parts.append("<h1>API reference</h1>")
    headings.append(Heading(level=1, text="API reference", slug="api-reference"))
    parts.append('<p class="lead">Every public function and class across the codebase, extracted from <code>ast</code>. '
                 'Grouped by package. For per-package context, follow the link in each section heading.</p>')

    # Quick stats
    total_files = sum(len(p["files"]) for p in packages)
    total_syms = sum(len(f["symbols"]) for p in packages for f in p["files"])
    parts.append(f'<p class="muted">{total_files} files · {total_syms} public symbols across {len(packages)} packages.</p>')

    # Index
    parts.append('<h2 id="index">Index</h2>')
    headings.append(Heading(level=2, text="Index", slug="index"))
    parts.append('<div class="api-index">')
    for pkg in packages:
        if pkg.get("archived"):
            continue
        pkg_anchor = slugify(pkg["name"])
        parts.append(f'<div class="api-index-pkg"><h4><a href="#{pkg_anchor}">{html.escape(pkg["name"])}</a></h4>')
        names: list[str] = []
        for f in pkg["files"]:
            for sym in f["symbols"]:
                file_anchor = slugify(f["path"])
                a = f"#{pkg_anchor}-{file_anchor}-{slugify(sym['name'])}"
                names.append(f'<a href="{a}"><code>{html.escape(sym["name"])}</code></a>')
        parts.append(" · ".join(names) if names else "—")
        parts.append("</div>")
    parts.append("</div>")

    # Detail
    for pkg in packages:
        if pkg.get("archived"):
            continue
        pkg_anchor = slugify(pkg["name"])
        parts.append(f'<h2 id="{pkg_anchor}"><a class="anchor" href="#{pkg_anchor}" aria-hidden="true">#</a>'
                     f'{html.escape(pkg["name"])} '
                     f'<a class="muted-link" href="modules/{pkg["id"]}.html">(module page →)</a></h2>')
        headings.append(Heading(level=2, text=pkg["name"], slug=pkg_anchor))
        for f in pkg["files"]:
            if not f["symbols"]:
                continue
            file_anchor = f"{pkg_anchor}-{slugify(f['path'])}"
            parts.append(f'<h3 id="{file_anchor}"><code>{html.escape(f["path"])}</code></h3>')
            for sym in f["symbols"]:
                sym_anchor = f"{file_anchor}-{slugify(sym['name'])}"
                parts.append(f'<div class="symbol symbol-{sym["kind"]}" id="{sym_anchor}">')
                parts.append('<div class="symbol-head">')
                parts.append(f'<span class="symbol-kind">{sym["kind"]}</span>')
                parts.append(f'<a class="symbol-name" href="#{sym_anchor}">{html.escape(sym["name"])}</a>')
                parts.append("</div>")
                parts.append(f'<pre class="symbol-sig"><code class="language-python">{html.escape(sym["signature"])}</code></pre>')
                if sym["docstring"]:
                    parts.append(f'<div class="symbol-doc">{format_docstring(sym["docstring"])}</div>')
                parts.append("</div>")
    return "\n".join(parts), headings


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------

def build_search_documents(packages: list[dict[str, Any]], prose_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []

    # One doc per prose page
    for p in prose_pages:
        docs.append({
            "id": p["href"],
            "title": p["title"],
            "page": p["title"],
            "href": p["href"],
            "body": p["plain_text"],
            "kind": "page",
        })

    # One doc per module page
    for pkg in packages:
        href = f"modules/{pkg['id']}.html"
        body_chunks = [pkg["name"], pkg["summary"]]
        for f in pkg["files"]:
            body_chunks.append(f["path"])
            body_chunks.append(f["summary"])
            body_chunks.append(f["docstring"])
            for sym in f["symbols"]:
                body_chunks.append(sym["name"])
                body_chunks.append(sym["signature"])
                body_chunks.append(sym["docstring"])
        docs.append({
            "id": href,
            "title": pkg["name"],
            "page": pkg["name"],
            "href": href,
            "body": " ".join(s for s in body_chunks if s),
            "kind": "package",
        })

    # One doc per public symbol (linked into its package page)
    for pkg in packages:
        if pkg.get("archived"):
            continue
        for f in pkg["files"]:
            for sym in f["symbols"]:
                file_anchor = slugify(f["path"])
                anchor = f"{file_anchor}-{slugify(sym['name'])}"
                href = f"modules/{pkg['id']}.html#{anchor}"
                docs.append({
                    "id": href,
                    "title": f"{sym['name']} ({sym['kind']})",
                    "page": pkg["name"],
                    "href": href,
                    "body": " ".join([sym["name"], sym["signature"], sym["docstring"], f["path"]]),
                    "kind": "symbol",
                })

    return docs


# ---------------------------------------------------------------------------
# Stats / housekeeping
# ---------------------------------------------------------------------------

def plain_text_from_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))


def get_build_timestamp() -> str:
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%cs"], capture_output=True, text=True, cwd=ROOT)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    from datetime import date
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    modules_path = BUILD / "modules.json"
    if not modules_path.exists():
        print("modules.json missing; run extract.py first", file=sys.stderr)
        return 1
    data = json.loads(modules_path.read_text())
    packages: list[dict[str, Any]] = data["packages"]
    imports_index: dict[str, list[str]] = data["imports_index"]

    nfiles = sum(len(p["files"]) for p in packages)
    nsymbols = sum(len(f["symbols"]) for p in packages for f in p["files"])
    stats = {"files": nfiles, "symbols": nsymbols}
    built = get_build_timestamp()
    nav = build_nav()

    prose_pages_meta = [
        {"slug": "index", "title": "Overview", "href": "index.html", "breadcrumb": "Overview"},
        {"slug": "architecture", "title": "Architecture", "href": "architecture.html",
         "breadcrumb": '<a href="index.html">Overview</a> / Architecture'},
        {"slug": "data-model", "title": "Data model", "href": "data-model.html",
         "breadcrumb": '<a href="index.html">Overview</a> / Data model'},
        {"slug": "database-dataflow", "title": "Database dataflow", "href": "database-dataflow.html",
         "breadcrumb": '<a href="index.html">Overview</a> / Database dataflow'},
        {"slug": "frontend", "title": "Frontend", "href": "frontend.html",
         "breadcrumb": '<a href="index.html">Overview</a> / Frontend'},
        {"slug": "operations", "title": "Operations", "href": "operations.html",
         "breadcrumb": '<a href="index.html">Overview</a> / Operations'},
        {"slug": "examples", "title": "Examples", "href": "examples.html",
         "breadcrumb": '<a href="index.html">Overview</a> / Examples'},
    ]

    rendered_prose: list[dict[str, Any]] = []
    for meta in prose_pages_meta:
        md_path = CONTENT / f"{meta['slug']}.md"
        body_html, headings = render_markdown(md_path.read_text(encoding="utf-8"))
        out_path = DOCS / meta["href"]
        write_page(
            out_path=out_path,
            base="",
            nav=nav,
            current=meta["href"],
            title=meta["title"],
            breadcrumb=meta["breadcrumb"],
            body_html=body_html,
            headings=headings,
            stats=stats,
            built=built,
        )
        rendered_prose.append({**meta, "plain_text": plain_text_from_html(body_html)})

    # API page
    api_body, api_headings = render_api_page(packages)
    write_page(
        out_path=DOCS / "api.html",
        base="",
        nav=nav,
        current="api.html",
        title="API reference",
        breadcrumb='<a href="index.html">Overview</a> / API reference',
        body_html=api_body,
        headings=api_headings,
        stats=stats,
        built=built,
    )
    rendered_prose.append({
        "slug": "api",
        "title": "API reference",
        "href": "api.html",
        "plain_text": plain_text_from_html(api_body),
    })

    # Module pages
    for pkg in packages:
        if pkg["id"] == "legacy":
            # Use the hand-written legacy.md as the main content, then list files
            legacy_md = CONTENT / "legacy.md"
            body_html, headings = render_markdown(legacy_md.read_text(encoding="utf-8"))
            # append the file list
            file_list_html = '<h2 id="files-listed">Files in this directory</h2><ul>'
            for f in pkg["files"]:
                file_list_html += f'<li><code>{html.escape(f["path"])}</code>'
                if f["summary"]:
                    file_list_html += f" — {html.escape(f['summary'])}"
                file_list_html += "</li>"
            file_list_html += "</ul>"
            body_html += file_list_html
            headings.append(Heading(level=2, text="Files in this directory", slug="files-listed"))
            title = "Legacy"
        else:
            body_html, headings = render_package_page(pkg, imports_index)
            title = pkg["name"]

        href = f"modules/{pkg['id']}.html"
        write_page(
            out_path=DOCS / href,
            base="../",
            nav=nav,
            current=href,
            title=title,
            breadcrumb=f'<a href="../index.html">Overview</a> / <a href="../api.html">Modules</a> / {html.escape(pkg["name"])}',
            body_html=body_html,
            headings=headings,
            stats=stats,
            built=built,
        )

    # Search index
    docs_to_index = build_search_documents(packages, rendered_prose)
    (DOCS / "search-index.json").write_text(json.dumps(docs_to_index), encoding="utf-8")

    # Stats
    print(f"Rendered:")
    print(f"  prose pages: {len(rendered_prose)}")
    print(f"  module pages: {len(packages)}")
    print(f"  search docs:  {len(docs_to_index)}")
    print(f"Open: file://{(DOCS / 'index.html').resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
