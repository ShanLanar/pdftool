"""
PDF Optimizer - Kommandozeilen-Version.

Nutzt dieselbe Engine wie die GUI (engine.py), aber ohne tkinter – ideal für
Skripte, Server oder Stapelläufe über Nacht.

Beispiele:
    python cli.py akte.pdf
    python cli.py -o out/ --preset screen --no-ocr ordner/
    python cli.py -r --workers 2 --gentle ~/Scans
"""
import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import engine
from engine import OptimizeSettings, optimize_pdf, resolve_output_paths

log = logging.getLogger("pdfopt.cli")


def gather_pdfs(paths: list[str], recursive: bool) -> list[Path]:
    """Sammelt PDF-Dateien aus den angegebenen Pfaden (Dateien oder Ordner)."""
    collected: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            collected += sorted(p.glob("**/*.pdf" if recursive else "*.pdf"))
        elif p.suffix.lower() == ".pdf" and p.is_file():
            collected.append(p)
        else:
            log.warning("Übersprungen (keine PDF / nicht gefunden): %s", p)
    # Reihenfolge erhalten, Duplikate entfernen
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in collected:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pdfopt",
        description="PDF-Dateien komprimieren und per OCR durchsuchbar machen.",
    )
    ap.add_argument("paths", nargs="+", metavar="PFAD",
                    help="PDF-Dateien oder Ordner")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="Ordner rekursiv nach PDFs durchsuchen")
    ap.add_argument("-o", "--output-dir", metavar="DIR",
                    help="Ausgabeordner (Standard: neben der Quelle)")
    ap.add_argument("-s", "--suffix", default="_opt",
                    help='Dateinamen-Suffix (Standard "_opt"; "" = überschreiben)')
    ap.add_argument("--preset", choices=list(engine.GS_PRESETS.keys()),
                    default="ebook", help="Ghostscript-Preset (Standard: ebook)")
    ap.add_argument("--dpi", type=int, default=150,
                    help="Ziel-DPI für Bilder (Standard: 150)")
    ap.add_argument("--jpeg-quality", type=int, default=75,
                    help="JPEG-Qualität 1–95 (Standard: 75)")
    ap.add_argument("--no-compress", action="store_true",
                    help="Ghostscript-Komprimierung überspringen")
    ap.add_argument("--no-recompress", action="store_true",
                    help="direkte Bildneukomprimierung (Pillow) überspringen")
    ap.add_argument("--no-ocr", action="store_true", help="kein OCR")
    ap.add_argument("--ocr-lang", default="deu+eng",
                    help='OCR-Sprache(n), z. B. "deu+eng" (Standard)')
    ap.add_argument("--force-ocr", action="store_true",
                    help="OCR auch bei bereits vorhandenem Text erzwingen")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallele Worker (Standard: 1)")
    ap.add_argument("--gentle", action="store_true",
                    help="Schonmodus (geringere CPU-Last)")
    ap.add_argument("--skip-kb", type=int, default=0, metavar="N",
                    help="Dateien mit ≤ N KB/Seite überspringen (0 = aus)")
    ap.add_argument("-q", "--quiet", action="store_true", help="nur Warnungen/Fehler")
    ap.add_argument("-v", "--verbose", action="store_true", help="ausführliche Ausgabe")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING if args.quiet else (logging.DEBUG if args.verbose else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    files = gather_pdfs(args.paths, args.recursive)
    if not files:
        log.error("Keine PDF-Dateien gefunden.")
        return 1

    workers = max(1, args.workers)
    settings = OptimizeSettings(
        compress=not args.no_compress,
        gs_preset=args.preset,
        image_dpi=args.dpi,
        recompress_images=not args.no_recompress,
        jpeg_quality=args.jpeg_quality,
        ocr_enabled=not args.no_ocr,
        ocr_lang=args.ocr_lang,
        ocr_force=args.force_ocr,
        gentle_mode=args.gentle,
        skip_kb_per_page=args.skip_kb,
        output_suffix=args.suffix,
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
    )
    # CPU-Budget wie in der GUI an Kerne/Worker koppeln (verhindert Überlastung).
    settings.ocr_jobs = engine._ocr_jobs_budget(workers, settings.gentle_mode)
    out_map = resolve_output_paths(files, settings)

    log.info("%d Datei(en) · %d Worker · %d OCR-Job(s)/Worker",
             len(files), workers, settings.ocr_jobs)

    results = []
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(optimize_pdf, p, settings,
                                out_path=out_map.get(p)): p for p in files}
            for fut in as_completed(futs):
                results.append(fut.result())
    else:
        for p in files:
            results.append(optimize_pdf(p, settings, out_path=out_map.get(p)))

    # ── Zusammenfassung ──────────────────────────────────────────────────────
    ok     = [r for r in results if r.success and not r.skipped]
    failed = [r for r in results if not r.success and not r.skipped]
    before = sum(r.size_before for r in ok)
    after  = sum(r.size_after for r in ok)
    print("\n" + "=" * 60)
    for r in ok:
        tag = " [Original behalten]" if r.kept_original else (" [+OCR]" if r.ocr_applied else "")
        print(f"  OK   {r.path.name[:48]:48}  "
              f"{r.size_before/1024:7.0f} KB -> {r.size_after/1024:7.0f} KB"
              f"  (-{r.saved_pct:4.1f}%){tag}")
    for r in failed:
        print(f"  FEHL {r.path.name[:48]:48}  {r.error}")
    if before:
        print(f"\nGesamt: {before/1048576:.1f} MB -> {after/1048576:.1f} MB "
              f"(-{(before-after)/before*100:.1f}%)")
    print(f"{len(ok)} optimiert, {len(failed)} Fehler.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
