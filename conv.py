#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import sys
import math
import re
import subprocess
import tempfile
import shutil
from pathlib import Path
import glob

# -------------------- Konstanten (unverändert zur Logik) --------------------
PCL_HEADER = b"\x1bE\x1b%1B"   # Reset + Wechsel in HPGL/2
PCL_FOOTER = b"\x1b%0A\x1bE"   # Zurück nach PCL + Reset
# ----------------------------------------------------------------------------


def pts_from_px(px: int, dpi: float) -> float:
    return px / dpi * 72.0


def run_bbox(gpcl_exe: str, plt_path: str, w_px: int, h_px: int, dpi: int):
    """Rendert mit GhostPCL auf bbox-Device und liest BoundingBox aus stdout/stderr."""
    args = [gpcl_exe, "-sDEVICE=bbox", f"-r{dpi}", f"-g{w_px}x{h_px}",
            "-dNOPAUSE", "-dBATCH", plt_path]
    res = subprocess.run(args, capture_output=True, text=True, check=False)
    out = (res.stdout or "") + (res.stderr or "")

    boxes = []
    for m in re.finditer(r"HiResBoundingBox:\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)", out):
        boxes.append(tuple(map(float, m.groups())))
    if not boxes:
        for m in re.finditer(r"BoundingBox:\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)", out):
            boxes.append(tuple(map(float, m.groups())))
    if not boxes:
        raise RuntimeError("Konnte BoundingBox nicht ermitteln.\nOutput:\n" + out)

    return (min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes))


def ensure_exists(path: str, what: str):
    if not path:
        print(f"Fehlt: {what} (kein Pfad angegeben)")
        sys.exit(1)
    if not os.path.isfile(path):
        print(f"Fehlt: {what} -> {path}")
        sys.exit(1)


# -------------------- Ausführungslogik pro Datei (unverändert) --------------------

def convert_single(
    gpcl_path: str,
    gs_path: str,
    raw_plt_path: Path,
    out_pdf_path: Path,
    dpi: int,
    margin_pts: float,
    edge_eps: float,
):
    """Konvertiert genau eine PLT/HPGL-Datei ins PDF (zwei Passes)."""
    ensure_exists(gpcl_path, "GhostPCL")
    ensure_exists(gs_path, "Ghostscript")
    if not raw_plt_path.is_file():
        print(f"Fehlt: HPGL/PLT-Datei -> {raw_plt_path}")
        sys.exit(1)

    # 0) Temporäre Wrapper-PLT (PCL-Header + HPGL + PCL-Footer)
    temp_fd, TEMP_PLT_PATH = tempfile.mkstemp(suffix=".plt")
    os.close(temp_fd)
    print(f"Erstelle temporäre PCL-Wrapper-Datei: {TEMP_PLT_PATH}")

    try:
        with open(TEMP_PLT_PATH, 'wb') as temp_f:
            temp_f.write(PCL_HEADER)
            with open(raw_plt_path, 'rb') as raw_f:
                temp_f.write(raw_f.read())
            temp_f.write(PCL_FOOTER)

        # 1) BBox messen (identische Schleife wie zuvor)
        print("Messe die Größe der Zeichnung (Bounding Box)...")
        width_px, height_px = 50000, 50000
        while True:
            llx, lly, urx, ury = run_bbox(gpcl_path, TEMP_PLT_PATH, width_px, height_px, dpi)
            page_w_pts = pts_from_px(width_px, dpi)
            page_h_pts = pts_from_px(height_px, dpi)
            print(f"  Test mit Arbeitsfläche {width_px}x{height_px}px -> BBox: [{urx:.2f}, {ury:.2f}] pts")

            if (urx >= page_w_pts - edge_eps) or (ury >= page_h_pts - edge_eps):
                width_px = math.ceil(width_px * 1.5)
                height_px = math.ceil(height_px * 1.5)
                print(f"  Zeichnung ist größer als die Arbeitsfläche. Vergrößere auf {width_px}x{height_px}px.")
                continue
            break

        print(f"Finale Bounding Box ermittelt: [{llx:.2f}, {lly:.2f}, {urx:.2f}, {ury:.2f}] pts")

        # ---------- PASS 1: GhostPCL -> "großes" PDF, nichts abschneiden ----------
        pass1_w_pts = math.ceil(urx + margin_pts)
        pass1_h_pts = math.ceil(ury + margin_pts)
        pass1_w_px  = math.ceil(pass1_w_pts / 72.0 * dpi)
        pass1_h_px  = math.ceil(pass1_h_pts / 72.0 * dpi)

        _, TEMP_PASS1_PDF = tempfile.mkstemp(suffix=".pdf")
        print(f"Pass 1 (GhostPCL) -> {TEMP_PASS1_PDF}")
        cmd1 = [
            gpcl_path,
            "-sDEVICE=pdfwrite",
            f"-sOutputFile={TEMP_PASS1_PDF}",
            f"-r{dpi}",
            f"-g{pass1_w_px}x{pass1_h_px}",
            f"-dDEVICEWIDTHPOINTS={pass1_w_pts}",
            f"-dDEVICEHEIGHTPOINTS={pass1_h_pts}",
            "-dFIXEDMEDIA",
            "-dAutoRotatePages=/None",
            "-dNOPAUSE", "-dBATCH",
            TEMP_PLT_PATH
        ]
        subprocess.run(cmd1, check=True)

        # ---------- PASS 2: Ghostscript -> verschieben + beschneiden ----------
        draw_w_pts = urx - llx
        draw_h_pts = ury - lly
        out_w_pts  = math.ceil(draw_w_pts + 2 * margin_pts)
        out_h_pts  = math.ceil(draw_h_pts + 2 * margin_pts)
        x_off = margin_pts - llx
        y_off = margin_pts - lly

        print(f"Pass 2 (Ghostscript): Zielseite {out_w_pts} x {out_h_pts} pt, Offset ({x_off:.3f}, {y_off:.3f}) pt")
        cmd2 = [
            gs_path,
            "-sDEVICE=pdfwrite",
            f"-sOutputFile={str(out_pdf_path)}",
            f"-dDEVICEWIDTHPOINTS={out_w_pts}",
            f"-dDEVICEHEIGHTPOINTS={out_h_pts}",
            "-dFIXEDMEDIA",
            "-dAutoRotatePages=/None",
            "-dCompatibilityLevel=1.6",
            "-dNOPAUSE", "-dBATCH",
            "-c", f"<< /PageOffset [{x_off} {y_off}] >> setpagedevice",
            "-f", TEMP_PASS1_PDF
        ]
        subprocess.run(cmd2, check=True)
        print(f"\nFertig! PDF erstellt: {out_pdf_path}")

    finally:
        # Aufräumen
        try:
            if os.path.exists(TEMP_PLT_PATH):
                os.remove(TEMP_PLT_PATH)
                print(f"Temporäre Datei gelöscht: {TEMP_PLT_PATH}")
        except Exception:
            pass
        try:
            if 'TEMP_PASS1_PDF' in locals() and os.path.exists(TEMP_PASS1_PDF):
                os.remove(TEMP_PASS1_PDF)
                print(f"Temporäre Datei gelöscht: {TEMP_PASS1_PDF}")
        except Exception:
            pass


# -------------------- Helfer: Pfad-Findung für Executables --------------------

def discover_executable(user_arg: str, env_keys: list[str], candidates: list[str]) -> str | None:
    """Findet ein Executable über (1) CLI-Arg, (2) Umgebungsvariablen, (3) PATH-Kandidaten."""
    if user_arg:
        return user_arg

    for key in env_keys:
        p = os.environ.get(key)
        if p and Path(p).exists():
            return p

    for name in candidates:
        p = shutil.which(name)
        if p:
            return p

    return None


def resolve_gpcl(path_arg: str | None) -> str | None:
    # Häufige Binärnamen unter Windows/Linux/macOS
    candidates = [
        "gpcl6win64.exe", "gpcl6win32.exe", "gpcl6.exe",
        "gpcl6", "pcl6",
    ]
    found = discover_executable(path_arg, ["GHOSTPCL", "GPCL"], candidates)
    if found:
        return found

    # Zusätzliche Windows-Autodetektion in Program Files
    if os.name == "nt":
        bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        patterns = [
            "ghostpcl-*\gpcl6win64.exe",
            "ghostpcl-*\gpcl6win32.exe",
            "ghostpcl-*\gpcl6.exe",
            "GhostPCL*\gpcl6win64.exe",
            "GhostPCL*\gpcl6win32.exe",
            "GhostPCL*\gpcl6.exe",
            "ghostpcl-*\bin\gpcl6win64.exe",
            "ghostpcl-*\bin\gpcl6win32.exe",
            "ghostpcl-*\bin\gpcl6.exe",
            "ghostpcl-*\bin\pcl6.exe",
        ]
        for base in filter(None, bases):
            for pat in patterns:
                for p in glob.glob(os.path.join(base, pat)):
                    if os.path.isfile(p):
                        return p
    return None


def resolve_gs(path_arg: str | None) -> str | None:
    candidates = [
        "gswin64c.exe", "gswin32c.exe",
        "gs",
    ]
    found = discover_executable(path_arg, ["GHOSTSCRIPT", "GS"], candidates)
    if found:
        return found

    if os.name == "nt":
        bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        patterns = [
            "gs\gs*\bin\gswin64c.exe",
            "gs\gs*\bin\gswin32c.exe",
        ]
        for base in filter(None, bases):
            for pat in patterns:
                for p in glob.glob(os.path.join(base, pat)):
                    if os.path.isfile(p):
                        return p
    return None


# -------------------- CLI & Batch-Verarbeitung --------------------

def iter_input_files(base: Path, recursive: bool) -> list[Path]:
    if base.is_file():
        return [base]
    if base.is_dir():
        if recursive:
            return [p for p in base.rglob("*.plt") if p.is_file()]
        else:
            return [p for p in base.glob("*.plt") if p.is_file()]
    raise FileNotFoundError(str(base))


def build_out_path(in_path: Path, out_arg: str | None) -> Path:
    if out_arg:
        outp = Path(out_arg)
        if outp.is_dir():
            return outp / (in_path.stem + ".pdf")
        return outp
    return in_path.with_suffix(".pdf")


def parse_args():
    p = argparse.ArgumentParser(
        description="Konvertiert rohe HP-GL/2 (.plt) in sauber zentrierte/beschnittene PDFs (2-Pass-Methode)")
    p.add_argument("input", help="PLT-Datei oder Verzeichnis")
    p.add_argument("--output", "-o", help="Zieldatei (bei Einzeldatei) oder Zielordner (bei Verzeichnis)")

    p.add_argument("--gpcl", help="Pfad zur GhostPCL-Executable. Alternativ Umgebungsvariablen GHOSTPCL/GPCL oder PATH.")
    p.add_argument("--gs", help="Pfad zur Ghostscript-Executable. Alternativ Umgebungsvariablen GHOSTSCRIPT/GS oder PATH.")

    p.add_argument("--dpi", type=int, default=500, help="Renderauflösung (DPI), Standard: 500")
    p.add_argument("--margin-pts", type=float, default=40.0, help="Rand in Punkten, Standard: 40.0")
    p.add_argument("--edge-eps", type=float, default=1.0, help="Toleranz zur Kantenprüfung in Punkten, Standard: 1.0")

    p.add_argument("--recursive", "-r", action="store_true", help="Verzeichnis rekursiv durchsuchen")
    return p.parse_args()


def main():
    args = parse_args()

    gpcl_path = resolve_gpcl(args.gpcl)
    gs_path = resolve_gs(args.gs)

    if not gpcl_path:
        print("GhostPCL nicht gefunden. Übergib --gpcl oder setze GHOSTPCL/GPCL oder installiere es und sorge dafür, dass es im PATH ist.")
        sys.exit(1)
    if not gs_path:
        print("Ghostscript nicht gefunden. Übergib --gs oder setze GHOSTSCRIPT/GS oder installiere es und sorge dafür, dass es im PATH ist.")
        sys.exit(1)

    base = Path(args.input)
    files = iter_input_files(base, args.recursive)
    if not files:
        print("Keine .plt-Dateien gefunden.")
        sys.exit(1)

    for f in files:
        out_path = build_out_path(f, args.output)
        # Stelle sicher, dass Zielordner existiert
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print("-" * 78)
        print(f"Eingabe: {f}")
        print(f"Ausgabe: {out_path}")
        convert_single(
            gpcl_path=gpcl_path,
            gs_path=gs_path,
            raw_plt_path=f,
            out_pdf_path=out_path,
            dpi=args.dpi,
            margin_pts=args.margin_pts,
            edge_eps=args.edge_eps,
        )


if __name__ == "__main__":
    main()
