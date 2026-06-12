"""Tests für die CLI-Hilfsfunktionen (ohne echte PDF-Verarbeitung)."""
import cli


def test_gather_pdfs(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "c.txt").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.pdf").write_bytes(b"%PDF")

    # nicht rekursiv: nur die oberste Ebene
    assert {p.name for p in cli.gather_pdfs([str(tmp_path)], recursive=False)} == {"a.pdf"}
    # rekursiv: auch Unterordner
    assert {p.name for p in cli.gather_pdfs([str(tmp_path)], recursive=True)} == {"a.pdf", "d.pdf"}

    # explizit angegebene Datei wird auch mit Groß-Endung übernommen
    big = tmp_path / "e.PDF"
    big.write_bytes(b"%PDF")
    assert cli.gather_pdfs([str(big)], recursive=False) == [big]

    # gleicher Pfad doppelt -> dedupliziert, Reihenfolge bleibt
    a = tmp_path / "a.pdf"
    assert cli.gather_pdfs([str(a), str(a)], recursive=False) == [a]

    # Nicht-PDF und fehlende Datei werden ignoriert
    assert cli.gather_pdfs([str(tmp_path / "c.txt"), str(tmp_path / "weg.pdf")],
                           recursive=False) == []
