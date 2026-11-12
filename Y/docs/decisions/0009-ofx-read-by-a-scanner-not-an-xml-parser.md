# 9. OFX is read by Tally's own scanner, never an XML parser

**Status:** accepted · **Date:** 2026-11-11 · **Replaces:** the part of ADR 0001 that reads OFX XML with `xml.etree`

## Context

ADR 0001 planned `xml.etree` for OFX 2.x and a small tag scanner for OFX 1.x SGML, and AC39 planned a DOCTYPE check "on the first bytes before any XML parser runs". Slice 3 found that OFX 2.x is OFX 1.x with closed tags and an XML prolog, so one scanner that knows SGML's rule (a value ends at the next tag) reads both forms.

## Decision

`importers/ofx.py` reads both forms with one streamed scanner. No XML parser runs on a statement file. A DOCTYPE is refused wherever the scanner meets it, at any offset and in any case, and every other `<!` declaration (`<!ENTITY`, `<![CDATA[`) is refused too. Entities are the five XML ones, `&nbsp;` and numeric references, decoded by Tally. The encoding comes from the SGML header or the XML prolog and must be on the allowlist in `importers/__init__.py`.

## Rejected options

- **`xml.etree` for the XML form, as ADR 0001 had it:** a second parser for the same vocabulary, with its own view of entities, encodings and errors, and expat underneath. Its errors carry positions, and one file would mean different things to Tally's two readers.
- **`defusedxml`:** still rejected, as in ADR 0001; with no XML parser there is nothing for it to defend.

## Consequences

The "what does another parser do" question has one answer to check: the scanner against what a real SGML or XML parser would read. Slice 3's review and mutation testing asked it: case-insensitive names, text across comments, processing instructions refused in SGML, tags after `</OFX>` refused. A bank whose OFX needs CDATA or a DTD is refused; revisit then.
