import hashlib

import pytest

from tally.encoding import detect_encoding, scan_file


def test_utf8_bom(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"\xef\xbb\xbfDate,Desc\n")
    assert detect_encoding(p) == "utf-8-sig"


def test_utf16_bom(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes("Date,Desc\n".encode("utf-16"))
    assert detect_encoding(p) == "utf-16"


def test_plain_utf8(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes("CAFÉ NERO\n".encode())
    assert detect_encoding(p) == "utf-8"


def test_latin1_falls_to_cp1252(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes("CAFÉ NERO\n".encode("latin-1"))
    assert detect_encoding(p) == "cp1252"
    assert p.read_text(encoding="cp1252") == "CAFÉ NERO\n"


def test_bytes_undefined_in_cp1252_fall_to_latin1(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"A\x81B\n")
    assert detect_encoding(p) == "latin-1"


def test_declared_encoding_wins_without_bom(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"plain ascii\n")
    assert detect_encoding(p, "latin-1") == "latin-1"
    assert detect_encoding(p, "auto") == "utf-8"


def test_bom_beats_declared(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"\xef\xbb\xbfx\n")
    assert detect_encoding(p, "latin-1") == "utf-8-sig"


def test_unknown_declared_encoding(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"x\n")
    with pytest.raises(ValueError, match="unknown encoding"):
        detect_encoding(p, "klingon")


def test_large_file_is_scanned_in_chunks(tmp_path):
    p = tmp_path / "big.csv"
    with open(p, "wb") as fh:
        for _ in range(300):
            fh.write(b"x" * 10000 + "é\n".encode())
        fh.write("é".encode("latin-1"))  # invalid UTF-8 right at the end
    assert detect_encoding(p) == "cp1252"


def test_scan_file_returns_encoding_and_digest_in_one_pass(tmp_path):
    p = tmp_path / "f.csv"
    data = "CAFÉ NERO\n".encode("latin-1") * 1000
    p.write_bytes(data)
    assert scan_file(p) == ("cp1252", hashlib.sha256(data).hexdigest())
    p.write_bytes(b"\xef\xbb\xbfplain\n")
    assert scan_file(p)[0] == "utf-8-sig"
    assert scan_file(p, "latin-1")[0] == "utf-8-sig"
    p.write_bytes(b"plain\n")
    assert scan_file(p, "latin-1") == ("latin-1", hashlib.sha256(b"plain\n").hexdigest())
