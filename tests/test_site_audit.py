# SPDX-License-Identifier: MIT
"""Deterministic offline proof cases for the static site audit."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from site_audit import audit  # noqa: E402


BASE = "https://example.invalid/app/"
PAGES = ("index.html", "privacy-policy.html", "terms-of-use.html", "support.html")
LABELS = ("Home", "Privacy policy", "Terms of use", "Support")
CONTENTS = {
    "index.html": "<h1>Make pictures locally</h1><p>Photos stay local. This product offers an optional subscription.</p>",
    "privacy-policy.html": "<h1>Privacy policy</h1><p>We collect no data. No analytics, no tracking, no cookies. Photos stay on your device. Contact support for questions.</p>",
    "terms-of-use.html": "<h1>Terms of use</h1><p>You own your work. Contact support. A subscription will renew. You can cancel. Refund questions may be sent to support.</p>",
    "support.html": "<h1>Support</h1><p>Contact us about the local editor.</p>",
}


def markup(name: str, content: str | None = None) -> str:
    body = content if content is not None else CONTENTS[name]
    dest = BASE if name == "index.html" else BASE + name
    nav = "".join(f'<a href="{page}">{label}</a>' for page, label in zip(PAGES, LABELS))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} | Local editor</title><meta name="description" content="A locally processed image editor with clear product and policy pages.">
<link rel="canonical" href="{dest}"></head><body>
<header><a href="index.html"><img src="logo.svg" alt="">Local editor</a><nav aria-label="Primary navigation">{nav}</nav></header>
<main>{body}</main><footer><p>Contact support for questions.</p></footer>
</body></html>'''


@pytest.fixture
def site(tmp_path: Path) -> Path:
    for name in PAGES:
        (tmp_path / name).write_text(markup(name), encoding="utf-8")
    (tmp_path / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    return tmp_path


def change(site: Path, page: str, needle: str, replacement: str) -> None:
    p = site / page
    content = p.read_text(encoding="utf-8")
    assert needle in content, needle
    p.write_text(content.replace(needle, replacement, 1), encoding="utf-8")


def codes(site: Path) -> set[str]:
    return {entry["code"] for entry in audit(site)["issues"]}


def test_clean_fixture_passes_and_is_deterministic(site: Path) -> None:
    a = audit(site)
    assert a == audit(site)
    assert a["summary"] == {"pages": 4, "errors": 0, "warnings": 0, "passed": True}
    assert a["offline"] is True and a["legal_review_required"] is True


def test_html_is_not_executed(site: Path) -> None:
    change(site, "index.html", "</main>", '<script>throw Error("should not run")</script></main>')
    assert audit(site)["summary"]["passed"]


def test_no_html_files_is_error(tmp_path: Path) -> None:
    assert "NO_HTML" in codes(tmp_path)


def test_missing_required_page(site: Path) -> None:
    (site / "support.html").unlink()
    assert "PAGE_REQUIRED" in codes(site)
    assert "LINK_BROKEN" in codes(site)


@pytest.mark.parametrize(
    ("needle", "replacement", "code"),
    [
        ('href="support.html"', 'href="missing.html"', "LINK_BROKEN"),
        ('href="support.html"', 'href="support.html#ghost"', "LINK_FRAGMENT_MISSING"),
        ('href="support.html"', 'href="../private.html"', "URL_ESCAPES_ROOT"),
        ('href="support.html"', 'href="/private.html"', "URL_ABSOLUTE_LOCAL_PATH"),
        ('<img src="logo.svg" alt="">', '<img src="logo.svg">', "IMG_ALT_MISSING"),
        ('src="logo.svg"', 'src="missing.svg"', "ASSET_BROKEN"),
        ('<main>', '<div>', "LANDMARK_COUNT"),
        ('<h1>Make pictures locally</h1>', '<h2>Make pictures locally</h2>', "H1_REQUIRED"),
        ('lang="en"', 'lang=""', "LANG_MISSING"),
        ('<title>index.html | Local editor</title>', '<title></title>', "TITLE_MISSING"),
        ('name="description"', 'name="other"', "DESCRIPTION_REQUIRED"),
        ('<meta charset="utf-8">', '', "CHARSET_MISSING"),
        ('name="viewport"', 'name="other"', "VIEWPORT_MISSING"),
        ('rel="canonical"', 'rel="alternate"', "CANONICAL_COUNT"),
        ('href="https://example.invalid/app/"', 'href="http://example.invalid/app/"', "CANONICAL_INVALID"),
        ('href="https://example.invalid/app/"', 'href="https://example.invalid/wrong/"', "CANONICAL_PATH"),
    ],
)
def test_structural_errors(site: Path, needle: str, replacement: str, code: str) -> None:
    change(site, "index.html", needle, replacement)
    assert code in codes(site)


def test_canonical_duplicate(site: Path) -> None:
    change(site, "privacy-policy.html", BASE + "privacy-policy.html", BASE)
    assert "CANONICAL_DUPLICATE" in codes(site)


def test_nameless_link(site: Path) -> None:
    change(site, "index.html", "</main>", '<a href="support.html"><img src="logo.svg" alt=""></a></main>')
    assert "LINK_NAME_MISSING" in codes(site)


def test_image_alt_is_valid_when_decorative(site: Path) -> None:
    assert "IMG_ALT_MISSING" not in codes(site)


def test_button_requires_label(site: Path) -> None:
    change(site, "index.html", "</main>", '<button type="button"><svg></svg></button></main>')
    assert "BUTTON_NAME_MISSING" in codes(site)


def test_image_only_link_with_descriptive_alt_passes(site: Path) -> None:
    change(site, "index.html", "</main>", '<a href="support.html"><img alt="Contact support" src="logo.svg"></a></main>')
    assert "LINK_NAME_MISSING" not in codes(site)


def test_fragment_target_passes(site: Path) -> None:
    change(site, "support.html", '<h1>Support</h1>', '<h1 id="answers">Support</h1>')
    change(site, "index.html", "</main>", '<p><a href="support.html#answers">Read answers</a></p></main>')
    assert "LINK_FRAGMENT_MISSING" not in codes(site)


def test_duplicate_id_fails(site: Path) -> None:
    change(site, "index.html", "</main>", '<p id="same">One</p><p id="same">Two</p></main>')
    assert "DUPLICATE_OR_EMPTY_ID" in codes(site)


def test_aria_label_reference_fails(site: Path) -> None:
    change(site, "index.html", "<main>", '<main aria-labelledby="nonexistent">')
    assert "ARIA_REF_MISSING" in codes(site)


def test_nav_accessible_name_warning(site: Path) -> None:
    change(site, "index.html", 'aria-label="Primary navigation"', '')
    assert "NAV_NAME" in codes(site)


def test_heading_jump_warning(site: Path) -> None:
    change(site, "index.html", "</main>", "<h4>Unexpected jump</h4></main>")
    assert "HEADING_SKIP" in codes(site)


def test_external_link_is_not_embedded_resource(site: Path) -> None:
    change(site, "index.html", "</main>", '<p><a href="https://external.invalid/help">Visit documentation</a></p></main>')
    assert "PRIVACY_REMOTE_ASSET" not in codes(site)
    assert "REMOTE_EMBED" not in codes(site)


@pytest.mark.parametrize(
    "snippet",
    [
        '<img src="https://cdn.invalid/pixel.gif" alt="">',
        '<script src="https://cdn.invalid/site.js"></script>',
        '<link rel="stylesheet" href="https://cdn.invalid/site.css">',
        '<style>body{background:url(https://cdn.invalid/image.png)}</style>',
        '<div style="background:url(https://cdn.invalid/image.png)"></div>',
    ],
)
def test_zero_tracking_claim_detects_remote_embeds(site: Path, snippet: str) -> None:
    change(site, "index.html", "</main>", snippet + "</main>")
    assert "PRIVACY_REMOTE_ASSET" in codes(site) or "REMOTE_CSS" in codes(site)


def test_network_api_is_flagged_for_review_not_assumed_tracking(site: Path) -> None:
    change(site, "index.html", "</main>", '<script>fetch("/asset.json")</script></main>')
    result = audit(site)
    assert "NETWORK_API_REVIEW" in {x["code"] for x in result["issues"]}
    assert result["summary"]["passed"]


def test_remote_form_flags_manual_review(site: Path) -> None:
    change(site, "index.html", "</main>", '<form action="https://forms.invalid/process"><button>Send</button></form></main>')
    assert "PRIVACY_REMOTE_FORM" in codes(site)


def test_missing_tracking_disclosure(site: Path) -> None:
    change(site, "privacy-policy.html", "no tracking", "private use")
    assert "PRIVACY_TOPIC_MISSING" in codes(site)


def test_subscription_terms_are_checked(site: Path) -> None:
    change(site, "terms-of-use.html", "will renew", "continues")
    assert "SUBSCRIPTION_TERMS_GAP" in codes(site)


def test_deep_link_scheme_is_opt_in(site: Path) -> None:
    change(site, "index.html", "</main>", '<a href="myapp://open">Open editor</a></main>')
    assert "LINK_SCHEME_REVIEW" in codes(site)
    allowed = audit(site, allowed_schemes=["myapp"])
    assert "LINK_SCHEME_REVIEW" not in {issue["code"] for issue in allowed["issues"]}


def test_http_link_gets_warning(site: Path) -> None:
    change(site, "index.html", "</main>", '<a href="http://example.invalid/info">Get more information</a></main>')
    assert "LINK_HTTP" in codes(site)


def test_new_tab_rel_noopener_warning(site: Path) -> None:
    change(site, "index.html", "</main>", '<a target="_blank" href="https://example.invalid/info">Get information</a></main>')
    assert "LINK_NOOPENER" in codes(site)


def test_ambiguous_anchor_warning(site: Path) -> None:
    change(site, "index.html", "</main>", '<a href="support.html">Click here</a></main>')
    assert "LINK_NAME_AMBIGUOUS" in codes(site)


def test_json_cli_is_offline_and_strict_warnings_optional(site: Path, tmp_path: Path) -> None:
    tool = Path(__file__).resolve().parents[1] / "tools" / "site_audit.py"
    output = tmp_path.parent / "audit_result.json"
    command = [sys.executable, str(tool), "--root", str(site), "--output", str(output)]
    run = subprocess.run(command, capture_output=True, text=True, check=False)
    assert run.returncode == 0 and "ALL=true" in run.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["passed"]
    change(site, "index.html", "</main>", '<a href="myapp://open">Open editor</a></main>')
    warn = subprocess.run(command, capture_output=True, text=True, check=False)
    strict = subprocess.run(command + ["--strict-warnings"], capture_output=True, text=True, check=False)
    assert warn.returncode == 0 and strict.returncode == 1
    assert "ALL=false" in strict.stdout


def test_invalid_explicit_base_rejected(site: Path) -> None:
    with pytest.raises(ValueError):
        audit(site, canonical_base="http://example.invalid/")


def test_absolute_same_site_broken_link(site: Path) -> None:
    change(site, "index.html", "</main>", f'<a href="{BASE}missing.html">Missing local page</a></main>')
    assert "LINK_BROKEN" in codes(site)


def test_absolute_same_site_fragment_is_checked(site: Path) -> None:
    change(site, "index.html", "</main>", f'<a href="{BASE}support.html#not-found">Read support answer</a></main>')
    assert "LINK_FRAGMENT_MISSING" in codes(site)


def test_external_same_path_different_host_is_not_local(site: Path) -> None:
    change(site, "index.html", "</main>", '<a href="https://unrelated.invalid/app/missing.html">Other help</a></main>')
    assert "LINK_BROKEN" not in codes(site)


def test_meta_refresh_needs_review(site: Path) -> None:
    change(site, "index.html", "</head>", '<meta http-equiv="refresh" content="5;url=support.html"></head>')
    assert "META_REFRESH_REVIEW" in codes(site)


def test_nested_asset_css_zero_tracking_error(site: Path) -> None:
    change(site, "index.html", "</main>", '<section style="background:url(https://static.invalid/image.png)"></section></main>')
    assert "PRIVACY_REMOTE_ASSET" in codes(site)


@pytest.mark.parametrize("scheme", ["javascript", "data", "vbscript", "file"])
def test_dangerous_link_scheme_fails_even_if_allowed(site: Path, scheme: str) -> None:
    change(site, "index.html", "</main>", f'<a href="{scheme}:payload">Unsafe target</a></main>')
    result = audit(site, allowed_schemes=[scheme])
    assert "LINK_DANGEROUS_SCHEME" in {issue["code"] for issue in result["issues"]}
    assert not result["summary"]["passed"]
