# 1. Python 3.15 with uv, standard library only, SQLite for storage

**Status:** accepted; replaced in part by [ADR 0009](0009-ofx-read-by-a-scanner-not-an-xml-parser.md), which reads OFX XML with Tally's own scanner instead of `xml.etree` · **Date:** 2026-11-10 · **Replaces:** none

## Context

The brief fixes the stack: Python 3.15 managed with uv, standard library first, SQLite through the standard library, installed as one command. What was open was whether any runtime dependency earns its place: a CSV dialect sniffer, an OFX library, a TOML writer, a terminal table library, a rich-text or colour library.

## Decision

No runtime dependencies. `csv`, `decimal`, `sqlite3`, `tomllib`, `xml.etree`, `argparse`, `datetime` and `re` cover the brief; OFX's SGML form gets a small purpose-built reader (a tag scanner, not a general SGML parser). Colour is a handful of ANSI sequences behind one function that checks the terminal and `NO_COLOR`.

## Rejected options

- **An OFX library (`ofxparse`, `ofxtools`):** each pulls in further packages and parses more of OFX than Tally needs (a statement's transaction list), and the supply-chain gates then have to cover them. The SGML form is unclosed tags with a known small vocabulary; a 100-line reader with tests on the sample and on hostile files is less risk than a dependency nobody here reviews.
- **A rich terminal library (`rich`):** colour and tables are a few lines; the brief's accessibility requirements (meaning never by colour alone, colour off when piped or under `NO_COLOR`) are easier to prove on code this small than to configure in a library.
- **`defusedxml` for the OFX XML form:** `xml.etree` on Python 3.15 does not resolve external entities and expat limits entity expansion; the design still refuses any document with a DOCTYPE (AC39), which removes the remaining surface without a dependency.

## Consequences

The dependency audit, lockfile check and release-age gate guard only development tools. Every parser is Tally's own, so the "what does another parser do with this input" question is answered by Tally's tests rather than a library's. Worth revisiting if OFX files from real banks turn out to need more of the standard than the samples show.
