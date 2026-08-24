---
name: git-release-workflow
description: Prepare commits and releases for this repository. Use when committing changes, drafting commit messages, preparing releases or version bumps, updating CHANGELOG.md or pyproject.toml, tagging versions, or when the user mentions Conventional Commits or release prep.
---

# Git Release Workflow

Draft Conventional Commits and keep `CHANGELOG.md` and `pyproject.toml` in sync.

## Conventional Commits

Format: `<type>(<optional scope>): <subject>`

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`, `revert`.

Rules:

- Subject: imperative mood, lowercase first word, no trailing period, ≤ 72 chars.
- Breaking changes append `!` after the type/scope (e.g. `feat!:`) and include a
  `BREAKING CHANGE:` footer describing the impact and migration.
- Body (optional): blank line after subject; explain *why*, wrap at 72 chars.
- Reference issues with `Closes #123` / `Fixes #123` footers when applicable.
- Draft the message and show it to the user before committing unless they already
  provided exact wording. Never commit without explicit request.

## Versioning

Semver, derived from changes since the last tag:

- `feat:` → minor bump; `fix:` → patch bump; `BREAKING CHANGE` → major bump;
  `docs:`/`chore:`/etc. alone → typically no version bump unless part of a release.

Update **both** files together so they never disagree:

1. `pyproject.toml`: bump `version` under `[project]`.
2. `CHANGELOG.md`: add an entry at the top following the existing format of the
   file (read it first). Group items as Added / Changed / Fixed / Removed /
   Security, one bullet per user-visible change, sourced from the commits being
   released.

## Release steps

1. Confirm quality gates pass (ruff + pytest via the test-driven-development skill).
2. Read `git status` and stage only intended files; inspect the diff for secrets
   before staging.
3. Update `CHANGELOG.md` + `pyproject.toml` in a dedicated `chore(release): vX.Y.Z`
   commit (or fold into the feature commit if the user prefers).
4. Propose the annotated tag command: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
   Create tags/pushes only when the user confirms.
