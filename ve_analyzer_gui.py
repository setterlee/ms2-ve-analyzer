#!/usr/bin/env python3
"""
ve_analyzer_gui.py
GUI multiplataforma para ve_analyzer.py — Mac / Windows.
Portable: distribuir como ejecutable standalone con PyInstaller.
"""

import io
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime

# ── Ruta base: directorio del ejecutable (frozen) o del script ──────────────
if getattr(sys, 'frozen', False):
    _APP_DIR = Path(sys.executable).parent
    sys.path.insert(0, str(Path(sys._MEIPASS)))
else:
    _APP_DIR = Path(__file__).parent
    sys.path.insert(0, str(_APP_DIR))

from ve_analyzer import (
    load_ve_table, load_ae_config,
    load_msl_logs, load_msl_full, load_mlg_full,
    analyze, generate_table, smooth_table,
    load_history_from_tables, check_effectiveness,
    analyze_health, _fmt_health_report,
    detect_ae_events, analyze_ae_calibration, print_ae_calibration,
    print_report, print_effectiveness,
)


# ── Redirección de stdout a widget Text ─────────────────────────────────────

class _Redirect:
    def __init__(self, widget: scrolledtext.ScrolledText):
        self._w = widget

    def write(self, s: str):
        self._w.configure(state='normal')
        self._w.insert(tk.END, s)
        self._w.see(tk.END)
        self._w.configure(state='disabled')

    def flush(self):
        pass


# ── Helper: Treeview de celdas VE ──────────────────────────────────────────

def _cell_tree(parent) -> tuple:
    cols = ('map', 'rpm', 'afr', 'target', 'n', 've_cur', 've_new', 'delta')
    hdrs = ('MAP', 'RPM', 'AFR', 'Target', 'n', 'VE act.', 'VE nuevo', 'Δ')
    wids = (60, 70, 70, 70, 50, 70, 80, 50)

    frm  = ttk.Frame(parent)
    tree = ttk.Treeview(frm, columns=cols, show='headings', height=20)
    for col, hdr, w in zip(cols, hdrs, wids):
        tree.heading(col, text=hdr)
        tree.column(col, width=w, anchor=tk.CENTER, minwidth=w)

    vsb = ttk.Scrollbar(frm, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    return frm, tree


# ── Aplicación principal ─────────────────────────────────────────────────────

class VEAnalyzerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VE Analyzer — MegaSquirt MS2")
        self.root.minsize(1020, 660)

        self._result    = None
        self._ve_data   = None
        self._ae_cfg    = None
        self._log_files: list = []

        self._msq_path     = tk.StringVar()
        self._project_dir  = tk.StringVar()
        self._table_num    = tk.IntVar(value=1)
        self._include_idle = tk.BooleanVar(value=False)
        self._min_samples  = tk.IntVar(value=5)

        # Auto-detectar MSQ en el directorio del ejecutable/script
        for candidate in (_APP_DIR / 'CurrentTune.msq', Path.cwd() / 'CurrentTune.msq'):
            if candidate.exists():
                self._msq_path.set(str(candidate))
                self._project_dir.set(str(candidate.parent))
                break

        self._build_ui()
        self._auto_find_logs()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left = ttk.Frame(paned, width=295)
        left.pack_propagate(False)
        paned.add(left, weight=0)
        self._build_left(left)

        right = ttk.Frame(paned)
        paned.add(right, weight=1)
        self._build_right(right)

        self._status = tk.StringVar(value="Listo.")
        ttk.Label(self.root, textvariable=self._status,
                  relief=tk.SUNKEN, anchor=tk.W,
                  padding=(4, 2)).pack(fill=tk.X, side=tk.BOTTOM, padx=6, pady=(0, 6))

    def _build_left(self, parent):
        # ── Archivos ────────────────────────────────────────────────────────
        f = ttk.LabelFrame(parent, text="Archivos", padding=8)
        f.pack(fill=tk.X, padx=5, pady=5)
        f.columnconfigure(0, weight=1)

        ttk.Label(f, text="CurrentTune.msq").grid(
            row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Entry(f, textvariable=self._msq_path, width=27).grid(
            row=1, column=0, sticky=tk.EW)
        ttk.Button(f, text="…", width=3,
                   command=self._browse_msq).grid(row=1, column=1, padx=(2, 0))

        ttk.Label(f, text="Logs (.msl)").grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0))

        lb_frm = ttk.Frame(f)
        lb_frm.grid(row=3, column=0, columnspan=2, sticky=tk.EW)
        lb_frm.columnconfigure(0, weight=1)
        self._lb = tk.Listbox(lb_frm, height=5, selectmode=tk.EXTENDED)
        self._lb.grid(row=0, column=0, sticky=tk.EW)
        sb = ttk.Scrollbar(lb_frm, orient=tk.VERTICAL, command=self._lb.yview)
        self._lb.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky=tk.NS)

        btn_row = ttk.Frame(f)
        btn_row.grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(3, 0))
        ttk.Button(btn_row, text="Agregar",
                   command=self._browse_logs).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Quitar",
                   command=self._remove_logs).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Auto",
                   command=self._auto_find_logs).pack(side=tk.LEFT)

        ttk.Label(f, text="Directorio proyecto").grid(
            row=5, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(f, textvariable=self._project_dir, width=27).grid(
            row=6, column=0, sticky=tk.EW)
        ttk.Button(f, text="…", width=3,
                   command=self._browse_dir).grid(row=6, column=1, padx=(2, 0))

        # ── Opciones ────────────────────────────────────────────────────────
        o = ttk.LabelFrame(parent, text="Opciones", padding=8)
        o.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(o, text="Tabla VE:").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(o, textvariable=self._table_num,
                     values=[1, 3], width=4,
                     state='readonly').grid(row=0, column=1, sticky=tk.W)

        ttk.Checkbutton(o, text="Incluir ralentí estable",
                        variable=self._include_idle).grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=4)

        ttk.Label(o, text="Mín. muestras/celda:").grid(row=2, column=0, sticky=tk.W)
        ttk.Spinbox(o, textvariable=self._min_samples,
                    from_=1, to=50, width=5).grid(row=2, column=1, sticky=tk.W)

        # ── Acciones ────────────────────────────────────────────────────────
        a = ttk.LabelFrame(parent, text="Acciones", padding=8)
        a.pack(fill=tk.X, padx=5, pady=5)

        self._btn_analyze = ttk.Button(a, text="Analizar VE",
                                       command=self._run_analysis)
        self._btn_analyze.pack(fill=tk.X, pady=2)

        self._btn_gen = ttk.Button(a, text="Generar tabla corregida",
                                   command=self._gen_table, state=tk.DISABLED)
        self._btn_gen.pack(fill=tk.X, pady=2)

        ttk.Separator(a).pack(fill=tk.X, pady=5)

        self._btn_health = ttk.Button(a, text="Diagnóstico de salud",
                                      command=self._run_health)
        self._btn_health.pack(fill=tk.X, pady=2)

        self._btn_ae = ttk.Button(a, text="Calibrar AE",
                                   command=self._run_ae_cal)
        self._btn_ae.pack(fill=tk.X, pady=2)

        ttk.Separator(a).pack(fill=tk.X, pady=5)

        self._btn_smooth = ttk.Button(a, text="Suavizar tabla VE",
                                      command=self._smooth)
        self._btn_smooth.pack(fill=tk.X, pady=2)

    def _build_right(self, parent):
        self._nb = ttk.Notebook(parent)
        self._nb.pack(fill=tk.BOTH, expand=True)

        self._txt_summary = scrolledtext.ScrolledText(
            self._nb, state='disabled', wrap=tk.WORD,
            font=('Courier', 10))
        self._nb.add(self._txt_summary, text="Resumen")

        self._lean_frm, self._lean_tree = _cell_tree(self._nb)
        self._nb.add(self._lean_frm, text="Pobres")

        self._rich_frm, self._rich_tree = _cell_tree(self._nb)
        self._nb.add(self._rich_frm, text="Ricas")

        self._txt_health = scrolledtext.ScrolledText(
            self._nb, state='disabled', wrap=tk.WORD,
            font=('Courier', 10))
        self._nb.add(self._txt_health, text="Salud")

        self._txt_ae = scrolledtext.ScrolledText(
            self._nb, state='disabled', wrap=tk.WORD,
            font=('Courier', 10))
        self._nb.add(self._txt_ae, text="AE")

        self._txt_log = scrolledtext.ScrolledText(
            self._nb, state='disabled', wrap=tk.NONE,
            font=('Courier', 10))
        self._nb.add(self._txt_log, text="Log")

    # ── Navegación de archivos ───────────────────────────────────────────────

    def _browse_msq(self):
        p = filedialog.askopenfilename(
            title="Seleccionar CurrentTune.msq",
            filetypes=[("MSQ", "*.msq"), ("Todos", "*.*")])
        if p:
            self._msq_path.set(p)
            self._project_dir.set(str(Path(p).parent))

    def _browse_logs(self):
        ps = filedialog.askopenfilenames(
            title="Agregar logs (.msl / .mlg)",
            filetypes=[("Logs TunerStudio", "*.msl *.mlg"),
                       ("MSL texto", "*.msl"),
                       ("MLG binario", "*.mlg"),
                       ("Todos", "*.*")])
        for p in ps:
            if p not in self._log_files:
                self._log_files.append(p)
                self._lb.insert(tk.END, Path(p).name)

    def _remove_logs(self):
        for idx in sorted(self._lb.curselection(), reverse=True):
            self._log_files.pop(idx)
            self._lb.delete(idx)

    def _browse_dir(self):
        p = filedialog.askdirectory(title="Seleccionar directorio del proyecto")
        if p:
            self._project_dir.set(p)

    def _auto_find_logs(self):
        msq = self._msq_path.get()
        if not msq:
            return
        logs_dir = Path(msq).parent / 'DataLogs'
        if not logs_dir.is_dir():
            return
        # Preferir .mlg (más compactos y comunes en versiones recientes)
        found = sorted(
            list(logs_dir.glob('*.msl')) + list(logs_dir.glob('*.mlg')),
            key=os.path.getmtime, reverse=True
        )
        # Si hay .mlg y .msl con el mismo nombre base, conservar solo el .mlg
        seen_stems = set()
        deduped = []
        for f in found:
            stem = f.stem
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            deduped.append(f)

        added = 0
        for f in deduped[:15]:
            s = str(f)
            if s not in self._log_files:
                self._log_files.append(s)
                self._lb.insert(tk.END, f.name)
                added += 1
        if added:
            self._status.set(f"Auto: {added} log(s) encontrado(s) en DataLogs/")

    # ── Helpers internos ────────────────────────────────────────────────────

    def _set_txt(self, w: scrolledtext.ScrolledText, text: str):
        w.configure(state='normal')
        w.delete('1.0', tk.END)
        w.insert(tk.END, text)
        w.configure(state='disabled')

    def _append_log(self, text: str):
        self._txt_log.configure(state='normal')
        self._txt_log.insert(tk.END, text)
        self._txt_log.see(tk.END)
        self._txt_log.configure(state='disabled')

    def _lock(self, locked: bool):
        state = tk.DISABLED if locked else tk.NORMAL
        for b in (self._btn_analyze, self._btn_health,
                  self._btn_ae, self._btn_smooth):
            b.configure(state=state)
        if locked:
            self._btn_gen.configure(state=tk.DISABLED)

    def _validate(self, need_logs=True) -> bool:
        msq = self._msq_path.get()
        if not msq or not Path(msq).exists():
            messagebox.showerror("Error", "Selecciona un archivo CurrentTune.msq válido.")
            return False
        if need_logs and not self._log_files:
            messagebox.showerror("Error", "Agrega al menos un log (.msl).")
            return False
        return True

    def _run_in_thread(self, fn, on_done=None):
        """Ejecuta fn() en hilo secundario con stdout redirigido al tab Log."""
        def _worker():
            old = sys.stdout
            sys.stdout = _Redirect(self._txt_log)
            try:
                fn()
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                self.root.after(0, lambda t=tb, err=str(e): (
                    self._append_log(f"\nERROR:\n{t}"),
                    messagebox.showerror("Error", err)
                ))
            finally:
                sys.stdout = old
                if on_done:
                    self.root.after(0, on_done)
        threading.Thread(target=_worker, daemon=True).start()

    # ── Analizar VE ─────────────────────────────────────────────────────────

    def _run_analysis(self):
        if not self._validate():
            return
        self._lock(True)
        self._status.set("Analizando VE…")
        self._set_txt(self._txt_log, "")

        def _do():
            msq       = self._msq_path.get()
            tnum      = self._table_num.get()
            idle      = self._include_idle.get()
            min_s     = self._min_samples.get()
            proj      = self._project_dir.get() or str(Path(msq).parent)

            print(f"Cargando VE tabla {tnum} desde {Path(msq).name}…")
            ve_data = load_ve_table(msq, table_num=tnum, project_dir=proj)

            print("Cargando configuración AE…")
            ae_cfg = load_ae_config(msq)

            print(f"Cargando {len(self._log_files)} log(s)…")
            rows = load_msl_logs(self._log_files, include_idle=idle)
            print(f"  {len(rows):,} muestras válidas.")

            print("Cargando historial…")
            history = load_history_from_tables(proj, tnum)

            print("Analizando…")
            result = analyze(rows, ve_data, ae_cfg,
                             min_samples=min_s, history=history)
            eff = check_effectiveness(result, history)

            # Capturar texto del reporte completo
            buf = io.StringIO()
            old = sys.stdout; sys.stdout = buf
            print_report(result, ae_cfg, self._log_files, ve_data, idle)
            print_effectiveness(eff)
            sys.stdout = old

            self._result  = result
            self._ve_data = ve_data
            self._ae_cfg  = ae_cfg
            rpt = buf.getvalue()
            self.root.after(0, lambda: self._show_analysis(result, rpt))

        def _done():
            self._lock(False)
            if self._result is not None:
                self._btn_gen.configure(state=tk.NORMAL)
            self._status.set("Análisis VE completado.")

        self._run_in_thread(_do, _done)

    def _show_analysis(self, result: dict, report_text: str):
        lean = result['lean']
        rich = result['rich']
        ae   = result['ae']

        lines = [
            "═" * 52,
            "  ANÁLISIS VE — MegaSquirt MS2",
            "═" * 52,
            f"  Muestras válidas    : {ae['total']:,}",
            f"  Con AE activo       : {ae['ae_on_pct']:.1f}%",
            f"  Sin AE              : {ae['ae_off_pct']:.1f}%",
        ]
        if ae.get('afr_off_avg'):
            lines.append(f"  AFR prom (sin AE)   : {ae['afr_off_avg']:.2f}")
        lines += [
            "",
            f"  Celdas POBRES       : {len(lean)}",
            f"  Celdas RICAS        : {len(rich)}",
            f"  Celdas OK           : {len(result['ok'])}",
            f"  Ignoradas (dead band): {len(result.get('skipped', []))}",
        ]
        if not lean and not rich:
            lines.append("\n  ✓ Mezcla dentro de objetivo en todas las celdas.")

        self._set_txt(self._txt_summary, "\n".join(lines))

        self._lean_tree.delete(*self._lean_tree.get_children())
        for c in sorted(lean, key=lambda x: -x['delta']):
            self._lean_tree.insert('', tk.END, values=(
                f"{c['map']:.0f}", c['rpm'],
                f"{c['afr_avg']:.2f}", f"{c['target']:.1f}",
                c['n'], f"{c['ve_cur']:.0f}", c['ve_new'],
                f"{c['delta']:+d}"))
        self._nb.tab(self._lean_frm, text=f"Pobres ({len(lean)})")

        self._rich_tree.delete(*self._rich_tree.get_children())
        for c in sorted(rich, key=lambda x: x['delta']):
            self._rich_tree.insert('', tk.END, values=(
                f"{c['map']:.0f}", c['rpm'],
                f"{c['afr_avg']:.2f}", f"{c['target']:.1f}",
                c['n'], f"{c['ve_cur']:.0f}", c['ve_new'],
                f"{c['delta']:+d}"))
        self._nb.tab(self._rich_frm, text=f"Ricas ({len(rich)})")

        self._append_log("\n" + report_text)
        self._nb.select(0)

    # ── Generar tabla corregida ──────────────────────────────────────────────

    def _gen_table(self):
        if not self._result:
            return
        ts  = datetime.now().strftime('%Y-%m-%d_%H.%M.%S')
        ini = f"veTable{self._table_num.get()}Tbl_{ts}_corrected.table"
        out = filedialog.asksaveasfilename(
            title="Guardar tabla corregida",
            defaultextension=".table",
            filetypes=[("Table", "*.table")],
            initialfile=ini,
            initialdir=self._project_dir.get() or str(_APP_DIR))
        if not out:
            return
        try:
            generate_table(self._result, self._ve_data, out)
            self._status.set(f"Tabla guardada: {Path(out).name}")
            messagebox.showinfo("Éxito", f"Tabla guardada:\n{Path(out).name}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ── Diagnóstico de salud ─────────────────────────────────────────────────

    def _run_health(self):
        if not self._validate():
            return
        self._lock(True)
        self._status.set("Analizando salud del motor…")

        def _do():
            print(f"Cargando {len(self._log_files)} log(s)…")
            rows = load_msl_full(self._log_files)
            print(f"  {len(rows):,} filas totales.")
            print("Calculando métricas de salud…")
            health = analyze_health(rows)
            ts     = datetime.now().strftime('%Y-%m-%d %H:%M')
            report = _fmt_health_report(health, self._log_files, ts)
            self.root.after(0, lambda r=report: (
                self._set_txt(self._txt_health, r),
                self._nb.select(self._txt_health)
            ))

        self._run_in_thread(_do, lambda: (
            self._lock(False),
            self._status.set("Diagnóstico completado.")
        ))

    # ── Calibración AE ───────────────────────────────────────────────────────

    def _run_ae_cal(self):
        if not self._validate():
            return
        self._lock(True)
        self._status.set("Analizando eventos AE…")

        def _do():
            msq = self._msq_path.get()
            print(f"Cargando AE config desde {Path(msq).name}…")
            ae_cfg = load_ae_config(msq)

            print(f"Cargando {len(self._log_files)} log(s)…")
            rows = load_msl_full(self._log_files)
            print(f"  {len(rows):,} filas totales.")

            print("Detectando eventos de aceleración…")
            events = detect_ae_events(rows, ae_cfg)
            print(f"  {len(events)} eventos válidos.")

            result = analyze_ae_calibration(events, ae_cfg)

            buf = io.StringIO()
            old = sys.stdout; sys.stdout = buf
            print_ae_calibration(result, ae_cfg)
            sys.stdout = old

            rpt = buf.getvalue()
            self.root.after(0, lambda r=rpt: (
                self._set_txt(self._txt_ae, r),
                self._nb.select(self._txt_ae)
            ))

        self._run_in_thread(_do, lambda: (
            self._lock(False),
            self._status.set("Calibración AE completada.")
        ))

    # ── Suavizar tabla ───────────────────────────────────────────────────────

    def _smooth(self):
        proj = self._project_dir.get()
        if not proj:
            messagebox.showerror("Error", "Selecciona el directorio del proyecto.")
            return
        self._lock(True)
        self._status.set("Suavizando tabla VE…")

        def _do():
            smooth_table(proj, self._table_num.get())

        self._run_in_thread(_do, lambda: (
            self._lock(False),
            self._status.set("Tabla suavizada. Archivo _smoothed.table guardado."),
            self._nb.select(self._txt_log)
        ))


# ── Entrada principal ────────────────────────────────────────────────────────

def main():
    # DPI awareness en Windows (evita UI borrosa en pantallas HiDPI)
    if sys.platform == 'win32':
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    VEAnalyzerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
