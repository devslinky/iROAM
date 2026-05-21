"""Verify there are no broken internal links/anchors in the doc site."""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

DOCS = Path(__file__).resolve().parents[1]


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: v for k, v in attrs}
        if tag == "a" and d.get("href"):
            self.links.append(d["href"])
        if d.get("id"):
            self.ids.add(d["id"])


def parse_page(path: Path) -> tuple[list[str], set[str]]:
    p = LinkExtractor()
    p.feed(path.read_text(encoding="utf-8"))
    return p.links, p.ids


def main() -> int:
    pages = list(DOCS.rglob("*.html"))
    pages = [p for p in pages if "_build" not in p.parts]
    pages_by_rel = {str(p.relative_to(DOCS)).replace("\\", "/"): p for p in pages}

    page_ids: dict[str, set[str]] = {}
    page_links: dict[str, list[str]] = {}
    for rel, p in pages_by_rel.items():
        links, ids = parse_page(p)
        page_ids[rel] = ids
        page_links[rel] = links

    errors: list[str] = []
    for rel, links in page_links.items():
        page_dir = Path(rel).parent
        for href in links:
            href = unquote(href)
            if href.startswith(("http:", "https:", "mailto:")):
                continue
            if href.startswith("#"):
                anchor = href[1:]
                if anchor not in page_ids[rel]:
                    errors.append(f"{rel}: missing anchor #{anchor}")
                continue
            target, _, anchor = href.partition("#")
            if not target:
                continue
            resolved = (page_dir / target).as_posix()
            resolved = re.sub(r"/[^/]+/\.\./", "/", "/" + resolved).lstrip("/")
            if resolved not in pages_by_rel and not (DOCS / resolved).exists():
                # accept asset paths too
                errors.append(f"{rel}: missing target {target} (resolved {resolved})")
                continue
            if anchor and resolved.endswith(".html"):
                if anchor not in page_ids.get(resolved, set()):
                    errors.append(f"{rel}: target exists but missing anchor #{anchor} in {resolved}")

    if errors:
        print(f"BROKEN LINKS ({len(errors)}):", file=sys.stderr)
        for e in errors[:50]:
            print("  " + e, file=sys.stderr)
        if len(errors) > 50:
            print(f"  ... and {len(errors) - 50} more", file=sys.stderr)
        return 1
    print(f"OK: {len(pages)} pages, no broken internal links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
