"""Work out how a statement file is encoded without guessing wildly.

Banks send UTF-8 (with or without a byte order mark), UTF-16 (always with
one), Windows-1252 and Latin-1. The rules, in order:

1. A byte order mark decides.
2. An encoding named in the layout decides.
3. If the whole file is valid UTF-8, it is UTF-8.
4. If it is valid Windows-1252, it is Windows-1252 (which covers Latin-1
   text and the smart quotes and euro sign Windows exports add).
5. Otherwise Latin-1, which can decode any bytes.

Steps 3 and 4 read the whole file once, in chunks, so very large files
are not held in memory.
"""

from __future__ import annotations

import codecs
import hashlib
import os

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_CHUNK = 1 << 20


def detect_encoding(path: str | os.PathLike[str], declared: str | None = None) -> str:
    """Return the codec name to open ``path`` with."""
    return scan_file(path, declared)[0]


def scan_file(path: str | os.PathLike[str], declared: str | None = None) -> tuple[str, str]:
    """Return the codec name to open ``path`` with, and the file's SHA-256 hex digest.

    One pass over the file serves both: the import needs the digest to
    recognise a file it has seen before, and the encoding scan has to read
    every byte anyway. The UTF-8 and Windows-1252 decoders run side by side
    so a file that is not UTF-8 does not need a second pass.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        head = fh.read(4)
        fh.seek(0)
        bom_encoding = next((name for bom, name in _BOMS if head.startswith(bom)), None)
        if declared and declared.lower() != "auto":
            try:
                codecs.lookup(declared)
            except LookupError:
                raise ValueError(f"unknown encoding {declared!r} in layout") from None
        decoders: dict[str, codecs.IncrementalDecoder] = {}
        if bom_encoding is None and not (declared and declared.lower() != "auto"):
            decoders = {
                "utf-8": codecs.getincrementaldecoder("utf-8")(errors="strict"),
                "cp1252": codecs.getincrementaldecoder("cp1252")(errors="strict"),
            }
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            for name, decoder in list(decoders.items()):
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    del decoders[name]
        for name, decoder in list(decoders.items()):
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                del decoders[name]
    if bom_encoding is not None:
        return bom_encoding, digest.hexdigest()
    if declared and declared.lower() != "auto":
        return declared, digest.hexdigest()
    if "utf-8" in decoders:
        return "utf-8", digest.hexdigest()
    if "cp1252" in decoders:
        return "cp1252", digest.hexdigest()
    return "latin-1", digest.hexdigest()
