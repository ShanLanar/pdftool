"""
Unit-Tests für die reinen Kernfunktionen von pdf_optimizer.

Bewusst ohne Ghostscript/Tesseract/ocrmypdf: getestet wird nur Logik, die
ohne externe Tools und ohne GUI auskommt (tkinter wird in conftest.py
gestubbt). Ausführen:  python -m pytest -q
"""
from pathlib import Path

import engine as P


# ── CPU-Budget ────────────────────────────────────────────────────────────────

def test_ocr_jobs_budget_normal(monkeypatch):
    monkeypatch.setattr(P, "CPU_COUNT", 8)
    assert P._ocr_jobs_budget(1, gentle=False) == 7   # (8-1) // 1
    assert P._ocr_jobs_budget(2, gentle=False) == 3   # 7 // 2
    assert P._ocr_jobs_budget(4, gentle=False) == 1   # 7 // 4


def test_ocr_jobs_budget_gentle(monkeypatch):
    monkeypatch.setattr(P, "CPU_COUNT", 8)
    assert P._ocr_jobs_budget(1, gentle=True) == 4    # 8 // 2
    assert P._ocr_jobs_budget(2, gentle=True) == 2    # 4 // 2
    assert P._ocr_jobs_budget(8, gentle=True) == 1    # max(1, 4 // 8)


def test_ocr_jobs_budget_never_zero(monkeypatch):
    monkeypatch.setattr(P, "CPU_COUNT", 2)
    assert P._ocr_jobs_budget(4, gentle=False) == 1   # nie 0
    assert P._ocr_jobs_budget(1, gentle=True) == 1


# ── Kollisionsfreie Ausgabepfade ──────────────────────────────────────────────

def test_resolve_output_paths_collision_in_outdir():
    out = Path("/out")
    s = P.OptimizeSettings(output_suffix="_opt", output_dir=out)
    m = P.resolve_output_paths(
        [Path("/a/scan.pdf"), Path("/b/scan.pdf"), Path("/c/other.pdf")], s)
    assert m[Path("/a/scan.pdf")] == out / "scan_opt.pdf"
    assert m[Path("/b/scan.pdf")] == out / "scan_opt_1.pdf"
    assert m[Path("/c/other.pdf")] == out / "other_opt.pdf"
    assert len(set(m.values())) == 3                  # alle eindeutig


def test_resolve_output_paths_per_source_dir():
    s = P.OptimizeSettings(output_suffix="_opt", output_dir=None)
    m = P.resolve_output_paths([Path("/a/scan.pdf"), Path("/b/scan.pdf")], s)
    assert m[Path("/a/scan.pdf")] == Path("/a/scan_opt.pdf")
    assert m[Path("/b/scan.pdf")] == Path("/b/scan_opt.pdf")


def test_resolve_output_paths_in_place_allowed():
    s = P.OptimizeSettings(output_suffix="", output_dir=None)
    m = P.resolve_output_paths([Path("/a/scan.pdf"), Path("/b/scan.pdf")], s)
    assert m[Path("/a/scan.pdf")] == Path("/a/scan.pdf")
    assert m[Path("/b/scan.pdf")] == Path("/b/scan.pdf")


def test_resolve_output_paths_outdir_no_suffix_collision():
    out = Path("/out")
    s = P.OptimizeSettings(output_suffix="", output_dir=out)
    m = P.resolve_output_paths([Path("/a/scan.pdf"), Path("/b/scan.pdf")], s)
    assert m[Path("/a/scan.pdf")] == out / "scan.pdf"
    assert m[Path("/b/scan.pdf")] == out / "scan_1.pdf"


# ── Voller Datei-Hash (gegen Falsch-Duplikate) ───────────────────────────────

def test_file_hash_uses_full_content(tmp_path):
    # Identischer 2-MB-Anfang, unterschiedliches Ende -> muss verschieden sein.
    head = b"A" * (2 * 1024 * 1024)
    f1 = tmp_path / "f1.bin"; f1.write_bytes(head + b"ONE")
    f2 = tmp_path / "f2.bin"; f2.write_bytes(head + b"TWO")
    assert P.file_hash(f1) != P.file_hash(f2)

    f3 = tmp_path / "f3.bin"; f3.write_bytes(head + b"ONE")
    assert P.file_hash(f1) == P.file_hash(f3)   # gleicher Inhalt -> gleicher Hash


def test_file_hash_missing_file_returns_empty(tmp_path):
    assert P.file_hash(tmp_path / "gibtsnicht.bin") == ""


def test_sniff_pdf(tmp_path):
    def w(name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return p
    assert P.sniff_pdf(w("real.pdf", b"%PDF-1.7\n%\xe2\xe3")) == (True, "PDF")
    ok, kind = P.sniff_pdf(w("a.pdf", b"\xff\xfb\x90l\x00\x00"))   # MP3-Frame
    assert ok is False and "MP3" in kind
    assert P.sniff_pdf(w("b.pdf", b"ID3\x03\x00\x00"))[0] is False  # MP3 mit Tag
    assert P.sniff_pdf(w("c.pdf", b"{\\rtf1\\ansi")) == (False, "RTF-Dokument")
    assert P.sniff_pdf(w("d.pdf", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))[0] is False
    assert P.sniff_pdf(w("e.pdf", b"")) == (False, "leer")
    assert P.sniff_pdf(w("g.pdf", b"hello, kein pdf")) == (False, "unbekanntes Format")
    assert P.sniff_pdf(tmp_path / "weg.pdf") == (False, "nicht lesbar")


def test_size_duplicate_candidates(tmp_path):
    a = tmp_path / "a.bin"; a.write_bytes(b"x" * 100)
    b = tmp_path / "b.bin"; b.write_bytes(b"y" * 100)   # gleiche Größe wie a
    c = tmp_path / "c.bin"; c.write_bytes(b"z" * 50)    # eindeutige Größe
    assert set(P.size_duplicate_candidates([a, b, c])) == {a, b}
    assert P.size_duplicate_candidates([c]) == []        # einzeln -> kein Kandidat
    assert P.size_duplicate_candidates([]) == []


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

def test_kb_per_page(tmp_path):
    f = tmp_path / "x.bin"; f.write_bytes(b"x" * 10240)   # 10 KB
    assert P.kb_per_page(f, 0) == 0.0                     # unbekannte Seitenzahl
    assert P.kb_per_page(f, 10) == 1.0                    # 10 KB / 10 Seiten
    assert P.kb_per_page(tmp_path / "weg.bin", 5) == 0.0  # OSError -> 0.0


def test_fileresult_saved_pct():
    r = P.FileResult(path=Path("x.pdf"), success=True,
                     size_before=1000, size_after=250)
    assert r.saved_bytes == 750
    assert r.saved_pct == 75.0

    r0 = P.FileResult(path=Path("y.pdf"), success=True,
                      size_before=0, size_after=0)
    assert r0.saved_pct == 0.0   # keine Division durch Null


# ── Einstellungen & Berichtsordner ───────────────────────────────────────────

def test_load_settings_fills_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_settings_path", lambda: tmp_path / "settings.json")
    prefs = P.load_settings()                 # Datei existiert nicht -> Defaults
    for key in P.DEFAULT_PREFS:
        assert key in prefs
    assert prefs["image_dpi"] == P.DEFAULT_PREFS["image_dpi"]


def test_reports_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_settings_path", lambda: tmp_path / "cfg" / "settings.json")
    # ohne Ausgabeordner -> reports-Unterordner neben der Konfiguration
    d = P._reports_dir(None)
    assert d == tmp_path / "cfg" / "reports"
    assert d.is_dir()
    # mit Ausgabeordner -> genau dieser (und angelegt)
    out = tmp_path / "out"
    assert P._reports_dir(out) == out
    assert out.is_dir()


def test_profiles_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_settings_path", lambda: tmp_path / "settings.json")
    assert P.load_profiles() == {}
    P.save_profile("Archiv", {"image_dpi": 300, "ocr_enabled": False})
    P.save_profile("E-Mail", {"image_dpi": 72})
    profs = P.load_profiles()
    assert set(profs) == {"Archiv", "E-Mail"}
    assert profs["Archiv"]["image_dpi"] == 300
    P.save_profile("   ", {"x": 1})                 # leerer Name -> ignoriert
    assert set(P.load_profiles()) == {"Archiv", "E-Mail"}
    P.delete_profile("Archiv")
    assert set(P.load_profiles()) == {"E-Mail"}
    P.delete_profile("gibtsnicht")                  # kein Fehler
    assert set(P.load_profiles()) == {"E-Mail"}
