# 3. Installs refuse source builds, except the mutation testing job

**Status:** accepted · **Date:** 2026-11-10 · **Replaces:** none

## Context

The supply-chain policy turns install scripts off (`ignore-scripts` for npm). Python's equivalent is refusing source distributions: a wheel is unpacked, an sdist runs its `setup.py` or build backend at install time. uv has `--no-build` (and `UV_NO_BUILD`), but no per-package exception: `no-build-package` is a deny list. On Python 3.15, two months after its release, `pyyaml` has no wheel yet, and mutmut needs it through libcst.

## Decision

`UV_NO_BUILD=1` for every install: in CI's workflow `env` and in `scripts/setup-dev.sh`. mutmut lives in its own dependency group, `mutation`, which only the mutation job installs, with `UV_NO_BUILD=0`. The setting is on the install commands rather than in `pyproject.toml` because a `no-build = true` there cannot be overridden for one job.

## Rejected options

- **`no-build = true` in `pyproject.toml`:** cleaner, but then no job can install mutmut at all; uv cannot except one package.
- **No mutation testing until pyyaml ships a 3.15 wheel:** mutation testing on changed files is the check this project most relies on.
- **Building everything from source:** every dependency's build runs arbitrary code at install; the point of the gate.

## Consequences

One job builds one package from source, and that is written down. When `pyyaml` publishes a 3.15 wheel, the group can stay but the `UV_NO_BUILD=0` on the mutation job goes. CI proves the setting is on in the checks job (the "Supply-chain settings take effect" step).
