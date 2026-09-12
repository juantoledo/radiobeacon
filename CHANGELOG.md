# Changelog

All notable changes to radiobeacon are recorded here, newest first. One
unified version covers the whole project (`data-adapters`, `dispatcher`,
`actions`, `beacon`, `ui`), not a version per package. Entries are added by
`./release.sh`.

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
