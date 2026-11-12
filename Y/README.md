# Tally

See where your money goes without giving your bank statements to anyone. Tally imports the statement files banks let you download, removes duplicates, sorts transactions into categories by rules you write, and reports spending by month. It runs entirely on your own computer and never makes a network connection.

Python 3.15, standard library only, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv tool install .          # installs the `tally` command
tally --help
```

Development: `scripts/setup-dev.sh`, then `uv run pytest`. The design and acceptance criteria are in `docs/design.md`.
