# Versioning Policy

This repository follows the [SemVer 2.0.0](https://semver.org/lang/zh-CN/) version format: `MAJOR.MINOR.PATCH`.

## Version Source

- The `VERSION` file is the single source of truth for the version number; all version numbers are determined by its content.
- Commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/) specification; the prefix determines the version bump rule:

| Commit prefix | Version impact |
| --- | --- |
| `fix:` | PATCH increment |
| `feat:` | MINOR increment |
| `perf:` / `refactor:` / `docs:` / `test:` / `chore:` | No separate bump; merged into the next release |
| Contains `BREAKING CHANGE` or scope followed by `!:` | MAJOR increment |

- All commit messages and tag annotations MUST be written in English only.

- Breaking changes (MAJOR) are expressed as: a `BREAKING CHANGE:` description in the commit body, or a `!:` after the scope in the subject, e.g. `feat(api)!: remove legacy endpoint`.

## Release

1. On the `main` branch, update the `VERSION` file to the target version number.
2. Update `CHANGELOG.md`, adding the corresponding version entry in [Keep a Changelog](https://keepachangelog.com/) format.
3. On `main`, create an **annotated tag**: `git tag -a vX.Y.Z -m "vX.Y.Z: <summary>"`.
4. Push branch and tag: `git push origin main && git push origin vX.Y.Z`.

## Pre-1.0 Stage

- Before reaching `1.0.0` (pre-1.0 stage), `MINOR` versions may include breaking changes without bumping to `MAJOR`.
- After reaching `1.0.0`, breaking changes MUST bump `MAJOR`.
