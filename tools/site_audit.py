#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline, read-only static-page accessibility, SEO and policy consistency checks.

This tool performs deterministic, heuristic checks on local HTML files only.
It makes no network requests, does not execute scripts, and is not legal advice.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
import sys
from typing import Sequence
from urllib.parse import unquote, urlsplit


REQUIRED_PAGES = ("index.html", "privacy-policy.html", "terms-of-use.html", "support.html")
SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
DANGEROUS_SCHEMES = frozenset({"javascript", "data", "vbscript", "file"})
NETWORK_APIS = re.compile(r"\b(?:fetch\s*\(|XMLHttpRequest\b|sendBeacon\s*\(|WebSocket\s*\(|EventSource\s*\()", re.I)
REMOTE_CSS = re.compile(r"(?:@import\s+(?:url\s*\()?|url\s*\()\s*['\"]?\s*(?:https?:)?//", re.I)
ZERO_TRACKING = re.compile(
    r"\b(?:no|never|without)\s+(?:(?:third.party|user|any)\s+)?(?:tracking|analytics|cookies)\b"
    r"|\bdoes\s+not\s+(?:set\s+)?cookies\b|\bdoes\s+not\s+track\b",
    re.I,
)
NO_COLLECTION = re.compile(r"\b(?:collect\s+(?:nothing|no\s+data)|nothing\s+is\s+collected|no\s+data\s+(?:is\s+)?collected)\b", re.I)
AMBIGUOUS_LINK_TEXT = frozenset({"click here", "here", "read more", "more", "link"})
URL_ATTRS = {"img": "src", "script": "src", "iframe": "src", "video": "src", "audio": "src", "source": "src", "embed": "src", "object": "data"}


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    file: str
    line: int
    message: str


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    line: int
    name_parts: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return " ".join(" ".join(self.name_parts).split())


class Document(HTMLParser):
    """Small semantic extraction layer; source HTML is never executed."""

    def __init__(self, file: str, html: str):
        super().__init__(convert_charrefs=True)
        self.file = file
        self.elements: list[Element] = []
        self.anchors: list[Element] = []
        self.buttons: list[Element] = []
        self.headings: list[tuple[int, str, int, bool]] = []
        self.ids: list[tuple[str, int]] = []
        self.landmarks: dict[str, list[Element]] = {key: [] for key in ("header", "nav", "main", "footer")}
        self.meta: list[Element] = []
        self.links: list[Element] = []
        self.images: list[Element] = []
        self.forms: list[Element] = []
        self.resources: list[tuple[str, str, int]] = []
        self.inline_scripts: list[tuple[str, int]] = []
        self.css_blocks: list[tuple[str, int]] = []
        self.lang = ""
        self.title = ""
        self.text = ""
        self._html_parts: list[str] = []
        self._title_parts: list[str] = []
        self._title_open = False
        self._body_depth = 0
        self._main_depth = 0
        self._active_anchors: list[Element] = []
        self._active_buttons: list[Element] = []
        self._active_heading: tuple[int, int, bool, list[str]] | None = None
        self._active_capture: tuple[str, int, list[str]] | None = None
        self.feed(html)
        self.close()
        self.text = " ".join(" ".join(self._html_parts).split())
        self.title = " ".join(" ".join(self._title_parts).split())

    def handle_starttag(self, tag: str, raw_attrs: list[tuple[str, str | None]]) -> None:
        attrs = {key.lower(): value or "" for key, value in raw_attrs}
        el = Element(tag, attrs, self.getpos()[0])
        self.elements.append(el)
        if "id" in attrs:
            self.ids.append((attrs["id"], el.line))
        if tag == "title":
            self._title_open = True
        if tag == "html":
            self.lang = attrs.get("lang", "").strip()
        if tag == "body":
            self._body_depth += 1
        if tag == "main":
            self._main_depth += 1
        if tag in self.landmarks:
            self.landmarks[tag].append(el)
        if tag == "meta":
            self.meta.append(el)
        if tag == "link":
            self.links.append(el)
            rel = set(attrs.get("rel", "").lower().split())
            if rel.intersection({"stylesheet", "icon", "preload", "modulepreload", "prefetch", "apple-touch-icon", "manifest"}):
                self.resources.append((tag, attrs.get("href", ""), el.line))
        if tag in URL_ATTRS and (URL_ATTRS[tag] in attrs or tag == "img"):
            self.resources.append((tag, attrs.get(URL_ATTRS[tag], ""), el.line))
        if tag == "video" and attrs.get("poster"):
            self.resources.append(("poster", attrs["poster"], el.line))
        if tag in {"img", "source"} and attrs.get("srcset"):
            # Split by comma; an image URL may contain a query but not a literal comma.
            for candidate in attrs["srcset"].split(","):
                part = candidate.strip().split()
                if part:
                    self.resources.append(("srcset", part[0], el.line))
        if tag == "img":
            self.images.append(el)
            if self._active_anchors and attrs.get("alt", "").strip() and attrs.get("aria-hidden") != "true":
                self._active_anchors[-1].name_parts.append(attrs["alt"])
        if tag == "form":
            self.forms.append(el)
        if tag == "a":
            self.anchors.append(el)
            self._active_anchors.append(el)
        if tag == "button":
            self.buttons.append(el)
            self._active_buttons.append(el)
        if re.fullmatch(r"h[1-6]", tag):
            self._active_heading = (int(tag[1]), el.line, self._main_depth > 0, [])
        if tag in ("script", "style"):
            self._active_capture = (tag, el.line, [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._active_capture is not None:
            self._active_capture[2].append(data)
            return
        if self._body_depth:
            self._html_parts.append(data)
            for anchor in self._active_anchors:
                anchor.name_parts.append(data)
            for button in self._active_buttons:
                button.name_parts.append(data)
            if self._active_heading is not None:
                self._active_heading[3].append(data)
        elif self._title_open:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._active_capture is not None:
            kind, line, parts = self._active_capture
            if kind == tag:
                payload = "".join(parts)
                if kind == "script":
                    self.inline_scripts.append((payload, line))
                else:
                    self.css_blocks.append((payload, line))
                self._active_capture = None
        if tag == "a" and self._active_anchors:
            self._active_anchors.pop()
        if tag == "button" and self._active_buttons:
            self._active_buttons.pop()
        if re.fullmatch(r"h[1-6]", tag) and self._active_heading is not None:
            level, line, in_main, words = self._active_heading
            self.headings.append((level, " ".join(" ".join(words).split()), line, in_main))
            self._active_heading = None
        if tag == "main":
            self._main_depth = max(0, self._main_depth - 1)
        if tag == "body":
            self._body_depth = max(0, self._body_depth - 1)
        if tag == "title":
            self._title_open = False



def _issue(level: str, code: str, doc: Document | str, line: int, message: str) -> Issue:
    return Issue(level, code, doc.file if isinstance(doc, Document) else doc, line, message)


def _rel_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_remote_resource(url: str) -> bool:
    url = url.strip()
    return url.startswith("//") or urlsplit(url).scheme.lower() in {"http", "https"}


def _local_target(root: Path, origin: Path, raw_url: str) -> tuple[Path | None, str, str | None]:
    """Return local path + fragment or a categorized URL error.

    Relative references must remain inside the scanned root. Absolute web URLs,
    mail links and registered custom schemes are handled by the caller.
    """
    url = urlsplit(raw_url)
    if url.scheme or url.netloc:
        return None, "", None
    encoded_path = url.path
    decoded = unquote(encoded_path)
    if "\x00" in decoded or "\\" in decoded or "%" in decoded:
        return None, "", "URL_UNSAFE_PATH"
    if decoded.startswith("/"):
        return None, "", "URL_ABSOLUTE_LOCAL_PATH"
    resolved = (origin.parent / decoded).resolve() if decoded else origin
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None, "", "URL_ESCAPES_ROOT"
    if resolved.is_dir():
        resolved = resolved / "index.html"
    return resolved, unquote(url.fragment), None


def _canonical_values(doc: Document) -> list[str]:
    return [e.attrs.get("href", "").strip() for e in doc.links if "canonical" in e.attrs.get("rel", "").lower().split()]


def _meta_content(doc: Document, name: str) -> list[str]:
    return [el.attrs.get("content", "").strip() for el in doc.meta if el.attrs.get("name", "").lower() == name]


def audit(root: Path, *, allowed_schemes: Sequence[str] = (), canonical_base: str | None = None) -> dict:
    root = Path(root).resolve()
    issues: list[Issue] = []
    if not root.is_dir():
        raise ValueError("The audit root must be a local directory")
    files = sorted(p for p in root.rglob("*.html") if p.is_file() and not p.is_symlink())
    if not files:
        issues.append(_issue("error", "NO_HTML", ".", 0, "No HTML pages found"))
    documents: dict[str, Document] = {}
    for path in files:
        rel = _rel_path(root, path)
        try:
            text = path.read_text(encoding="utf-8-sig")
            documents[rel] = Document(rel, text)
        except (OSError, UnicodeError, ValueError) as exc:
            issues.append(_issue("error", "HTML_UNREADABLE", rel, 0, f"Unable to parse UTF-8 HTML ({type(exc).__name__})"))
    for expected in REQUIRED_PAGES:
        if expected not in documents:
            issues.append(_issue("error", "PAGE_REQUIRED", expected, 0, "Required site page not found"))

    known_schemes = SAFE_SCHEMES.union(s.lower() for s in allowed_schemes)
    id_sets = {rel: {name for name, _ in doc.ids} for rel, doc in documents.items()}
    canon_seen: dict[str, str] = {}
    inferred_base: str | None = canonical_base
    home = documents.get("index.html")
    if inferred_base is None and home is not None:
        candidates = _canonical_values(home)
        if len(candidates) == 1:
            parsed = urlsplit(candidates[0])
            path = parsed.path
            if path.endswith("/index.html"):
                path = path[: -len("index.html")]
            if path.endswith("/"):
                inferred_base = f"{parsed.scheme}://{parsed.netloc}{path}"
    if inferred_base is not None:
        base_parsed = urlsplit(inferred_base)
        if (base_parsed.scheme != "https" or not base_parsed.netloc or not base_parsed.path.endswith("/")
                or base_parsed.username or base_parsed.password or base_parsed.query or base_parsed.fragment):
            if canonical_base is not None:
                raise ValueError("canonical_base must be an HTTPS URL ending in /")
            inferred_base = None

    for rel, doc in documents.items():
        if not doc.lang:
            issues.append(_issue("error", "LANG_MISSING", doc, 1, "The html element needs a nonempty lang attribute"))
        if not doc.title:
            issues.append(_issue("error", "TITLE_MISSING", doc, 1, "A nonempty page title is required"))
        elif len(doc.title) > 70:
            issues.append(_issue("warning", "TITLE_LONG", doc, 1, "Page title exceeds 70 characters"))
        desc = _meta_content(doc, "description")
        if len(desc) != 1 or not desc[0]:
            issues.append(_issue("error", "DESCRIPTION_REQUIRED", doc, 1, "Exactly one nonempty meta description is required"))
        elif len(desc[0]) > 170:
            issues.append(_issue("warning", "DESCRIPTION_LONG", doc, 1, "Meta description exceeds 170 characters"))
        if not any(el.attrs.get("charset", "").lower() in ("utf-8", "utf8") for el in doc.meta):
            issues.append(_issue("error", "CHARSET_MISSING", doc, 1, "Declare UTF-8 charset"))
        if not _meta_content(doc, "viewport"):
            issues.append(_issue("error", "VIEWPORT_MISSING", doc, 1, "A viewport meta tag is required"))
        for meta in doc.meta:
            if meta.attrs.get("http-equiv", "").lower() == "refresh":
                issues.append(_issue("warning", "META_REFRESH_REVIEW", doc, meta.line, "Automatic redirects need an accessibility and data-flow review"))
        for name in ("header", "main", "footer"):
            if len(doc.landmarks[name]) != 1:
                issues.append(_issue("error", "LANDMARK_COUNT", doc, 1, f"Expected exactly one {name} landmark"))
        if not doc.landmarks["nav"]:
            issues.append(_issue("error", "NAV_REQUIRED", doc, 1, "A navigation landmark is required"))
        for nav in doc.landmarks["nav"]:
            if not nav.attrs.get("aria-label", "").strip() and not nav.attrs.get("aria-labelledby", "").strip():
                issues.append(_issue("warning", "NAV_NAME", doc, nav.line, "Give the navigation landmark an accessible name"))
        h1 = [h for h in doc.headings if h[0] == 1 and h[3]]
        if len(h1) != 1 or not h1[0][1]:
            issues.append(_issue("error", "H1_REQUIRED", doc, 1, "Exactly one nonempty h1 is required inside main"))
        previous = 0
        for level, _text, line, _main in doc.headings:
            if previous and level > previous + 1:
                issues.append(_issue("warning", "HEADING_SKIP", doc, line, "Heading level jumps more than one level"))
            previous = level
        seen_ids: set[str] = set()
        for identifier, line in doc.ids:
            if not identifier or identifier in seen_ids:
                issues.append(_issue("error", "DUPLICATE_OR_EMPTY_ID", doc, line, "Element IDs must be unique and nonempty"))
            seen_ids.add(identifier)
        for el in doc.elements:
            labelled_by = el.attrs.get("aria-labelledby", "").split()
            if labelled_by and any(ref not in seen_ids for ref in labelled_by):
                issues.append(_issue("error", "ARIA_REF_MISSING", doc, el.line, "aria-labelledby points to an unknown ID"))
        for img in doc.images:
            if "alt" not in img.attrs:
                issues.append(_issue("error", "IMG_ALT_MISSING", doc, img.line, "Image needs an alt attribute; alt='' is valid for decorative images"))
        for button in doc.buttons:
            if not (button.name or button.attrs.get("aria-label", "").strip() or button.attrs.get("aria-labelledby", "").strip()):
                issues.append(_issue("error", "BUTTON_NAME_MISSING", doc, button.line, "Button needs an accessible name"))
        canonical = _canonical_values(doc)
        if len(canonical) != 1:
            issues.append(_issue("error", "CANONICAL_COUNT", doc, 1, "Exactly one canonical link is required"))
        else:
            value = canonical[0]
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
                issues.append(_issue("error", "CANONICAL_INVALID", doc, 1, "Canonical URL must be absolute HTTPS without credentials, query or fragment"))
            else:
                normalized = value.rstrip("/")
                if normalized in canon_seen:
                    issues.append(_issue("error", "CANONICAL_DUPLICATE", doc, 1, "Canonical URL is reused by another page"))
                canon_seen[normalized] = rel
                if inferred_base is not None:
                    expected = inferred_base + ("" if rel == "index.html" else rel)
                    alt_index = inferred_base + "index.html" if rel == "index.html" else expected
                    if value not in {expected, alt_index}:
                        issues.append(_issue("error", "CANONICAL_PATH", doc, 1, "Canonical URL does not match page location and configured site base"))
                elif rel != "index.html" and not unquote(parsed.path).endswith("/" + rel):
                    issues.append(_issue("error", "CANONICAL_PATH", doc, 1, "Canonical path does not match page file path"))
        for a in doc.anchors:
            href = a.attrs.get("href", "").strip()
            label = a.attrs.get("aria-label", "").strip() or a.name
            if not label and a.attrs.get("aria-labelledby", "").strip():
                label = "referenced label"
            if not label:
                issues.append(_issue("error", "LINK_NAME_MISSING", doc, a.line, "Link needs discernible text, image alt, or an accessible name"))
            elif label.strip().lower() in AMBIGUOUS_LINK_TEXT:
                issues.append(_issue("warning", "LINK_NAME_AMBIGUOUS", doc, a.line, "Link text does not describe the destination"))
            if not href:
                issues.append(_issue("error", "LINK_HREF_MISSING", doc, a.line, "Link needs a nonempty href"))
                continue
            parsed = urlsplit(href)
            scheme = parsed.scheme.lower()
            if scheme in DANGEROUS_SCHEMES:
                issues.append(_issue("error", "LINK_DANGEROUS_SCHEME", doc, a.line, "Script, file or embedded-data link scheme is forbidden"))
                continue
            if scheme and scheme not in known_schemes:
                issues.append(_issue("warning", "LINK_SCHEME_REVIEW", doc, a.line, "Unregistered app link scheme requires review; allow explicitly if expected"))
                continue
            if scheme == "http":
                issues.append(_issue("warning", "LINK_HTTP", doc, a.line, "Prefer HTTPS for external links"))
            same_site_url: str | None = None
            if inferred_base is not None and scheme == "https":
                base = urlsplit(inferred_base)
                if parsed.netloc.lower() == base.netloc.lower() and parsed.path.startswith(base.path):
                    relative = parsed.path[len(base.path):] or "index.html"
                    same_site_url = relative + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")
            if scheme or parsed.netloc:
                if a.attrs.get("target", "").lower() == "_blank" and "noopener" not in a.attrs.get("rel", "").lower().split():
                    issues.append(_issue("warning", "LINK_NOOPENER", doc, a.line, "New-tab external link should specify rel=noopener"))
                if same_site_url is None:
                    continue
            target, fragment, reason = _local_target(root, root / rel, same_site_url or href)
            if reason:
                issues.append(_issue("error", reason, doc, a.line, "Local link has unsafe or unsupported path"))
            elif target is not None:
                if not target.is_file():
                    issues.append(_issue("error", "LINK_BROKEN", doc, a.line, "Relative link destination does not exist"))
                elif fragment:
                    target_rel = _rel_path(root, target)
                    if target_rel in id_sets and fragment not in id_sets[target_rel]:
                        issues.append(_issue("error", "LINK_FRAGMENT_MISSING", doc, a.line, "Fragment target ID does not exist"))
        for tag, resource_url, line in doc.resources:
            if not resource_url:
                if tag not in ("video", "audio", "source"):
                    issues.append(_issue("error", "ASSET_SRC_MISSING", doc, line, "Asset element requires a URL"))
                continue
            if resource_url.startswith("data:"):
                continue
            parsed = urlsplit(resource_url)
            if parsed.scheme not in {"", "http", "https"} and not resource_url.startswith("//"):
                issues.append(_issue("warning", "ASSET_SCHEME_REVIEW", doc, line, "Nonstandard asset URL requires review"))
                continue
            if _is_remote_resource(resource_url):
                issues.append(_issue("warning", "REMOTE_EMBED", doc, line, "Embedded remote asset may expose request metadata"))
                continue
            target, _fragment, reason = _local_target(root, root / rel, resource_url)
            if reason:
                issues.append(_issue("error", reason, doc, line, "Asset path is unsafe or unsupported"))
            elif target is not None and not target.is_file():
                issues.append(_issue("error", "ASSET_BROKEN", doc, line, "Local asset does not exist"))
        for form in doc.forms:
            action = form.attrs.get("action", "").strip()
            if _is_remote_resource(action):
                issues.append(_issue("warning", "REMOTE_FORM_REVIEW", doc, form.line, "Remote form submission needs privacy disclosure review"))
        for code, line in doc.inline_scripts:
            if NETWORK_APIS.search(code):
                issues.append(_issue("warning", "NETWORK_API_REVIEW", doc, line, "Inline network API use needs manual data-flow review"))
        for css, line in doc.css_blocks:
            if REMOTE_CSS.search(css):
                issues.append(_issue("warning", "REMOTE_CSS", doc, line, "CSS references an externally hosted resource"))
        for el in doc.elements:
            if el.tag == "style":
                continue
            for key in ("style",):
                if REMOTE_CSS.search(el.attrs.get(key, "")):
                    issues.append(_issue("warning", "REMOTE_CSS", doc, el.line, "Inline style references an externally hosted resource"))

    # Cross-page policies are heuristic integrity checks, not legal certification.
    privacy = documents.get("privacy-policy.html")
    terms = documents.get("terms-of-use.html")
    if privacy:
        body = privacy.text.lower()
        for token in ("collect", "tracking", "cookies", "contact"):
            if token not in body:
                issues.append(_issue("error", "PRIVACY_TOPIC_MISSING", privacy, 1, f"Privacy disclosure should address {token}"))
        zero_claim = bool(ZERO_TRACKING.search(body) or NO_COLLECTION.search(body))
        if zero_claim:
            for rel, doc in documents.items():
                if (any(_is_remote_resource(url) for _tag, url, _line in doc.resources)
                        or any(REMOTE_CSS.search(css) for css, _line in doc.css_blocks)
                        or any(REMOTE_CSS.search(el.attrs.get("style", "")) for el in doc.elements)):
                    issues.append(_issue("error", "PRIVACY_REMOTE_ASSET", rel, 1, "Zero-tracking or collection claim requires review of externally loaded assets"))
                if any(_is_remote_resource(form.attrs.get("action", "")) for form in doc.forms):
                    issues.append(_issue("warning", "PRIVACY_REMOTE_FORM", rel, 1, "Zero-tracking or collection claim requires review of remote form submissions"))
        if any(re.search(r"\b(?:photos?|pictures?|images?)\b", doc.text, re.I) and re.search(r"\b(?:stay|local|no uploads?|never uploaded)\b", doc.text, re.I) for doc in documents.values()):
            if not re.search(r"\b(?:photos?|pictures?|images?|device)\b", body):
                issues.append(_issue("error", "PRIVACY_MEDIA_DISCLOSURE", privacy, 1, "Privacy page should address claimed local media handling"))
    if terms:
        lower = terms.text.lower()
        for topic, pattern in (
            ("contact", r"\bcontact\b"),
            ("usage rights", r"\b(?:own|rights|license|licensed)\b"),
        ):
            if not re.search(pattern, lower):
                issues.append(_issue("error", "TERMS_TOPIC_MISSING", terms, 1, f"Terms page should address {topic}"))
        selling = any(re.search(r"\b(?:subscription|auto.renewing|paid plan)\b", doc.text, re.I) for doc in documents.values())
        if selling:
            for topic, pattern in (
                ("renewal", r"\brenew"),
                ("cancellation", r"\bcancel"),
                ("refunds", r"\brefund"),
            ):
                if not re.search(pattern, lower):
                    issues.append(_issue("error", "SUBSCRIPTION_TERMS_GAP", terms, 1, f"Subscription terms should address {topic}"))
    for rel, doc in documents.items():
        targets: set[str] = set()
        for anchor in doc.anchors:
            href = anchor.attrs.get("href", "")
            path = unquote(urlsplit(href).path)
            targets.add(path.lstrip("./"))
            if inferred_base is not None and href.startswith(inferred_base):
                targets.add(unquote(urlsplit(href).path[len(urlsplit(inferred_base).path):]).lstrip("./"))
        for page in ("privacy-policy.html", "terms-of-use.html", "support.html"):
            if page in documents and page != rel and page not in targets:
                issues.append(_issue("warning", "POLICY_LINK_MISSING", doc, 1, f"No relative navigation link to {page}"))
    issues.sort(key=lambda i: (i.file, i.line, i.level != "error", i.code, i.message))
    errors = sum(i.level == "error" for i in issues)
    warnings = sum(i.level == "warning" for i in issues)
    return {
        "schema_version": 1,
        "offline": True,
        "legal_review_required": True,
        "summary": {"pages": len(documents), "errors": errors, "warnings": warnings, "passed": errors == 0},
        "pages": sorted(documents),
        "issues": [asdict(i) for i in issues],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="Local website directory")
    parser.add_argument("--output", type=Path, help="Write deterministic JSON findings to this local path")
    parser.add_argument("--canonical-base", help="Canonical HTTPS base ending in /")
    parser.add_argument("--allow-scheme", action="append", default=[], help="Explicit first-party deep-link scheme (repeatable)")
    parser.add_argument("--strict-warnings", action="store_true", help="Treat warnings as a failing exit status")
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, allowed_schemes=args.allow_scheme, canonical_base=args.canonical_base)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for issue in result["issues"]:
        print(f"{issue['level'].upper()} {issue['code']} {issue['file']}:{issue['line']} {issue['message']}")
    summary = result["summary"]
    ok = summary["passed"] and (not args.strict_warnings or summary["warnings"] == 0)
    print(f"AUDIT {'PASS' if ok else 'FAIL'} pages={summary['pages']} errors={summary['errors']} warnings={summary['warnings']}")
    print(f"ALL={'true' if ok else 'false'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
