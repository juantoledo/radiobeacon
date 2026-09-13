# Changelog

All notable changes to radiobeacon are recorded here, newest first. One
unified version covers the whole project (`data-adapters`, `dispatcher`,
`actions`, `beacon`, `ui`), not a version per package. Entries are added by
`./release.sh`.

## v0.4.2 — 2026-09-13

- Remove dockerized UI deployment option
- Add GitHub badges, MIT license, and README screenshots

## v0.4.1 — 2026-09-13

- Add SvxLink + Direwolf install guide, automated installer, and About page links

## v0.4.0 — 2026-09-12

- Rewrite README as product-facing, move technical detail to documentation/
- Ui / Readme Improvements
- Show on-air glow border and badge on every page, not just dashboard
- Added eye candy workflow to the dashboard
- Added responsive workflow view
- General audit improvements

## v0.3.0 — 2026-09-12

First tagged release since the versioning scheme was introduced (the prior
`v0.1.0`/`v0.2.0` tags point into history that predates a squash and are no
longer reachable from `main`). Includes everything up to this point:

- Improved adapter capabilities
- Improved wizard
- Threading optimizations
- Improved error management
- Added the first-run setup wizard
- UI improvements
- Marketing pass on the About page
- Added project versioning: a single `VERSION` file, `adapters.__version__`,
  version shown in the UI footer, `release.sh`, and a GitHub Actions
  workflow that publishes a GitHub Release on tag push
