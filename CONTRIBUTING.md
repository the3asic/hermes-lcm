# Contributing to hermes-lcm

Thanks for contributing.

This project is small, review-driven, and correctness-first. Keep changes scoped, tested, and easy to reason about.

## Fork release policy

For [`the3asic/hermes-lcm`](https://github.com/the3asic/hermes-lcm), `main` is
the official release line. A merged commit becomes a verified production version
only after controlled deployment and real-conversation acceptance with a
compatible Hermes Core commit. Record the merged and deployed LCM SHAs, the Core
SHA, and the acceptance result; merging or successful startup alone is not
production acceptance.

Develop each independent change on a focused branch and submit a PR to `main`.
GitHub branch protection requires PRs, applies to administrators, and prohibits
force pushes and branch deletion. Required status checks are not configured:
reviewers must inspect the relevant validation results. Failed, skipped,
cancelled, or missing checks must not be reported as passing. Explain validation
limits, and resolve real failures in affected runtime paths before deployment.
Document any pre-existing failures separately with evidence.

Use validation proportionate to the change. Runtime changes require relevant
tests and the existing release checks below. Documentation-only changes can use
`git diff --check` and link verification without rerunning pytest. Hotfixes follow
the same review, validation, controlled deployment, and acceptance gates.
Documentation-only or identical-runtime history changes do not require a restart;
prove the runtime files match and retain the actual process startup identity.

Review upstream changes through focused PRs instead of blindly synchronizing
upstream `main`. Before deployment, inspect the actual production checkout and
runtime, preserve a known rollback commit and verified recovery material, and
deploy the exact reviewed merge commit through the existing controlled procedure.
Normal rollback changes code only, retaining the live databases and newer
messages; never automatically replace live history with an older backup.
For a documented downgrade that cannot open the newer database safely, do not
start incompatible code on the live file. Preserve verified, consistent snapshots
of the current database and its newer history and payloads first. Restore the
pre-upgrade backup into a separate offline candidate, verify its integrity and
compatibility, and define how newer history will remain available. Any production
database replacement or loss of active history needs separate explicit approval;
a routine code rollback does not authorize it. See the
[version-specific upgrade and recovery guidance](docs/operator-guide.md#upgrade-from-v0200-or-v0210-rc2-to-v100-rc1).

This policy uses the existing tests, release workflow, and deployment process;
it adds no services, dependencies, or automatic deployment.

## Workflow

Preferred flow:

1. Open or reference an issue when the change affects behavior, architecture, or public tooling.
2. Create a focused branch from `main`.
3. Add or update tests with the change.
4. Run local validation before opening the PR.
5. Open a PR with a clear summary, rationale, and validation section.

Typical branch names:

- `fix/...`
- `feat/...`
- `docs/...`
- `refactor/...`
- `test/...`

## Issues

Use the GitHub issue forms for:

- bugs
- behavior regressions
- architectural direction
- follow-up work that should not be buried in PR comments

Choose the bug/regression form for broken behavior, and the feature/design form for new behavior or architecture proposals.

When filing a bug, include:

- expected behavior
- actual behavior
- minimal repro steps
- relevant logs, stack traces, or failing tests
- version / branch context when relevant

If a report is speculative, say so. If it is directional rather than a concrete bug, label it clearly in the issue body.

## Commits

Prefer clear, conventional-style subjects:

- `fix: ...`
- `feat: ...`
- `docs: ...`
- `refactor: ...`
- `test: ...`

Keep commits focused. Avoid mixing unrelated cleanup into the same change.

## Pull Requests

Open small PRs when possible. Large PRs are harder to review and easier to get wrong.

PR titles should be descriptive and usually follow the same style as commit subjects.

PR bodies should use this template. It is mirrored in `.github/PULL_REQUEST_TEMPLATE.md` so GitHub pre-fills new PRs.

```md
## Summary
-

## Why
-

## Validation
- [ ] Focused validation: `<command>` -> `<result>`
- [ ] Default validation:
  - [ ] `pytest tests/test_lcm_core.py tests/test_lcm_engine.py tests/test_packaging_install.py -q`
  - [ ] `pytest -q`
  - [ ] `scripts/validate_release.sh --full --keep-going --output /tmp/hermes-lcm-release-validation-<topic>`
  - [ ] `git diff --check origin/main...HEAD && git diff --check && git diff --cached --check`
- [ ] Workflow validation, if workflows changed: `actionlint`

## Notes
-

Refs #
```

If you skip any validation item, leave it unchecked and explain why in Notes.

Good PRs are:

- accurate about what is actually implemented
- honest about scope
- explicit about tradeoffs
- backed by tests

Do **not** claim behavior that is only partially implemented. If a filter, feature, or fix only applies to one path, say that clearly.

## Validation

Default validation for code changes:

```bash
scripts/validate_release.sh --full --keep-going --output /tmp/hermes-lcm-release-validation-<topic>
pytest tests/test_lcm_core.py tests/test_lcm_engine.py tests/test_packaging_install.py -q
pytest -q
git diff --check origin/main...HEAD
git diff --check
git diff --cached --check
```

Workflow changes should also run:

```bash
actionlint
```

If your PR only touches a narrow surface area, include the focused command too. Example:

```bash
pytest tests/test_lcm_command.py -q
```

Packaging or install-flow changes should also verify the standalone user-plugin path:

```bash
export HERMES_HOME=/tmp/hermes-lcm-smoke
mkdir -p "$HERMES_HOME/plugins"
git clone --branch main https://github.com/the3asic/hermes-lcm "$HERMES_HOME/plugins/hermes-lcm"
# then enable `hermes-lcm` in plugins.enabled and set context.engine: lcm
hermes plugins
```

If you skip part of the default validation, explain why in the PR body.

## Testing expectations

- behavior changes should come with tests
- bug fixes should include a regression test when practical
- command/output changes should verify the rendered text, not just internal helpers
- keep tests readable; avoid clever fixtures when simple setup is enough

## Review expectations

Before requesting review:

- rebase or merge `main` so the branch is current
- resolve conflicts locally
- make sure the PR description matches the branch exactly
- ensure CI is expected to pass from the current head

Reviewers will check:

- correctness
- edge cases
- test coverage
- whether the implementation matches the claimed behavior
- whether the change is appropriately scoped

## Scope guidelines

Priority order:

1. correctness
2. regressions
3. operator safety
4. maintainability
5. new features

Backwards-compatible, well-tested changes are preferred. Destructive or risky workflows should be backup-first and clearly labeled.

## Documentation

Update docs when you change:

- user-facing commands
- tool schemas
- configuration flags
- expected operator workflows

If a new feature needs explanation for contributors or operators, document it in the same PR.

## Questions

If you are unsure whether something should be an issue first, open the issue. It is cheaper than reviewing the wrong PR.
