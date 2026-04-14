#!/usr/bin/env python3
"""
ve_analyzer.py — Analizador de mezcla VE para MegaSquirt MS2 / TunerStudio

Uso:
  python3 ve_analyzer.py                    # modo interactivo
  python3 ve_analyzer.py --latest 3         # últimos N logs
  python3 ve_analyzer.py --logs f1.msl f2.msl
  python3 ve_analyzer.py --save-report      # guarda reporte de salud como .md

Requiere en el mismo directorio:
  - CurrentTune.msq    (configuración activa: AE, etc.)
  - DataLogs/*.msl     (logs de datos)
"""

import argparse
import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# ─────────────────────────────────────────────
# 1. LECTURA DE ARCHIVOS
# ─────────────────────────────────────────────

def load_ve_table(msq_path: str, table_num: int = 1, project_dir: str = None) -> dict:
    """
    Carga la tabla VE completa desde CurrentTune.msq:
      - Bins RPM : frpm_table{N}
      - Bins MAP : fmap_table{N}
      - Valores  : veTable{N}

    La base VE siempre es el MSQ (lo que el usuario importó manualmente en
    TunerStudio). Esto garantiza idempotencia: correr el script N veces con
    los mismos logs produce siempre el mismo resultado.
    """
    with open(msq_path, errors='replace') as f:
        msq = f.read()

    def get_constant(name):
        m = re.search(rf'name="{name}"[^>]*>(.*?)</constant>', msq, re.DOTALL)
        if not m:
            raise ValueError(f"No se encontró '{name}' en {msq_path}")
        return [float(x) for x in re.findall(r'[\d.]+', m.group(1))]

    rpm_bins  = [int(v) for v in get_constant(f'frpm_table{table_num}')]
    map_bins  = get_constant(f'fmap_table{table_num}')
    ve_values = get_constant(f'veTable{table_num}')

    n_rows, n_cols = len(map_bins), len(rpm_bins)
    if len(ve_values) != n_rows * n_cols:
        raise ValueError(f"veTable{table_num} tiene {len(ve_values)} valores, "
                         f"esperaba {n_rows * n_cols}")

    ve = [ve_values[r * n_cols:(r + 1) * n_cols] for r in range(n_rows)]
    print(f"  Base VE : {os.path.basename(msq_path)}  (tabla {table_num})")
    return {'rpm_bins': rpm_bins, 'map_bins': map_bins, 've': ve,
            'n_rows': n_rows, 'n_cols': n_cols,
            've_source': msq_path, 'bins_source': msq_path}


def load_ae_config(msq_path: str) -> dict:
    """Extrae configuración AE/TAE del CurrentTune.msq."""
    with open(msq_path, errors='replace') as f:
        content = f.read()

    def get_values(name):
        m = re.search(rf'name="{name}"[^>]*>(.*?)</constant>', content, re.DOTALL)
        if m:
            return [float(x) for x in re.findall(r'[\d.]+', m.group(1))]
        return None

    def get_scalar(name):
        m = re.search(rf'name="{name}"[^>]*>([\d.]+)</constant>', content)
        if m:
            return float(m.group(1))
        return None

    return {
        'taeRates':    get_values('taeRates'),
        'taeBins':     get_values('taeBins'),
        'maeRates':    get_values('maeRates'),
        'maeBins':     get_values('maeBins'),
        'taeTime':     get_scalar('taeTime'),
        'tpsThresh':   get_scalar('tpsThresh'),
        'aeTaperTime': get_scalar('aeTaperTime'),
        'aeEndPW':     get_scalar('aeEndPW'),
    }


def load_msl_logs(log_files: list, include_idle: bool = False) -> list:
    """Parsea uno o más .msl (texto tab-delimitado con header binario).

    include_idle: si True, incluye filas de ralentí estable (TPS<3%, CLT>70°C,
    RPM 600-1200, sin AE, MAP estable) además de las de carga normal.
    """
    all_rows = []
    for fname in log_files:
        with open(fname, 'rb') as fh:
            raw = fh.read()
        text_start = raw.find(b'Time')
        if text_start < 0:
            print(f"  [!] No se encontró header en {fname}, omitiendo.")
            continue
        text  = raw[text_start:].decode('latin-1', errors='replace')
        lines = text.split('\n')
        if len(lines) < 3:
            continue
        headers = lines[0].strip().split('\t')
        cols    = {h.strip(): i for i, h in enumerate(headers)}

        required = ['RPM', 'MAP', 'AFR', 'TPS', 'CLT']
        missing  = [c for c in required if c not in cols]
        if missing:
            print(f"  [!] Faltan columnas {missing} en {fname}, omitiendo.")
            continue

        for line in lines[2:]:
            parts = line.strip().split('\t')
            if len(parts) < 10:
                continue
            try:
                row = {
                    'rpm':      float(parts[cols['RPM']]),
                    'map':      float(parts[cols['MAP']]),
                    'afr':      float(parts[cols['AFR']]),
                    'tps':      float(parts[cols['TPS']]),
                    'clt':      float(parts[cols['CLT']]),
                    'mat':      float(parts[cols['MAT']])       if 'MAT'       in cols else None,
                    'tpsdot':   float(parts[cols['TPSdot']])   if 'TPSdot'    in cols else 0.0,
                    'mapdot':   float(parts[cols['MAPdot']])   if 'MAPdot'    in cols else 0.0,
                    'accel_pw': float(parts[cols['Accel PW']]) if 'Accel PW'  in cols else 0.0,
                    'ego_cor':  float(parts[cols['EGO cor1']]) if 'EGO cor1'  in cols else 100.0,
                }
            except (ValueError, IndexError):
                continue

            # Filtros: motor encendido, AFR válido, motor caliente
            if row['rpm'] < 400 or not (8.0 < row['afr'] < 20.0) or row['clt'] < 70:
                continue
            # Excluir TPS cerrado (decel o ralentí)
            # Excepción: ralentí estable permitido cuando include_idle=True
            if row['tps'] < 3.0:
                if include_idle:
                    idle_ok = (
                        row['clt'] > 70
                        and 600 < row['rpm'] < 1200
                        and row.get('accel_pw', 0) <= 0.05
                        and abs(row.get('mapdot', 0)) < 15
                    )
                    if not idle_ok:
                        continue
                else:
                    continue
            # Filtro MAT: solo rango térmico estabilizado (evita oscilación por densidad)
            mat = row.get('mat')
            if mat is not None and not (38.0 <= mat <= 58.0):
                continue
            all_rows.append(row)

    return all_rows


# ─────────────────────────────────────────────
# 2. ANÁLISIS
# ─────────────────────────────────────────────

def find_bin(val: float, bins: list) -> int:
    return min(range(len(bins)), key=lambda i: abs(val - bins[i]))


def target_afr(map_kpa: float) -> float:
    """AFR objetivo según zona de carga."""
    if map_kpa <= 40:  return 14.5   # vacío / crucero ligero
    if map_kpa <= 55:  return 14.2   # crucero medio
    if map_kpa <= 75:  return 13.8   # carga media
    return 13.0                       # carga alta / WOT


def zone_min_samples(map_kpa: float, base: int) -> int:
    """Mínimo de muestras según zona — más exigente en zonas ruidosas."""
    if map_kpa <= 40:  return max(base, 20)   # ralentí/vacío: muy ruidoso
    if map_kpa <= 55:  return max(base, 10)   # crucero ligero: algo ruidoso
    return base                                # carga media/alta: usar base


def cell_damping(mi: int, ri: int, history: list) -> float:
    """
    Factor de amortiguación 0.0–1.0 según historial de la celda.
    Si la celda fue corregida en direcciones opuestas → 0.5 (mitad de corrección).
    Si no hay historial de oscilación → 1.0 (corrección completa).
    """
    signs = []
    for session in history:
        for c in session['corrections']:
            if c['mi'] == mi and c['ri'] == ri and abs(c['delta']) >= 1:
                signs.append(1 if c['delta'] > 0 else -1)
    if len(signs) < 2:
        return 1.0
    # Detectar cambios de signo
    changes = sum(1 for i in range(len(signs) - 1) if signs[i] != signs[i + 1])
    return 0.5 if changes >= 1 else 1.0


def analyze(rows: list, ve_data: dict, ae_cfg: dict,
            min_samples: int = 5, history: list = None) -> dict:
    """Calcula AFR promedio por celda, separando muestras con/sin AE activo."""
    rpm_bins   = ve_data['rpm_bins']
    map_bins   = ve_data['map_bins']
    ve         = ve_data['ve']
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0
    history    = history or []

    # Separar muestras
    ae_on  = [r for r in rows if r['accel_pw'] > 0.05]
    ae_off = [r for r in rows if r['accel_pw'] <= 0.05]
    # Falsos positivos AE: accel_pw > 0 pero tpsdot bajo (período de tapering)
    ae_taper = [r for r in ae_on if abs(r['tpsdot']) < tps_thresh]

    # Acumular AFR por celda (solo sin AE para correcciones limpias)
    cell_afrs = {}
    for row in ae_off:
        mi = find_bin(row['map'], map_bins)
        ri = find_bin(row['rpm'], rpm_bins)
        cell_afrs.setdefault((mi, ri), []).append(row['afr'])

    # Calcular correcciones
    lean_cells = []
    rich_cells = []
    ok_cells   = []
    skipped    = []

    for (mi, ri), afrs in sorted(cell_afrs.items()):
        m   = map_bins[mi]
        r   = rpm_bins[ri]
        req = zone_min_samples(m, min_samples)
        if len(afrs) < req:
            continue

        avg   = sum(afrs) / len(afrs)
        tgt   = target_afr(m)
        vc    = ve[mi][ri]
        raw_delta = vc * avg / tgt - vc
        damp  = cell_damping(mi, ri, history)
        delta = round(raw_delta * damp)
        vn    = int(vc) + delta

        entry = {
            'mi': mi, 'ri': ri, 'map': m, 'rpm': r,
            'afr_avg': avg, 'target': tgt, 'n': len(afrs),
            've_cur': vc, 've_new': vn, 'delta': delta,
            'damped': damp < 1.0,
        }

        # Dead band: ignorar correcciones menores a 2 VE (ruido, no señal)
        if abs(delta) < 2:
            ok_cells.append(entry)
            if abs(delta) >= 1:
                skipped.append(entry)   # delta real pero amortiguado a 0
            continue

        if avg > 14.5:
            lean_cells.append(entry)
        elif avg < 13.0:
            rich_cells.append(entry)
        else:
            ok_cells.append(entry)

    # Estadísticas AE
    afrs_all = [r['afr'] for r in rows]
    afrs_on  = [r['afr'] for r in ae_on]
    afrs_off = [r['afr'] for r in ae_off]

    ae_stats = {
        'total':        len(rows),
        'ae_on_pct':    100 * len(ae_on) / max(len(rows), 1),
        'ae_off_pct':   100 * len(ae_off) / max(len(rows), 1),
        'taper_pct':    100 * len(ae_taper) / max(len(ae_on), 1),
        'afr_all_avg':  sum(afrs_all) / max(len(afrs_all), 1),
        'afr_on_avg':   sum(afrs_on)  / max(len(afrs_on), 1)  if afrs_on  else None,
        'afr_off_avg':  sum(afrs_off) / max(len(afrs_off), 1) if afrs_off else None,
        'lean_on_pct':  100 * sum(1 for a in afrs_on  if a > 14.5) / max(len(afrs_on), 1),
        'lean_off_pct': 100 * sum(1 for a in afrs_off if a > 14.5) / max(len(afrs_off), 1),
        'rich_on_pct':  100 * sum(1 for a in afrs_on  if a < 13.0) / max(len(afrs_on), 1),
    }

    return {
        'lean':    lean_cells,
        'rich':    rich_cells,
        'ok':      ok_cells,
        'skipped': skipped,
        'ae':      ae_stats,
        'rows':    rows,
    }


# ─────────────────────────────────────────────
# 3. REPORTE
# ─────────────────────────────────────────────

def print_report(result: dict, ae_cfg: dict, log_files: list, ve_data: dict,
                 include_idle: bool = False):
    ae   = result['ae']
    lean = result['lean']
    rich = result['rich']

    print("\n" + "="*60)
    print("  ANÁLISIS VE — MegaSquirt MS2")
    print("="*60)
    print(f"  Fuente VE  : {os.path.basename(ve_data['ve_source'])}")
    print(f"  Logs       : {', '.join(os.path.basename(f) for f in log_files)}")
    print(f"  Muestras   : {ae['total']:,} válidas")
    if include_idle:
        print(f"  Modo       : incluye ralentí estable (TPS<3%, CLT>70°C, sin AE)")
    print()

    print("── CONFIGURACIÓN AE ──────────────────────────────────")
    print(f"  tpsThresh   : {ae_cfg.get('tpsThresh')} %/s")
    print(f"  taeRates    : {ae_cfg.get('taeRates')}  (%/s bins)")
    print(f"  taeBins     : {ae_cfg.get('taeBins')}  (ms added)")
    print(f"  taeTime     : {ae_cfg.get('taeTime')} s")
    print(f"  aeTaperTime : {ae_cfg.get('aeTaperTime')} s")
    print()

    print("── RESUMEN AE ────────────────────────────────────────")
    print(f"  Con AE activo   : {ae['ae_on_pct']:.1f}%  "
          f"(AFR avg {ae['afr_on_avg']:.2f}  — "
          f"pobres {ae['lean_on_pct']:.0f}%  ricos {ae['rich_on_pct']:.0f}%)")
    print(f"  Sin AE          : {ae['ae_off_pct']:.1f}%  "
          f"(AFR avg {ae['afr_off_avg']:.2f}  — "
          f"pobres {ae['lean_off_pct']:.0f}%)")
    print(f"  Tapering (tpsdot<thresh con AE>0): {ae['taper_pct']:.0f}% del AE total")
    print()

    def table_header():
        print(f"  {'MAP':>5} {'RPM':>6} {'AFR':>7} {'Target':>7} {'n':>5}  "
              f"{'VE_act':>6} {'VE_nuevo':>8} {'Δ':>4}")
        print("  " + "-"*54)

    if lean:
        print(f"── ZONAS POBRES (AFR>14.5) — {len(lean)} celdas ──────────")
        table_header()
        for c in sorted(lean, key=lambda x: -x['delta']):
            print(f"  {c['map']:>5.0f} {c['rpm']:>6} {c['afr_avg']:>7.2f} "
                  f"{c['target']:>7.1f} {c['n']:>5}  "
                  f"{c['ve_cur']:>6.0f} {c['ve_new']:>8} {c['delta']:>+4}")
        print()

    if rich:
        print(f"── ZONAS RICAS (AFR<13.0) — {len(rich)} celdas ──────────")
        table_header()
        for c in sorted(rich, key=lambda x: x['delta']):
            print(f"  {c['map']:>5.0f} {c['rpm']:>6} {c['afr_avg']:>7.2f} "
                  f"{c['target']:>7.1f} {c['n']:>5}  "
                  f"{c['ve_cur']:>6.0f} {c['ve_new']:>8} {c['delta']:>+4}")
        print()

    if not lean and not rich:
        print("  ✓ Sin zonas fuera de objetivo. Mezcla dentro de rango.\n")

    skipped = result.get('skipped', [])
    if skipped:
        print(f"── IGNORADAS (dead band / amortiguadas) — {len(skipped)} celdas ──")
        for c in sorted(skipped, key=lambda x: (x['map'], x['rpm'])):
            damp_tag = ' [amortiguada]' if c.get('damped') else ''
            print(f"  MAP={c['map']:5.0f} RPM={c['rpm']:5d}  "
                  f"AFR={c['afr_avg']:.2f}  Δ={c['delta']:+d}{damp_tag}")
        print()


# ─────────────────────────────────────────────
# 4. GENERACIÓN DEL .table CORREGIDO
# ─────────────────────────────────────────────

def generate_table(result: dict, ve_data: dict, out_path: str):
    """Aplica correcciones al VE y guarda un nuevo .table."""
    import copy
    ve_new = copy.deepcopy(ve_data['ve'])

    for c in result['lean'] + result['rich']:
        ve_new[c['mi']][c['ri']] = float(c['ve_new'])

    rpm_bins = ve_data['rpm_bins']
    map_bins = ve_data['map_bins']

    z_rows = "\n".join(
        "         " + " ".join(f"{v:.1f}" for v in row) + " "
        for row in ve_new
    )
    now = datetime.now().strftime("%a %b %d %H:%M:%S CLST %Y")

    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<tableData xmlns="http://www.EFIAnalytics.com/:table">
<bibliography author="EFI Analytics - philip.tobin@yahoo.com" company="EFI Analytics, copyright 2010, All Rights Reserved." writeDate="{now}"/>
<versionInfo fileFormat="1.0"/>
<table cols="{ve_data['n_cols']}" rows="{ve_data['n_rows']}">
<xAxis cols="{ve_data['n_cols']}" name="rpm">
{chr(10).join("         " + str(r) + " " for r in rpm_bins)}
      </xAxis>
<yAxis name="fuelload" rows="{ve_data['n_rows']}">
{chr(10).join("         " + str(m) + " " for m in map_bins)}
      </yAxis>
<zValues cols="{ve_data['n_cols']}" rows="{ve_data['n_rows']}">
{z_rows}
      </zValues>
</table>
</tableData>
'''
    with open(out_path, 'w') as f:
        f.write(xml)

    total = len(result['lean']) + len(result['rich'])
    print(f"  Tabla corregida guardada: {out_path}")
    print(f"  Celdas modificadas: {total} "
          f"({len(result['lean'])} pobres, {len(result['rich'])} ricas)")


# ─────────────────────────────────────────────
# 5. HISTORIAL DE CORRECCIONES
# ─────────────────────────────────────────────

def _parse_table_file(path: str) -> dict | None:
    """Lee bins y valores VE de un .table XML."""
    with open(path) as f:
        content = f.read()
    xa = re.search(r'<xAxis[^>]*>(.*?)</xAxis>', content, re.DOTALL)
    ya = re.search(r'<yAxis[^>]*>(.*?)</yAxis>', content, re.DOTALL)
    za = re.search(r'<zValues[^>]*>(.*?)</zValues>', content, re.DOTALL)
    if not (xa and ya and za):
        return None
    rpm_bins = [int(float(x)) for x in re.findall(r'[\d.]+', xa.group(1))]
    map_bins = [float(x)      for x in re.findall(r'[\d.]+', ya.group(1))]
    values   = [float(x)      for x in re.findall(r'[\d.]+', za.group(1))]
    return {'rpm_bins': rpm_bins, 'map_bins': map_bins, 'values': values}


def _ts_from_filename(path: str) -> str:
    """Extrae timestamp legible del nombre del archivo."""
    name = os.path.basename(path)
    m = re.search(r'_(\d{4}-\d{2}-\d{2})_(\d{2}[.:]\d{2})', name)
    if m:
        return f"{m.group(1)} {m.group(2).replace('.', ':')}"
    return name


def load_history_from_tables(project_dir: str, table_num: int) -> list:
    """
    Reconstruye historial comparando archivos veTable{N}Tbl_*.table consecutivos.
    Cada par donde difieren celdas = una sesión de correcciones.
    Retorna lista ordenada de más antiguo a más reciente.
    """
    all_files = sorted(glob.glob(
        os.path.join(project_dir, f'veTable{table_num}Tbl_*.table')
    ), key=os.path.getmtime)
    # Excluir _smoothed.table — son outputs, no sesiones de calibración
    files = [f for f in all_files if '_smoothed' not in os.path.basename(f)]
    if len(files) < 2:
        return []

    sessions = []
    prev = _parse_table_file(files[0])

    for path in files[1:]:
        curr = _parse_table_file(path)
        if not prev or not curr:
            prev = curr
            continue
        if len(prev['values']) != len(curr['values']):
            prev = curr
            continue

        n_cols = len(curr['rpm_bins'])
        changed = []
        for idx, (v_old, v_new) in enumerate(zip(prev['values'], curr['values'])):
            if abs(v_new - v_old) >= 0.5:
                mi = idx // n_cols
                ri = idx % n_cols
                changed.append({
                    'mi':        mi,
                    'ri':        ri,
                    'map':       curr['map_bins'][mi],
                    'rpm':       curr['rpm_bins'][ri],
                    'reason':    'lean' if v_new > v_old else 'rich',
                    've_before': v_old,
                    've_after':  v_new,
                    'delta':     v_new - v_old,
                })

        if changed:
            sessions.append({
                'timestamp':  _ts_from_filename(path),
                'table_file': os.path.basename(path),
                'corrections': changed,
            })
        prev = curr

    return sessions


def smooth_table(project_dir: str, table_num: int) -> None:
    """
    Suaviza la tabla VE usando el historial completo de _corrected.table.

    Estrategia de confianza por frecuencia:
    - Cuenta cuántas sesiones corrigieron cada celda → freq[mi][ri]
    - Celdas con freq ≥ anchor_threshold: ANCLADAS (no cambian)
    - Celdas con freq < anchor_threshold: mezcla ponderada hacia el promedio
      de vecinos 3x3 (más blend cuanto menor la frecuencia)
    - alpha (mezcla hacia vecinos) = 1 − freq / anchor_threshold
      → freq=0: alpha=1.0 (100% vecinos)
      → freq=1: alpha=0.67 (66% vecinos)
      → freq≥threshold: alpha=0.0 (anclada)
    - Se aplican PASSES pasadas para propagar suavizado a zonas adyacentes.
    """
    pattern  = os.path.join(project_dir, f'veTable{table_num}Tbl_*_corrected.table')
    files    = sorted(glob.glob(pattern), key=os.path.getmtime)

    if not files:
        print("No se encontraron archivos _corrected.table en el directorio.")
        return

    latest = _parse_table_file(files[-1])
    if not latest:
        print(f"Error leyendo {files[-1]}")
        return

    n_cols = len(latest['rpm_bins'])
    n_rows = len(latest['map_bins'])

    # ── Mapa de frecuencia de correcciones por celda ──
    history = load_history_from_tables(project_dir, table_num)
    freq = [[0] * n_cols for _ in range(n_rows)]
    for session in history:
        for c in session['corrections']:
            mi, ri = c['mi'], c['ri']
            if 0 <= mi < n_rows and 0 <= ri < n_cols:
                freq[mi][ri] += 1

    n_sessions     = len(history)
    # Una celda se considera "establecida" si fue corregida en al menos 1/3
    # de las sesiones (mínimo 2 para evitar que una sola sesión ancle todo).
    anchor_threshold = max(2, n_sessions // 3)

    n_anchored  = sum(freq[mi][ri] >= anchor_threshold
                      for mi in range(n_rows) for ri in range(n_cols))
    n_blend     = sum(0 < freq[mi][ri] < anchor_threshold
                      for mi in range(n_rows) for ri in range(n_cols))
    n_zero      = sum(freq[mi][ri] == 0
                      for mi in range(n_rows) for ri in range(n_cols))
    n_total     = n_rows * n_cols

    print(f"\n── SUAVIZADO DE TABLA VE ─────────────────────────────")
    print(f"  Base:                       {os.path.basename(files[-1])}")
    print(f"  Sesiones en historial:      {n_sessions}")
    print(f"  Umbral de anclaje:          {anchor_threshold} sesiones")
    print(f"  Celdas ancladas (f≥{anchor_threshold}):    {n_anchored}/{n_total}")
    print(f"  Celdas mezcla parcial:      {n_blend}/{n_total}")
    print(f"  Celdas sin historial (f=0): {n_zero}/{n_total}")

    # ── Reconstruir grid 2D de valores ──
    ve = [[latest['values'][mi * n_cols + ri] for ri in range(n_cols)]
          for mi in range(n_rows)]

    # ── Suavizado con mezcla ponderada por confianza + detección de outliers ──
    # Una celda es outlier si su valor difiere del promedio de vecinos en más de
    # OUTLIER_THRESHOLD puntos. Los outliers se suavizan agresivamente
    # independientemente de su frecuencia de corrección.
    PASSES            = 5
    OUTLIER_THRESHOLD = 8   # puntos VE de diferencia vs vecinos (radio amplio)

    def neighbor_avg_weighted(mi, ri, grid, radius=1):
        """Promedio ponderado por distancia inversa en radio NxN."""
        total_w, total_v = 0.0, 0.0
        for dmi in range(-radius, radius + 1):
            for dri in range(-radius, radius + 1):
                if dmi == 0 and dri == 0:
                    continue
                nmi, nri = mi + dmi, ri + dri
                if 0 <= nmi < n_rows and 0 <= nri < n_cols:
                    dist = (dmi ** 2 + dri ** 2) ** 0.5
                    w = 1.0 / dist
                    total_w += w
                    total_v += grid[nmi][nri] * w
        return (total_v / total_w) if total_w > 0 else None

    # Detectar outliers con radio amplio (5x5) para capturar clusters elevados
    # que se protegerían mutuamente en radio 3x3
    outliers = set()
    for mi in range(n_rows):
        for ri in range(n_cols):
            navg = neighbor_avg_weighted(mi, ri, ve, radius=2)
            if navg is not None and abs(ve[mi][ri] - navg) > OUTLIER_THRESHOLD:
                outliers.add((mi, ri))

    if outliers:
        print(f"\n  Celdas outlier detectadas (>{OUTLIER_THRESHOLD} pts vs vecinos): {len(outliers)}")
        for mi, ri in sorted(outliers):
            navg = neighbor_avg_weighted(mi, ri, ve, radius=2)
            print(f"    MAP={latest['map_bins'][mi]:.0f} kPa  RPM={latest['rpm_bins'][ri]}"
                  f"  VE={ve[mi][ri]:.1f}  vecinos_avg={navg:.1f}")

    for _pass in range(PASSES):
        ve_next = [row[:] for row in ve]

        for mi in range(n_rows):
            for ri in range(n_cols):
                is_outlier = (mi, ri) in outliers
                alpha = max(0.0, 1.0 - freq[mi][ri] / anchor_threshold)

                # Outlier: forzar alpha alto para que converja hacia vecinos
                if is_outlier:
                    alpha = max(alpha, 0.7)

                if alpha == 0.0:
                    continue  # anclada y no es outlier

                navg = neighbor_avg_weighted(mi, ri, ve)
                if navg is not None:
                    blended = ve[mi][ri] * (1.0 - alpha) + navg * alpha
                    ve_next[mi][ri] = round(blended, 1)

        ve = ve_next

    # ── Calcular delta total aplicado ──
    orig = [[latest['values'][mi * n_cols + ri] for ri in range(n_cols)]
            for mi in range(n_rows)]
    changed_cells = [(mi, ri, orig[mi][ri], ve[mi][ri])
                     for mi in range(n_rows) for ri in range(n_cols)
                     if abs(ve[mi][ri] - orig[mi][ri]) >= 0.5]

    print(f"\n  Pasadas de suavizado:       {PASSES}")
    print(f"  Celdas modificadas (≥0.5):  {len(changed_cells)}/{n_total}")
    if changed_cells:
        deltas = [abs(v_new - v_old) for _, _, v_old, v_new in changed_cells]
        print(f"  Delta promedio:             {sum(deltas)/len(deltas):.1f}")
        print(f"  Delta máximo:               {max(deltas):.1f}")

    # ── Guardar _smoothed.table ──
    ts  = datetime.now().strftime('%Y-%m-%d_%H.%M')
    out = os.path.join(project_dir, f'veTable{table_num}Tbl_{ts}_smoothed.table')

    z_rows = "\n".join(
        "         " + " ".join(f"{v:.1f}" for v in row) + " "
        for row in ve
    )
    now_str = datetime.now().strftime("%a %b %d %H:%M:%S CLST %Y")

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<tableData xmlns="http://www.EFIAnalytics.com/:table">\n'
        f'<bibliography author="EFI Analytics - philip.tobin@yahoo.com"'
        f' company="EFI Analytics, copyright 2010, All Rights Reserved."'
        f' writeDate="{now_str}"/>\n'
        '<versionInfo fileFormat="1.0"/>\n'
        f'<table cols="{n_cols}" rows="{n_rows}">\n'
        f'<xAxis cols="{n_cols}" name="rpm">\n'
        + "\n".join(f"         {r} " for r in latest['rpm_bins']) + "\n"
        '      </xAxis>\n'
        f'<yAxis name="fuelload" rows="{n_rows}">\n'
        + "\n".join(f"         {m} " for m in latest['map_bins']) + "\n"
        '      </yAxis>\n'
        f'<zValues cols="{n_cols}" rows="{n_rows}">\n'
        f'{z_rows}\n'
        '      </zValues>\n'
        '</table>\n'
        '</tableData>\n'
    )
    with open(out, 'w') as f:
        f.write(xml)

    print(f"\n  Tabla suavizada guardada: {os.path.basename(out)}")


def check_effectiveness(result: dict, history: list) -> list:
    """
    Para cada sesión del historial, evalúa el estado actual de las celdas
    corregidas usando el análisis actual.
    """
    current = {}
    for c in result['lean'] + result['rich'] + result['ok']:
        current[(c['mi'], c['ri'])] = c

    sessions = []
    for rec in reversed(history):  # más reciente primero
        cells = []
        for c in rec['corrections']:
            now   = current.get((c['mi'], c['ri']))
            entry = {
                'map':       c['map'],
                'rpm':       c['rpm'],
                'reason':    c['reason'],
                've_before': c['ve_before'],
                've_after':  c['ve_after'],
                'target':    target_afr(c['map']),
            }
            if now:
                afr_now  = round(now['afr_avg'], 2)
                tgt      = entry['target']
                in_range = abs(afr_now - tgt) <= 0.5
                # La dirección esperada: lean→AFR bajó, rich→AFR subió
                moved_right = (c['reason'] == 'lean' and afr_now < c['ve_before'] + 99) or True
                if in_range:
                    status = 'OK'
                elif (c['reason'] == 'lean'  and afr_now < 14.5) or \
                     (c['reason'] == 'rich'  and afr_now > 13.0):
                    status = 'mejorando'
                elif (c['reason'] == 'lean'  and afr_now > 14.5) or \
                     (c['reason'] == 'rich'  and afr_now < 13.0):
                    status = 'pendiente'
                else:
                    status = 'sin cambio'
                entry['afr_now'] = afr_now
                entry['n_now']   = now['n']
                entry['status']  = status
            else:
                entry['afr_now'] = None
                entry['n_now']   = 0
                entry['status']  = 'sin datos'
            cells.append(entry)

        sessions.append({
            'timestamp':  rec['timestamp'],
            'table_file': rec['table_file'],
            'cells':      cells,
        })

    return sessions


def print_effectiveness(sessions: list):
    """Imprime sección de efectividad de correcciones anteriores."""
    if not sessions:
        return
    STATUS_ICON = {'OK': '✓', 'mejorando': '↑', 'pendiente': '→',
                   'sin cambio': '~', 'sin datos': '?'}
    print("── EFECTIVIDAD CORRECCIONES PREVIAS ─────────────────")
    for s in sessions:
        print(f"  {s['timestamp']}  →  {s['table_file']}")
        print(f"  {'MAP':>5} {'RPM':>6} {'VE Δ':>6} {'AFR_ahora':>10} "
              f"{'Target':>7}  Estado")
        print("  " + "-"*56)
        for c in s['cells']:
            delta_str  = f"{c['ve_after'] - c['ve_before']:>+.1f}"
            afr_str    = f"{c['afr_now']:>10.2f}" if c['afr_now'] else "  sin datos"
            icon       = STATUS_ICON.get(c['status'], '?')
            print(f"  {c['map']:>5.0f} {c['rpm']:>6} {delta_str:>6} "
                  f"{afr_str} {c['target']:>7.1f}  {icon} {c['status']}")
        print()


# ─────────────────────────────────────────────
# 6. DIAGNÓSTICO DE SALUD
# ─────────────────────────────────────────────

def load_msl_full(log_files: list) -> list:
    """
    Parsea logs .msl capturando todas las columnas relevantes para
    diagnóstico de salud (sin filtrar por CLT ni AFR).
    Retorna lista de dicts con claves normalizadas.
    """
    FLOAT_COLS = {
        'RPM':                        'rpm',
        'MAP':                        'map',
        'AFR':                        'afr',
        'TPS':                        'tps',
        'CLT':                        'clt',
        'MAT':                        'mat',
        'Batt V':                     'batt',
        'SPK: Spark Advance':         'adv',
        'SPK: Knock retard':          'knock_retard',
        'SPK: MAT Retard':            'mat_retard',
        'SPK: Cold advance':          'cold_adv',
        'SPK: Idle Correction Advance': 'idle_corr_adv',
        'Fuel: Accel enrich':         'ae_pct',
        'Fuel: Warmup cor':           'wue',
        'Dwell':                      'dwell',
        'DutyCycle1':                 'duty_cycle',
        'PWM Idle Duty':              'iac_duty',
        'Lost Sync Count':            'lost_sync',
        'Timing Err%':                'timing_err',
        'Barometer':                  'baro',
        'TPSdot':                     'tpsdot',
        'MAPdot':                     'mapdot',
        'Accel PW':                   'accel_pw',
    }
    all_rows = []
    for fname in log_files:
        with open(fname, 'rb') as fh:
            raw = fh.read()
        text_start = raw.find(b'Time')
        if text_start < 0:
            continue
        text  = raw[text_start:].decode('latin-1', errors='replace')
        lines = text.split('\n')
        if len(lines) < 3:
            continue
        headers = lines[0].strip().split('\t')
        cols    = {h.strip(): i for i, h in enumerate(headers)}

        for line in lines[2:]:
            parts = line.strip().split('\t')
            if len(parts) < 5:
                continue
            row = {}
            for col_name, key in FLOAT_COLS.items():
                if col_name in cols:
                    try:
                        row[key] = float(parts[cols[col_name]])
                    except (ValueError, IndexError):
                        row[key] = None
                else:
                    row[key] = None
            all_rows.append(row)

    return all_rows


def _pct(count, total):
    return 100.0 * count / total if total > 0 else 0.0


def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _stdev(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5


def analyze_health(rows: list) -> dict:
    """
    Analiza métricas de salud del motor con filtros contextuales.
    Cada métrica se evalúa solo en las condiciones donde tiene sentido.
    """
    n_total = len(rows)

    def fv(key, subset=None):
        src = subset if subset is not None else rows
        return [r[key] for r in src if r.get(key) is not None]

    # ── Segmentos base reutilizables ──
    # Motor en marcha (no cranking)
    running   = [r for r in rows if (r.get('rpm') or 0) > 600]
    # Cranking: motor intentando arrancar
    cranking  = [r for r in rows if 50 < (r.get('rpm') or 0) <= 600]
    # Motor caliente en marcha
    warm_run  = [r for r in running if (r.get('clt') or 0) > 70]
    # Motor frío en marcha (incluye calentamiento)
    cold_run  = [r for r in rows if 200 < (r.get('rpm') or 0) and (r.get('clt') or 99) < 60]

    health = {}

    # ── VOLTAJE ──
    # Solo evaluado con motor en marcha — cranking siempre baja voltaje (normal)
    batt_run  = fv('batt', running)
    batt_warm = fv('batt', warm_run)
    batt_all  = fv('batt')
    health['voltage'] = {
        'n':              len(batt_all),
        'avg_running':    _mean(batt_run),
        'min_running':    min(batt_run)  if batt_run  else None,
        'max_running':    max(batt_run)  if batt_run  else None,
        'min_all':        min(batt_all)  if batt_all  else None,
        # Bajo voltaje con motor caliente y en marcha = problema real
        'low_warm':       sum(1 for v in batt_warm if v < 13.0),
        'low_warm_pct':   _pct(sum(1 for v in batt_warm if v < 13.0), len(batt_warm)),
        'very_low_warm':  sum(1 for v in batt_warm if v < 12.5),
        # Bajo voltaje en cranking: solo informativo
        'low_cranking':   sum(1 for v in fv('batt', cranking) if v < 11.0),
    }

    # ── CLT ──
    clt = fv('clt')
    health['clt'] = {
        'avg': _mean(clt),
        'min': min(clt) if clt else None,
        'max': max(clt) if clt else None,
        'above_100': sum(1 for v in fv('clt', running) if v > 100),
        'above_95':  sum(1 for v in fv('clt', running) if v > 95),
    }

    # ── MAT ──
    # La MAT alta solo es relevante con motor en marcha sostenida
    mat_run = fv('mat', warm_run)
    mat_all = fv('mat')
    mat_retard = fv('mat_retard', warm_run)
    health['mat'] = {
        'avg_warm':       _mean(mat_run),
        'max_warm':       max(mat_run) if mat_run else None,
        'above_45_pct':   _pct(sum(1 for v in mat_run if v > 45), len(mat_run)),
        'above_55_pct':   _pct(sum(1 for v in mat_run if v > 55), len(mat_run)),
        'retard_active':  sum(1 for v in mat_retard if v > 0.5),
        'retard_max':     max(mat_retard) if mat_retard else 0,
        'threshold_71':   (max(mat_run) if mat_run else 0) < 71,
    }

    # ── IGNICIÓN ──
    # Timing error solo cuenta con motor en marcha normal (no cranking)
    adv_run    = fv('adv', running)
    knock_run  = fv('knock_retard', running)
    te_run     = fv('timing_err', running)   # solo en marcha, no cranking
    high_te    = [v for v in te_run if abs(v) > 5]
    health['ignition'] = {
        'adv_avg':             _mean(adv_run),
        'adv_min':             min(adv_run) if adv_run else None,
        'adv_max':             max(adv_run) if adv_run else None,
        'knock_events':        sum(1 for v in knock_run if v > 0),
        'timing_err_max':      max(abs(v) for v in te_run) if te_run else 0,
        'timing_err_high':     len(high_te),
        'timing_err_high_pct': _pct(len(high_te), len(te_run)),
    }

    # ── SYNC / TRIGGER ──
    # Tres zonas: engine off (RPM=0), cranking (1-600), running (>600)
    sync_off      = 0   # motor apagado — normal
    sync_cranking = 0   # cranking — algo esperado
    sync_running  = 0   # en marcha — problemático
    prev_sc = 0
    for r in rows:
        sc  = r.get('lost_sync')
        rpm = r.get('rpm') or 0
        if sc is not None and sc > prev_sc:
            if   rpm == 0:    sync_off      += 1
            elif rpm <= 600:  sync_cranking  += 1
            else:             sync_running   += 1
            prev_sc = sc
        elif sc is not None:
            prev_sc = sc

    sync_vals = fv('lost_sync')
    health['sync'] = {
        'max_count':    max(sync_vals) if sync_vals else 0,
        'events_off':      sync_off,
        'events_cranking': sync_cranking,
        'events_running':  sync_running,
    }

    # ── RALENTÍ ESTABLE (CLT>70, TPS<3%, RPM 600-1200, AE ~100%, MAP estable) ──
    # Excluimos: AE activo (transitorio), MAPdot alto (carga cambiando),
    # y los primeros instantes tras desaceleración (MAPdot muy negativo)
    idle_rows = [r for r in rows
                 if 600  < (r.get('rpm')    or 0)   < 1200
                 and       (r.get('tps')    or 99)  < 3
                 and       (r.get('clt')    or 0)   > 70
                 and       (r.get('ae_pct') or 100) < 106   # sin AE activo
                 and abs(  (r.get('mapdot') or 0))  < 15]   # MAP estable

    idle_rpms = fv('rpm', idle_rows)
    # AFR solo en condiciones verdaderamente estables (sin AE, MAP plano)
    idle_afrs = [r['afr'] for r in idle_rows
                 if r.get('afr') and 10 < r['afr'] < 20]
    idle_iac  = fv('iac_duty', idle_rows)
    idle_adv  = fv('adv', idle_rows)
    idle_corr = fv('idle_corr_adv', idle_rows)

    # Caídas de RPM en ralentí estable (excluye desaceleraciones)
    # Una caída real es cuando el RPM baja mientras ya estaba en ralentí quieto
    dips_real = sum(1 for v in idle_rpms if v < 750)

    # Swings RPM en ventana deslizante de 10 lecturas
    swing_count = 0
    for i in range(5, len(idle_rpms) - 5):
        window = idle_rpms[i - 5:i + 5]
        if max(window) - min(window) > 150:
            swing_count += 1

    # Idle Correction: ¿cuánto corrige y si el RPM sigue inestable a pesar de eso?
    idle_corr_big = [v for v in idle_corr if abs(v) > 3]

    health['idle'] = {
        'n':                    len(idle_rows),
        'rpm_avg':              _mean(idle_rpms),
        'rpm_std':              _stdev(idle_rpms),
        'rpm_min':              min(idle_rpms) if idle_rpms else None,
        'rpm_max':              max(idle_rpms) if idle_rpms else None,
        'rpm_dips':             dips_real,
        'rpm_dips_pct':         _pct(dips_real, len(idle_rpms)),
        'swings_pct':           _pct(swing_count, len(idle_rpms)),
        'afr_avg':              _mean(idle_afrs),
        'afr_lean_pct':         _pct(sum(1 for v in idle_afrs if v > 15.5), len(idle_afrs)),
        'afr_rich_pct':         _pct(sum(1 for v in idle_afrs if v < 13.0), len(idle_afrs)),
        'iac_avg':              _mean(idle_iac),
        'idle_corr_active_pct': _pct(sum(1 for v in idle_corr if abs(v) > 0.5), len(idle_corr)),
        'idle_corr_big_pct':    _pct(len(idle_corr_big), len(idle_corr)),
        'idle_corr_max':        max((abs(v) for v in idle_corr), default=0),
        'adv_avg':              _mean(idle_adv),
    }

    # ── COLD START (CLT<50°C, motor en marcha, excluye cranking) ──
    # Solo contamos AFR una vez que el motor ya está corriendo (RPM > 600)
    # para excluir el enriquecimiento de cranking que es puramente mecánico
    cold_rows = [r for r in rows
                 if (r.get('rpm') or 0) > 600
                 and (r.get('clt') or 99) < 50]
    cold_afrs = [r['afr'] for r in cold_rows
                 if r.get('afr') and 10 < r['afr'] < 20]
    cold_wue  = fv('wue', cold_rows)
    cold_adv  = fv('cold_adv', cold_rows)
    health['cold_start'] = {
        'n':            len(cold_rows),
        'afr_avg':      _mean(cold_afrs),
        'afr_lean_pct': _pct(sum(1 for v in cold_afrs if v > 14.7), len(cold_afrs)),
        'afr_rich_pct': _pct(sum(1 for v in cold_afrs if v < 13.0), len(cold_afrs)),
        'wue_max':      max(cold_wue) if cold_wue else 0,
        'cold_adv_max': max(cold_adv) if cold_adv else 0,
    }

    # ── AE ──
    # Separamos AE durante aceleración real (MAPdot alto o TPSdot alto)
    # de posibles disparos espurios (AE > 105% pero sin transición real)
    ae_rows_all    = [r for r in running if (r.get('ae_pct') or 100) > 105]
    ae_real        = [r for r in ae_rows_all
                      if abs(r.get('mapdot') or 0) > 15
                      or abs(r.get('tpsdot') or 0) > 20]
    ae_spurious    = [r for r in ae_rows_all if r not in ae_real]
    ae_vals_real   = fv('ae_pct', ae_real)
    ae_vals_spur   = fv('ae_pct', ae_spurious)
    health['ae'] = {
        'total_running':    len(running),
        'active_pct':       _pct(len(ae_rows_all), len(running)),
        'real_events':      len(ae_real),
        'spurious_events':  len(ae_spurious),
        'max_real':         max(ae_vals_real) if ae_vals_real else 0,
        'avg_real':         _mean(ae_vals_real),
        'above_200_real':   sum(1 for v in ae_vals_real if v > 200),
        'above_200_spur':   sum(1 for v in ae_vals_spur if v > 200),
    }

    # ── DWELL ──
    # Dwell bajo solo es problema cuando el voltaje es normal
    # Con voltaje bajo (< 12.5V) el battFac lo reduce intencionalmente
    dwell_normal_v = [r for r in running
                      if r.get('dwell') is not None
                      and (r.get('batt') or 0) > 12.5]
    dwell_vals = fv('dwell', dwell_normal_v)
    health['dwell'] = {
        'avg':          _mean(dwell_vals),
        'min':          min(dwell_vals) if dwell_vals else None,
        'max':          max(dwell_vals) if dwell_vals else None,
        'below_2':      sum(1 for v in dwell_vals if v < 2.0),
        'context':      'con voltaje normal (>12.5V)',
    }

    # ── INYECTORES ──
    # DC alto solo importa a RPM donde el motor trabaja (> 1500 RPM)
    dc_load  = fv('duty_cycle', [r for r in running if (r.get('rpm') or 0) > 1500])
    dc_all   = fv('duty_cycle', running)
    health['injectors'] = {
        'avg':      _mean(dc_all),
        'max_all':  max(dc_all)  if dc_all  else 0,
        'max_load': max(dc_load) if dc_load else 0,
        'above_80': sum(1 for v in dc_load if v > 80),
        'above_90': sum(1 for v in dc_load if v > 90),
    }

    # ── RPM COBERTURA ──
    rpm_run = fv('rpm', running)
    health['rpm'] = {
        'max_observed': max(rpm_run) if rpm_run else 0,
        'above_4500':   sum(1 for v in rpm_run if v > 4500),
        'above_5000':   sum(1 for v in rpm_run if v > 5000),
    }

    # ── BAROMETRO ──
    baro = fv('baro', running)
    health['baro'] = {
        'avg':   _mean(baro),
        'min':   min(baro) if baro else None,
        'max':   max(baro) if baro else None,
        'range': (max(baro) - min(baro)) if len(baro) > 1 else 0,
    }

    health['n_total'] = n_total
    return health


def _flag(condition, label_warn='⚠ REVISAR', label_ok='✓'):
    return label_warn if condition else label_ok


def _fmt_health_report(health: dict, log_files: list, timestamp: str) -> str:
    """
    Genera el texto completo del reporte de salud con contexto por condición.
    """
    lines = []
    W = 60

    def sec(title):
        lines.append(f"\n── {title} {'─' * max(1, W - len(title) - 4)}")

    def row(*parts):
        lines.append('  ' + '  '.join(str(p) for p in parts))

    def note(text):
        lines.append(f"  → {text}")

    lines.append('\n' + '═' * W)
    lines.append('  DIAGNÓSTICO DE SALUD DEL MOTOR')
    lines.append('═' * W)
    lines.append(f"  Fecha    : {timestamp}")
    lines.append(f"  Logs     : {len(log_files)} archivo(s)")
    for f in log_files:
        lines.append(f"             {os.path.basename(f)}")
    lines.append(f"  Muestras : {health['n_total']:,} filas totales")

    # ── VOLTAJE ──
    sec('VOLTAJE DE BATERÍA')
    v = health['voltage']
    if v['avg_running'] is not None:
        row(f"Motor en marcha — Prom: {v['avg_running']:.2f}V    "
            f"Min: {v['min_running']:.2f}V    Max: {v['max_running']:.2f}V")
        warn_low  = v['low_warm'] > 0
        warn_vlow = v['very_low_warm'] > 0
        row(f"< 13.0V con motor caliente (>70°C): {v['low_warm']:4d}   "
            f"{_flag(warn_low)}")
        if warn_vlow:
            row(f"< 12.5V con motor caliente: {v['very_low_warm']:4d}   ⚠ CRÍTICO — "
                f"impacta dwell e inyección")
        if warn_low:
            note("Alternador no está cargando correctamente en caliente")
        if v['low_cranking'] > 0:
            row(f"< 11.0V durante cranking: {v['low_cranking']} lecturas   "
                f"(esperado — no es una falla)")
        if v['min_all'] is not None and v['min_all'] < 10.5:
            row(f"Voltaje mínimo absoluto registrado: {v['min_all']:.2f}V   "
                f"(probablemente con motor apagado)")

    # ── CLT ──
    sec('TEMPERATURA MOTOR (CLT)')
    c = health['clt']
    if c['avg']:
        row(f"Prom: {c['avg']:.1f}°C    Min: {c['min']:.1f}°C    Max: {c['max']:.1f}°C")
        if c['above_100'] > 0:
            row(f"SOBRECALENTAMIENTO > 100°C con motor en marcha: "
                f"{c['above_100']} lecturas   ⚠ CRÍTICO")
        elif c['above_95'] > 0:
            row(f"Alta temperatura > 95°C con motor en marcha: "
                f"{c['above_95']} lecturas   ⚠ REVISAR")
        else:
            row("Sin sobrecalentamiento detectado.   ✓")

    # ── MAT ──
    sec('TEMPERATURA AIRE (MAT)')
    m = health['mat']
    if m['avg_warm'] is not None:
        row(f"Motor caliente — Prom: {m['avg_warm']:.1f}°C    Max: {m['max_warm']:.1f}°C")
        warn_mat = m['above_55_pct'] > 20
        row(f"> 45°C : {m['above_45_pct']:.1f}% del tiempo en caliente   "
            f"> 55°C : {m['above_55_pct']:.1f}%   "
            f"{_flag(warn_mat)}")
        if warn_mat:
            note("MAT alta sostenida — riesgo de detonación sin sensor de knock activo")
        if m['retard_active'] > 0:
            row(f"MAT Retard activo: {m['retard_active']} lecturas  "
                f"(máx {m['retard_max']:.1f}°)   ✓")
        elif m['threshold_71']:
            row(f"MAT Retard: 0 lecturas — MAT máxima ({m['max_warm']:.1f}°C) "
                f"no alcanza umbral de tabla (71°C)   (normal)")
        else:
            row("MAT Retard: 0 lecturas aunque MAT alcanza umbral — verificar configuración")

    # ── IGNICIÓN ──
    sec('IGNICIÓN / AVANCE  (solo motor en marcha, excluye cranking)')
    ig = health['ignition']
    if ig['adv_avg'] is not None:
        row(f"Avance prom: {ig['adv_avg']:.1f}°    "
            f"Min: {ig['adv_min']:.1f}°    Max: {ig['adv_max']:.1f}°")
        warn_te = ig['timing_err_high'] > 30
        row(f"Timing Error > 5% : {ig['timing_err_high']:4d} lecturas "
            f"({ig['timing_err_high_pct']:.1f}%)    "
            f"máx = {ig['timing_err_max']:.1f}%   "
            f"{_flag(warn_te)}")
        if warn_te:
            note("Ruido en señal CAS/trigger — revisar blindaje, masa MS2")
        if ig['knock_events'] > 0:
            row(f"Knock retard activo: {ig['knock_events']} eventos   ⚠ DETONACIÓN DETECTADA")
        else:
            row("Knock retard: ningún evento   "
                "(aviso: knock sensor desactivado en MSQ)")

    # ── SYNC ──
    sec('TRIGGER / SYNC')
    s = health['sync']
    row(f"Lost Sync en marcha (RPM>600)  : {s['events_running']:3d}   "
        f"{_flag(s['events_running'] > 0)}")
    row(f"Lost Sync en cranking (RPM≤600): {s['events_cranking']:3d}   "
        f"{_flag(s['events_cranking'] > 2, '⚠ REVISAR', '(aceptable en arranques)')}")
    row(f"Lost Sync con motor apagado    : {s['events_off']:3d}   (normal)")
    if s['events_running'] > 0:
        note("Sync perdido con motor en marcha — revisar cable CAS, masa, reluctor")

    # ── RALENTÍ ──
    sec('RALENTÍ CALIENTE  (CLT>70°C, TPS<3%, AE inactivo, MAP estable)')
    i = health['idle']
    if i['n'] > 10:
        row(f"Muestras válidas: {i['n']:,}    RPM prom: {i['rpm_avg']:.0f}    "
            f"std: {i['rpm_std']:.0f}    Min: {i['rpm_min']:.0f}    Max: {i['rpm_max']:.0f}")
        warn_dips   = i['rpm_dips_pct'] > 1
        warn_swings = i['swings_pct'] > 10
        row(f"Caídas < 750 RPM (sin desaceleración): {i['rpm_dips']:4d} "
            f"({i['rpm_dips_pct']:.1f}%)   {_flag(warn_dips)}")
        row(f"Oscilaciones > 150 RPM: {i['swings_pct']:.1f}%   "
            f"{_flag(warn_swings, '⚠ INESTABLE', '✓')}")
        if warn_dips or warn_swings:
            note("Ralentí inestable — revisar IAC, bypass mecánico, vacío")
        # AFR: ya filtrado sin AE activo ni MAP cambiante
        warn_afr = i['afr_rich_pct'] > 8 or i['afr_lean_pct'] > 8
        row(f"AFR en ralentí limpio — prom: {i['afr_avg']:.2f}    "
            f"Lean >15.5: {i['afr_lean_pct']:.1f}%    "
            f"Rico <13.0: {i['afr_rich_pct']:.1f}%   "
            f"{_flag(warn_afr)}")
        if warn_afr:
            note("AFR filtrado sin AE ni transitorios — revisar celdas VE de ralentí")
        # IAC
        row(f"IAC Duty prom: {i['iac_avg']:.1f}%   "
            f"(open-loop warmup cierra IAC en caliente — informativo)")
        # Idle Correction Advance
        warn_ic = i['idle_corr_big_pct'] > 20
        row(f"Idle Correction Advance > 3°: {i['idle_corr_big_pct']:.1f}% del tiempo  "
            f"máx: {i['idle_corr_max']:.1f}°   "
            f"{_flag(warn_ic, '⚠ timing compensando base de ralentí', '✓')}")
        row(f"Avance ignición promedio en ralentí: {i['adv_avg']:.1f}°")
    elif i['n'] > 0:
        row(f"Pocas muestras de ralentí estable ({i['n']}) — insuficiente para análisis")
    else:
        row("Sin datos de ralentí caliente en estos logs.")

    # ── COLD START ──
    sec('ARRANQUE EN FRÍO  (CLT<50°C, RPM>600, motor ya corriendo)')
    cs = health['cold_start']
    if cs['n'] > 20:
        row(f"Muestras: {cs['n']:,}")
        warn_cs = cs['afr_rich_pct'] > 25 or cs['afr_lean_pct'] > 10
        row(f"AFR prom: {cs['afr_avg']:.2f}    "
            f"Lean >14.7: {cs['afr_lean_pct']:.1f}%    "
            f"Rico <13.0: {cs['afr_rich_pct']:.1f}%   "
            f"{_flag(warn_cs)}")
        row(f"WUE max: {cs['wue_max']:.0f}%    Cold Advance max: {cs['cold_adv_max']:.1f}°")
        if cs['afr_lean_pct'] > 10:
            note("Mezcla pobre en frío — puede causar fallas de arranque")
        if cs['afr_rich_pct'] > 25:
            note("Mezcla muy rica en frío — WUE o ASE posiblemente alto")
    elif cs['n'] > 0:
        row(f"Pocas muestras en frío ({cs['n']}) — insuficiente para análisis")
    else:
        row("Sin datos de arranque en frío en estos logs.")

    # ── AE ──
    sec('ACELERACIÓN ENRICHMENT (AE)')
    ae = health['ae']
    row(f"AE activo (>105%) : {ae['active_pct']:.1f}% del tiempo en marcha")
    row(f"Aceleraciones reales detectadas (MAPdot>15 o TPSdot>20): {ae['real_events']}")
    if ae['real_events'] > 0:
        row(f"  AE max en aceleración real: {ae['max_real']:.0f}%    "
            f"Prom: {ae['avg_real']:.0f}%    "
            f"Eventos >200%: {ae['above_200_real']}   "
            f"{_flag(ae['above_200_real'] > 10)}")
    if ae['spurious_events'] > 0:
        warn_spur = ae['above_200_spur'] > 0
        row(f"AE sin transición detectada (posible ruido): {ae['spurious_events']} lecturas   "
            f"{_flag(warn_spur)}")
        if warn_spur:
            note("AE > 200% sin aceleración real — revisar señal TPS o MAP")

    # ── DWELL ──
    sec('DWELL BOBINAS  (solo con voltaje >12.5V — battFac excluido)')
    d = health['dwell']
    if d['min'] is not None:
        row(f"Prom: {d['avg']:.2f}ms    Min: {d['min']:.2f}ms    Max: {d['max']:.2f}ms   "
            f"{_flag(d['below_2'] > 0)}")
        if d['below_2'] > 0:
            note(f"{d['below_2']} lecturas < 2.0ms a voltaje normal — revisar battFac")
    else:
        row("Sin datos de dwell con voltaje normal.")

    # ── INYECTORES ──
    sec('INYECTORES (DUTY CYCLE)')
    inj = health['injectors']
    row(f"Prom (en marcha): {inj['avg']:.1f}%    "
        f"Máx (>1500 RPM): {inj['max_load']:.1f}%   "
        f"{_flag(inj['max_load'] > 80, '⚠ REVISAR', '✓ lejos de saturación')}")
    if inj['above_80'] > 0:
        row(f"DC > 80% a carga (>1500 RPM): {inj['above_80']}    "
            f"DC > 90%: {inj['above_90']}")
        note("Inyectores cerca de saturación — considerar inyectores más grandes")

    # ── RPM COBERTURA ──
    sec('COBERTURA RPM')
    r = health['rpm']
    warn_cov = r['max_observed'] < 3500
    row(f"RPM máximo observado: {r['max_observed']:.0f}   "
        f"{_flag(warn_cov, '⚠ Zonas altas sin cubrir', '✓')}")
    if warn_cov:
        note("Correcciones VE solo válidas para rango urbano/ralentí")
        note("Hacer rodada con aceleraciones para cubrir zonas de alta carga")

    # ── BAROMETRO ──
    sec('BARÓMETRO  (motor en marcha)')
    b = health['baro']
    if b['avg']:
        row(f"Prom: {b['avg']:.1f}kPa    Min: {b['min']:.1f}kPa    "
            f"Max: {b['max']:.1f}kPa    Rango sesión: {b['range']:.1f}kPa")

    lines.append('')
    return '\n'.join(lines)


def print_health_report(health: dict, log_files: list):
    """Imprime el reporte de salud en consola."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    print(_fmt_health_report(health, log_files, ts))


def save_health_report(health: dict, log_files: list, out_path: str):
    """Guarda el reporte de salud como archivo .md."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    text = _fmt_health_report(health, log_files, ts)
    # Convertir a Markdown básico: reemplazar líneas de '═' y '─' con headers
    md_lines = []
    raw_lines = text.split('\n')
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith('═'):
            continue  # separador, se omite en MD
        elif stripped.startswith('── '):
            title = stripped.lstrip('─ ').rstrip('─ ')
            md_lines.append(f'\n### {title}')
        elif stripped.startswith('DIAGNÓSTICO'):
            md_lines.append(f'# {stripped}')
        else:
            md_lines.append(line)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))
    print(f"  Reporte guardado: {out_path}")


# ─────────────────────────────────────────────
# 6. SELECCIÓN INTERACTIVA (renumerado)
# ─────────────────────────────────────────────

def select_logs_interactive(log_dir: str) -> list:
    logs = sorted(glob.glob(os.path.join(log_dir, '*.msl')),
                  key=os.path.getmtime, reverse=True)
    if not logs:
        print(f"No se encontraron .msl en {log_dir}")
        sys.exit(1)

    print("\nLogs disponibles (más recientes primero):")
    for i, f in enumerate(logs[:20]):
        size_kb = os.path.getsize(f) / 1024
        mtime   = datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
        print(f"  {i+1:2d}. {os.path.basename(f):40s}  {size_kb:7.0f} KB  {mtime}")
    if len(logs) > 20:
        print(f"  ... y {len(logs)-20} más antiguos")

    print("\nEscribe los números separados por comas (ej: 1,2,3) o 'Enter' para los 2 más recientes:")
    sel = input("  > ").strip()

    if not sel:
        return logs[:2]

    indices = []
    for part in sel.split(','):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(logs):
                indices.append(idx)
            else:
                print(f"  [!] Número fuera de rango: {part}")
    return [logs[i] for i in indices] if indices else logs[:2]



# ─────────────────────────────────────────────
# 8. MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Analiza logs MSL y genera tabla VE corregida para TunerStudio')
    parser.add_argument('--logs', nargs='+', metavar='FILE',
                        help='Archivos .msl a analizar')
    parser.add_argument('--latest', type=int, metavar='N',
                        help='Usar los N logs más recientes')
    parser.add_argument('--msq', default='CurrentTune.msq',
                        help='Archivo MSQ (default: CurrentTune.msq)')
    parser.add_argument('--table-num', type=int, default=1, choices=[1, 2, 3],
                        help='Número de tabla VE a usar del MSQ (default: 1)')
    parser.add_argument('--min-samples', type=int, default=20,
                        help='Mínimo de muestras por celda (default: 20)')
    parser.add_argument('--out', metavar='FILE',
                        help='Nombre del .table de salida (default: auto)')
    parser.add_argument('--log-dir', default='DataLogs',
                        help='Directorio de logs (default: DataLogs)')
    parser.add_argument('--save-report', action='store_true',
                        help='Guardar reporte de salud como archivo .md')
    parser.add_argument('--no-health', action='store_true',
                        help='Omitir el reporte de diagnóstico de salud')
    parser.add_argument('--health-only', action='store_true',
                        help='Solo mostrar diagnóstico de salud, sin análisis VE')
    parser.add_argument('--smooth', action='store_true',
                        help='Suavizar tabla VE usando historial de _corrected.table')
    parser.add_argument('--include-idle', action='store_true',
                        help='Incluir ralentí estable (TPS<3%%, CLT>70°C) en correcciones VE')
    args = parser.parse_args()

    project_dir = os.path.dirname(os.path.abspath(__file__))
    table_dir   = os.path.join(project_dir, 've-calibration-process')
    os.makedirs(table_dir, exist_ok=True)
    log_dir     = os.path.join(project_dir, args.log_dir)
    msq_path    = os.path.join(project_dir, args.msq)
    table_num   = args.table_num

    # ── Suavizado de tabla (no requiere logs) ──
    if args.smooth:
        smooth_table(table_dir, table_num)
        return

    # ── Selección de logs ──
    if args.logs:
        log_files = args.logs
    elif args.latest:
        all_logs  = sorted(glob.glob(os.path.join(log_dir, '*.msl')),
                           key=os.path.getmtime, reverse=True)
        log_files = all_logs[:args.latest]
        print(f"Usando los {args.latest} logs más recientes:")
        for f in log_files:
            print(f"  {os.path.basename(f)}")
    else:
        log_files = select_logs_interactive(log_dir)

    if not log_files:
        print("No se seleccionaron logs.")
        sys.exit(1)

    # ── Parsear logs ──
    print(f"\nParsando {len(log_files)} log(s)...")

    # Cargar versión completa para diagnóstico de salud
    rows_full = load_msl_full(log_files)

    # ── Diagnóstico de salud ──
    if not args.no_health:
        health = analyze_health(rows_full)
        print_health_report(health, log_files)
        if args.save_report:
            ts  = datetime.now().strftime('%Y-%m-%d_%H.%M')
            md_out = os.path.join(project_dir, f'diagnostico_{ts}.md')
            save_health_report(health, log_files, md_out)

    if args.health_only:
        return

    if not os.path.exists(msq_path):
        print(f"Error: no se encontró {args.msq}")
        sys.exit(1)

    # ── Cargar tabla VE y config AE desde MSQ ──
    print(f"\nCargando tabla VE (tabla {table_num}):")
    ve_data = load_ve_table(msq_path, table_num)
    ae_cfg  = load_ae_config(msq_path)

    # ── Cargar historial desde .table files ──
    history = load_history_from_tables(table_dir, table_num)

    # Cargar versión filtrada (motor caliente, AFR válido) para análisis VE
    rows = load_msl_logs(log_files, include_idle=args.include_idle)
    if not rows:
        print("No se encontraron muestras válidas para análisis VE.")
        sys.exit(1)

    # ── Análisis VE ──
    result = analyze(rows, ve_data, ae_cfg, min_samples=args.min_samples, history=history)

    # ── Reporte VE ──
    print_report(result, ae_cfg, log_files, ve_data, include_idle=args.include_idle)

    # ── Generar .table corregido o detectar convergencia ──
    n_sessions = len(history)
    if result['lean'] or result['rich']:
        ts  = datetime.now().strftime('%Y-%m-%d_%H.%M')
        out = args.out or os.path.join(table_dir, f'veTable{table_num}Tbl_{ts}_corrected.table')
        print("── TABLA CORREGIDA ───────────────────────────────────")
        generate_table(result, ve_data, out)
        print(f"\n  Sesión {n_sessions + 1} de calibración completada.")
        print(f"  Importa la tabla corregida, toma nuevos logs y vuelve a correr el análisis.")
    else:
        # ── CONVERGENCIA ──
        skipped = result.get('skipped', [])
        print("\n" + "="*60)
        if n_sessions == 0:
            # Sin historial: base del MSQ ya está bien — caso raro
            print("  Sin correcciones en primera sesión.")
            print("  Considera tomar más logs para mayor cobertura.")
        else:
            print("  ¡CALIBRACIÓN COMPLETA!")
            print("="*60)
            print(f"  Todas las celdas cubiertas están dentro del objetivo")
            print(f"  después de {n_sessions} sesión(es) de corrección.")
            if skipped:
                print(f"  ({len(skipped)} celdas con delta=±1 — ruido dentro de dead band)")
            print()
            print("  Generando tabla suavizada final...")
            smooth_table(table_dir, table_num)
            print()
            print("  De ahora en adelante el flujo es:")
            print("  1. Saca logs en condiciones controladas")
            print("  2. Corre el script  →  si hay desviaciones aplica correcciones")
            print("  3. Corre --smooth   →  carga la _smoothed.table en tabla 3")
        print("="*60)


if __name__ == '__main__':
    main()
