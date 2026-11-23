# Offline static site audit (private draft)

Read-only HTML, link, accessibility, metadata and policy-claim linting. The tool does not run JavaScript, fetch resources, phone home, open browsers, inspect payment systems, modify web pages, or deploy.

## Requirements

- Python 3.10+ for the tool (standard library only).
- `pytest` for test execution only; it is **not** shipped to the website.

## Run

From the site root after applying the supplied patch:

```sh
python tools/site_audit.py --root . --output local-audit.json
python -m pytest -q tests/test_site_audit.py
```

For a first-party app deep link and a known public canonical root, explicitly supply:

```sh
python tools/site_audit.py --root . --allow-scheme yourscheme \
  --canonical-base https://example.invalid/app/ --output local-audit.json
```

The illustrative reserved hostname above is not a deployment destination. Do **not** paste it into production configuration. Use the project's actual canonical HTTPS base or omit `--canonical-base` to infer it from the homepage.

Use `--strict-warnings` to fail the CLI on warnings as well as errors. Exit code 0 means no errors (or no warnings in strict mode); exit code 1 means failed checks. A console `ALL=true|false` marker is emitted. Output JSON is deterministic and includes issue codes, file names, line numbers, page counts and `legal_review_required: true`.

## Coverage and boundaries

Audited automatically:

- HTML language, UTF-8/viewport/description/title, navigation/header/main/footer, main heading, heading-order warnings and ID uniqueness.
- Accessible link/button names, image `alt` (including valid decorative empty `alt`), navigation label warnings, missing ARIA ID references, broken local links, fragments and assets, and same-site HTTPS absolute links.
- HTTPS canonical metadata, consistent site path, duplicate canonical URLs, meta-refresh warnings, new-tab `noopener`, insecure links, forbidden executable/data link schemes and unreviewed custom schemes.
- Required home/privacy/terms/support pages; cross-links; privacy topic disclosures; subscription renewal/cancellation/refund terms; obvious remote embeds, remote forms, network API usage and CSS remote-resource references.
- Conservatively raises an error for externally loaded assets alongside zero-tracking/zero-collection claims. A remote asset is **not proof of tracking**; it does require reconciling request-metadata exposure against the policy.

Not established automatically:

- Full accessibility compliance (keyboard interaction, rendered contrast, reading order, screen-reader behavior), search-engine ranking, factual claims about app internals, data processors, payments, third-party APIs, server access logs, or sufficiency/enforceability of legal language.
- Dynamic JavaScript behavior (including computed URL construction), browser extension behavior, external link uptime, responsive rendering and binary asset provenance.

**Manual legal, privacy, accessibility and product-behavior reviews remain required before any publication.** Avoid claiming zero tracking merely because this static check passes.

## Non-game feature flag note

This is a command-line audit tool for static pages, not a game-facing runtime system. The mandatory game feature-flag, kill-switch pixel-parity and game-test-hub paths are not applicable. Existing site files are not modified by the supplied patch.
