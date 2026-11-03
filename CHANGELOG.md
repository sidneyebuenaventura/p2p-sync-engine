# Changelog

All notable changes to this project are written down here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-03

First tagged release: the building blocks, before any end-to-end flows.

### Added

- Core: typed settings from environment variables and `.env`, structured
  (JSON or plain) logging with per-run context, `APIError` with redacted
  request details, an HTTP client with retries on idempotent calls only, and
  secret lookup from the environment or Azure Key Vault.
- Sync: a generic `RecordMatcher` and a `SyncEngine` that decides create,
  update, skip or conflict per record, supports dry runs, and keeps going when
  a single record fails.
- Scheduler: cron-style job decorators with timezone support.
- Procurement connector: invoice, supplier and payment models with amount
  checks, OAuth client-credentials auth with token caching, and an API client
  that skips (and reports) records it cannot parse.
- ERP connector: vendor and AP invoice models with separate draft and read
  shapes, and an API client that checks the fiscal period before posting.
- CI: ruff, mypy `--strict`, tests on Python 3.12 and 3.13, and a gitleaks scan
  over the full history.

### Changed

- License is now "all rights reserved" instead of MIT.

[Unreleased]: https://github.com/sidneyebuenaventura/p2p-sync-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sidneyebuenaventura/p2p-sync-engine/releases/tag/v0.1.0
