#!/usr/bin/env python3
"""
ve_analyzer.py — Analizador de mezcla VE para MegaSquirt MS2 / TunerStudio

Uso:
  python3 ve_analyzer.py                    # modo interactivo
  python3 ve_analyzer.py --latest 3         # últimos N logs
  python3 ve_analyzer.py --logs f1.msl f2.msl
  python3 ve_analyzer.py --table mi_ve.table --latest 2

Requiere en el mismo directorio:
  - CurrentTune.msq    (configuración activa: AE, etc.)
  - Un archivo .table  (tabla VE actual exportada desde TunerStudio)
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

def load_ve_table(msq_path: str, table_num: int = 1) -> dict:
    """
    Carga la tabla VE completa desde CurrentTune.msq:
      - Bins RPM : frpm_table{N}
      - Bins MAP : fmap_table{N}
      - Valores  : veTable{N}
    """
    with open(msq_path, errors='replace') as f:
        msq = f.read()

    def get_constant(name):
        m = re.search(rf'name="{name}"[^>]*>(.*?)</constant>', msq, re.DOTALL)
        if not m:
            raise ValueError(f"No se encontró '{name}' en {msq_path}")
        return [float(x) for x in re.findall(r'[\d.]+', m.group(1))]

    rpm_bins = [int(v) for v in get_constant(f'frpm_table{table_num}')]
    map_bins = get_constant(f'fmap_table{table_num}')
    ve_values = get_constant(f'veTable{table_num}')

    n_rows, n_cols = len(map_bins), len(rpm_bins)
    if len(ve_values) != n_rows * n_cols:
        raise ValueError(f"veTable{table_num} tiene {len(ve_values)} valores, "
                         f"esperaba {n_rows * n_cols}")

    ve = [ve_values[r * n_cols:(r + 1) * n_cols] for r in range(n_rows)]
    print(f"  Fuente: {os.path.basename(msq_path)}  (tabla {table_num})")
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


def load_msl_logs(log_files: list) -> list:
    """Parsea uno o más .msl (texto tab-delimitado con header binario)."""
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
                    'tpsdot':   float(parts[cols['TPSdot']])   if 'TPSdot'    in cols else 0.0,
                    'accel_pw': float(parts[cols['Accel PW']]) if 'Accel PW'  in cols else 0.0,
                    'ego_cor':  float(parts[cols['EGO cor1']]) if 'EGO cor1'  in cols else 100.0,
                }
            except (ValueError, IndexError):
                continue

            # Filtros: motor encendido, AFR válido, motor caliente
            if row['rpm'] < 400 or not (8.0 < row['afr'] < 20.0) or row['clt'] < 70:
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

def print_report(result: dict, ae_cfg: dict, log_files: list, ve_data: dict):
    ae   = result['ae']
    lean = result['lean']
    rich = result['rich']

    print("\n" + "="*60)
    print("  ANÁLISIS VE — MegaSquirt MS2")
    print("="*60)
    print(f"  Fuente VE  : {os.path.basename(ve_data['ve_source'])}")
    print(f"  Logs       : {', '.join(os.path.basename(f) for f in log_files)}")
    print(f"  Muestras   : {ae['total']:,} válidas")
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
    pattern = os.path.join(project_dir, f'veTable{table_num}Tbl_*.table')
    files   = sorted(glob.glob(pattern), key=os.path.getmtime)
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
# 6. SELECCIÓN INTERACTIVA
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
# 7. MAIN
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
    parser.add_argument('--min-samples', type=int, default=5,
                        help='Mínimo de muestras por celda (default: 5)')
    parser.add_argument('--out', metavar='FILE',
                        help='Nombre del .table de salida (default: auto)')
    parser.add_argument('--log-dir', default='DataLogs',
                        help='Directorio de logs (default: DataLogs)')
    args = parser.parse_args()

    project_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir     = os.path.join(project_dir, args.log_dir)
    msq_path    = os.path.join(project_dir, args.msq)
    table_num   = args.table_num

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

    if not os.path.exists(msq_path):
        print(f"Error: no se encontró {args.msq}")
        sys.exit(1)

    # ── Cargar tabla VE y config AE desde MSQ ──
    print(f"\nCargando tabla VE (tabla {table_num}):")
    ve_data = load_ve_table(msq_path, table_num)
    ae_cfg  = load_ae_config(msq_path)

    # ── Cargar historial desde .table files ──
    history = load_history_from_tables(project_dir, table_num)

    # ── Parsear logs ──
    print(f"\nParsando {len(log_files)} log(s)...")
    rows = load_msl_logs(log_files)
    if not rows:
        print("No se encontraron muestras válidas.")
        sys.exit(1)

    # ── Análisis ──
    result = analyze(rows, ve_data, ae_cfg, min_samples=args.min_samples, history=history)

    # ── Reporte ──
    print_report(result, ae_cfg, log_files, ve_data)

    # ── Efectividad de correcciones previas ──
    if history:
        sessions = check_effectiveness(result, history)
        print_effectiveness(sessions)

    # ── Generar .table corregido ──
    if result['lean'] or result['rich']:
        ts  = datetime.now().strftime('%Y-%m-%d_%H.%M')
        out = args.out or os.path.join(project_dir, f'veTable{table_num}Tbl_{ts}_corrected.table')
        print("── TABLA CORREGIDA ───────────────────────────────────")
        generate_table(result, ve_data, out)
    else:
        print("No hay correcciones que aplicar.")


if __name__ == '__main__':
    main()
