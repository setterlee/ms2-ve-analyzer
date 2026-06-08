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
import struct
import sys
from collections import deque
from datetime import datetime
from pathlib import Path


# ─────────────────────────────────────────────
# 0. SOPORTE MLG (binario TunerStudio/MegaLogViewer)
# ─────────────────────────────────────────────
#
# Formato MLVLG verificado empíricamente con archivos reales:
#   Header  24 bytes: magic(5) + flags(3) + timestamp(4) + misc(8) + n_ch(2) + ?(2)
#   Canales N × 89 bytes: type(1) + name(34) + units(34) + scale(4) + offset(4) + extra(12)
#   Datos   después de </msq>\r\n: [5 bytes prefijo][datos en orden de canales]
#
# Tipos:  0=uint8 (×1)   2=uint16 (×1)   3=uint16 (×0.1)   7=float32 BE
# Temps:  CLT y MAT se almacenan en °F×10 → convertir a °C

_MLG_MAGIC      = b'MLVLG'
_MLG_HDR_SIZE   = 24
_MLG_CH_SIZE    = 89
_MLG_REC_PREFIX = 5         # bytes de prefijo por record (flags/secuencia)
_MLG_TEMP_CH    = {'CLT', 'MAT'}   # almacenados en °F×10, MSL muestra °C

# Columnas MLG → clave interna (para load_mlg_full / diagnóstico de salud)
_MLG_FULL_MAP = {
    'RPM':                          'rpm',
    'MAP':                          'map',
    'AFR':                          'afr',
    'TPS':                          'tps',
    'CLT':                          'clt',
    'MAT':                          'mat',
    'Batt V':                       'batt',
    'SPK: Spark Advance':           'adv',
    'SPK: Knock retard':            'knock_retard',
    'SPK: MAT Retard':              'mat_retard',
    'SPK: Cold advance':            'cold_adv',
    'SPK: Idle Correction Advance': 'idle_corr_adv',
    'Fuel: Accel enrich':           'ae_pct',
    'Fuel: Warmup cor':             'wue',
    'Dwell':                        'dwell',
    'DutyCycle1':                   'duty_cycle',
    'PWM Idle Duty':                'iac_duty',
    'Lost Sync Count':              'lost_sync',
    'Timing Err%':                  'timing_err',
    'Barometer':                    'baro',
    'TPSdot':                       'tpsdot',
    'MAPdot':                       'mapdot',
    'RPMdot':                       'rpmdot',
    'Accel PW':                     'accel_pw',
    'PW':                           'pw',
    'SecL':                         'secl',
    'OilPressure':                  'oil_pressure',
}


def _mlg_ch_size(ctype: int) -> int:
    return {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 4}.get(ctype, 2)


def _mlg_to_physical(name: str, ctype: int, raw) -> float:
    """Convierte el valor raw MLG al valor físico con la escala correcta."""
    if ctype == 7:
        return float(raw)
    if ctype in (0, 1):
        return float(raw)
    if ctype == 2:
        return float(raw)          # uint16 escala 1:1 (RPM, SecL, etc.)
    if ctype == 3:
        val = raw / 10.0           # uint16 escala 0.1
        if name in _MLG_TEMP_CH:
            val = (val - 32.0) / 1.8   # °F × 10 → °C
        return val
    return float(raw)


def _mlg_parse_header(raw: bytes):
    """Parsea el header MLVLG. Retorna (channels, data_start) o (None, None)."""
    if not raw.startswith(_MLG_MAGIC):
        return None, None
    n_ch = struct.unpack_from('>H', raw, 20)[0]
    channels = []
    for i in range(n_ch):
        off = _MLG_HDR_SIZE + i * _MLG_CH_SIZE
        if off + _MLG_CH_SIZE > len(raw):
            break
        ctype = raw[off]
        name  = raw[off+1 : off+35].split(b'\x00')[0].decode('latin-1', errors='replace').strip()
        units = raw[off+35: off+69].split(b'\x00')[0].decode('latin-1', errors='replace').strip()
        channels.append({'type': ctype, 'name': name, 'units': units,
                         'size': _mlg_ch_size(ctype)})
    msq_end = raw.find(b'</msq>')
    if msq_end < 0:
        return channels, None
    data_start = msq_end + len(b'</msq>') + 2   # skip \r\n
    return channels, data_start


def _mlg_iter_records(raw: bytes, channels: list, data_start: int):
    """Itera los records binarios del MLG, yielding dicts con valores físicos."""
    rec_data  = sum(ch['size'] for ch in channels)
    rec_total = _MLG_REC_PREFIX + rec_data
    data      = raw[data_start:]
    n_rec     = len(data) // rec_total

    for r in range(n_rec):
        base = r * rec_total + _MLG_REC_PREFIX   # saltar prefijo
        row  = {}
        off  = base
        for ch in channels:
            sz    = ch['size']
            chunk = data[off: off + sz]
            if len(chunk) < sz:
                break
            ctype = ch['type']
            try:
                if ctype == 7:
                    val_raw = struct.unpack_from('>f', chunk)[0]
                elif sz == 1:
                    val_raw = chunk[0]
                elif sz == 2:
                    val_raw = struct.unpack_from('>H', chunk)[0]
                else:
                    val_raw = struct.unpack_from('>I', chunk)[0]
            except struct.error:
                break
            row[ch['name']] = _mlg_to_physical(ch['name'], ctype, val_raw)
            off += sz
        if row:
            yield row


def load_mlg_full(log_files: list) -> list:
    """Equivalente a load_msl_full para archivos .mlg (diagnóstico de salud)."""
    all_rows = []
    for fi, fname in enumerate(log_files):
        with open(fname, 'rb') as fh:
            raw = fh.read()
        channels, data_start = _mlg_parse_header(raw)
        if channels is None or data_start is None:
            print(f"  [!] No se pudo leer {os.path.basename(fname)} como MLG.")
            continue
        for row_raw in _mlg_iter_records(raw, channels, data_start):
            row = {key: row_raw.get(mlg_name)
                   for mlg_name, key in _MLG_FULL_MAP.items()}
            row['file_idx'] = fi
            all_rows.append(row)
    return all_rows


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


def load_inj_config(msq_path: str) -> dict:
    """Lee parámetros de inyección del .msq para calcular dead time por muestra."""
    with open(msq_path, errors='replace') as f:
        content = f.read()

    def get_scalar(name):
        m = re.search(rf'name="{name}"[^>]*>([\d.eE+-]+)</constant>', content)
        return float(m.group(1)) if m else None

    return {
        'inj_open':  get_scalar('injOpen') or 1.0,
        'batt_fac':  get_scalar('battFac') or 0.1,
        'volt_ref':  13.2,   # TunerStudio etiqueta injOpen como "@ 13.2V"
    }


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
        'taeRates':       get_values('taeRates'),
        'taeBins':        get_values('taeBins'),
        'maeRates':       get_values('maeRates'),
        'maeBins':        get_values('maeBins'),
        'taeTime':        get_scalar('taeTime'),
        'tpsThresh':      get_scalar('tpsThresh'),
        'mapThresh':      get_scalar('mapThresh'),      # umbral MAPdot para MAE
        'tpsProportion':  get_scalar('tpsProportion'),  # % TAE en el blend (100=solo TAE)
        'aeTaperTime':    get_scalar('aeTaperTime'),
        'aeEndPW':        get_scalar('aeEndPW'),
        'taeColdA':       get_scalar('taeColdA'),
        'taeColdM':       get_scalar('taeColdM'),
    }


_POST_AE_COOLDOWN_SECS = 1.5   # segundos de mezcla inestable tras apagarse el AE
_MAP_HISTORY_SECS      = 2.0   # ventana temporal del historial MAP
_MAP_STABILITY_KPA     = 5.0   # rango máximo (max-min) admitido en la ventana; más → transitorio


def load_msl_logs(log_files: list, include_idle: bool = False) -> list:
    """Parsea uno o más .msl (texto tab-delimitado con header binario).

    include_idle: si True, incluye filas de ralentí estable (TPS<3%, CLT>70°C,
    RPM 600-1200, sin AE, MAP estable) además de las de carga normal.
    """
    all_rows = []
    _last_ae_secl: dict = {}   # fi -> SecL del último frame con AE activo
    _map_hist:     dict = {}   # fi -> deque[(secl, map)] para detectar reversión
    for fi, fname in enumerate(log_files):
        with open(fname, 'rb') as fh:
            raw = fh.read()

        # ── Formato MLG (binario MLVLG) ──────────────────────────
        if raw.startswith(_MLG_MAGIC):
            channels, data_start = _mlg_parse_header(raw)
            if channels is None or data_start is None:
                print(f"  [!] {os.path.basename(fname)}: MLG sin sección de datos, omitiendo.")
                continue
            rec_size = _MLG_REC_PREFIX + sum(ch['size'] for ch in channels)
            n_rec    = (len(raw) - data_start) // rec_size
            print(f"  {os.path.basename(fname)}: {len(channels)} canales, {n_rec} records (MLG)")
            for row_raw in _mlg_iter_records(raw, channels, data_start):
                rpm      = row_raw.get('RPM', 0)
                afr      = row_raw.get('AFR', 0)
                clt      = row_raw.get('CLT', 0)
                tps      = row_raw.get('TPS', 0)
                accel_pw = row_raw.get('Accel PW', 0)
                rpmdot   = row_raw.get('RPMdot', 0)
                mat      = row_raw.get('MAT')
                if rpm < 400 or not (8.0 < afr < 20.0) or clt < 70:
                    continue
                secl_val = row_raw.get('SecL', 0) or 0
                if tps < 3.0:
                    if include_idle:
                        mapdot = row_raw.get('MAPdot', 0)
                        if not (clt > 70 and 600 < rpm < 1200
                                and accel_pw <= 0.05 and abs(mapdot) < 15):
                            continue
                    else:
                        continue
                else:
                    if accel_pw > 0.05:
                        _last_ae_secl[fi] = secl_val
                        continue
                    if abs(rpmdot) > 400:
                        continue
                    mapdot = row_raw.get('MAPdot', 0)
                    if abs(mapdot) > 40:
                        continue
                    if secl_val - _last_ae_secl.get(fi, -9999) < _POST_AE_COOLDOWN_SECS:
                        continue
                if mat is not None and not (38.0 <= mat <= 58.0):
                    continue
                map_val = row_raw.get('MAP', 0)
                _mh = _map_hist.setdefault(fi, deque())
                _mh.append((secl_val, map_val))
                while _mh and secl_val - _mh[0][0] > _MAP_HISTORY_SECS:
                    _mh.popleft()
                if len(_mh) >= 2:
                    _map_vals = [m for _, m in _mh]
                    if max(_map_vals) - min(_map_vals) > _MAP_STABILITY_KPA:
                        continue
                all_rows.append({
                    'rpm':      rpm,
                    'map':      map_val,
                    'afr':      afr,
                    'tps':      tps,
                    'clt':      clt,
                    'mat':      mat,
                    'tpsdot':   row_raw.get('TPSdot', 0),
                    'mapdot':   row_raw.get('MAPdot', 0),
                    'rpmdot':   rpmdot,
                    'accel_pw': accel_pw,
                    'ego_cor':  row_raw.get('EGO cor1', 100.0),
                    'afr_tgt':  row_raw.get('AFR Target 1'),
                    'secl':     row_raw.get('SecL', 0) or 0,
                    'batt_v':   row_raw.get('Batt V'),
                    'pw':       row_raw.get('PW'),
                    'file_idx': fi,
                })
            continue   # no procesar como MSL

        # ── Formato MSL (texto tab-delimitado con header binario) ─
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
                    'rpmdot':   float(parts[cols['RPMdot']])   if 'RPMdot'    in cols else 0.0,
                    'accel_pw': float(parts[cols['Accel PW']])      if 'Accel PW'     in cols else 0.0,
                    'ego_cor':  float(parts[cols['EGO cor1']])      if 'EGO cor1'     in cols else 100.0,
                    'afr_tgt':  float(parts[cols['AFR Target 1']])  if 'AFR Target 1' in cols else None,
                    'secl':     float(parts[cols['SecL']])          if 'SecL'         in cols else 0.0,
                    'batt_v':   float(parts[cols['Batt V']])        if 'Batt V'       in cols else None,
                    'pw':       float(parts[cols['PW']])            if 'PW'           in cols else None,
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
            else:
                # Carga normal: excluir transitorios de AE y desaceleración fuerte.
                if row.get('accel_pw', 0) > 0.05:
                    _last_ae_secl[fi] = row.get('secl', 0)
                    continue
                if abs(row.get('rpmdot', 0)) > 400:
                    continue
                if abs(row.get('mapdot', 0)) > 40:
                    continue
                # Excluir el período de estabilización post-AE: la mezcla tarda
                # ~1.5 s en volver al estado estacionario tras apagarse el enriquecimiento.
                if row.get('secl', 0) - _last_ae_secl.get(fi, -9999) < _POST_AE_COOLDOWN_SECS:
                    continue
            # Filtro MAT: solo rango térmico estabilizado (evita oscilación por densidad)
            mat = row.get('mat')
            if mat is not None and not (38.0 <= mat <= 58.0):
                continue
            _secl = row.get('secl', 0)
            _mval = row.get('map', 0)
            _mh = _map_hist.setdefault(fi, deque())
            _mh.append((_secl, _mval))
            while _mh and _secl - _mh[0][0] > _MAP_HISTORY_SECS:
                _mh.popleft()
            if len(_mh) >= 2:
                _map_vals = [m for _, m in _mh]
                if max(_map_vals) - min(_map_vals) > _MAP_STABILITY_KPA:
                    continue
            row['file_idx'] = fi
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
    if map_kpa <= 100: return 13.0   # WOT NA
    if map_kpa <= 130: return 12.5   # boost bajo / transición (~1-4 PSI)
    if map_kpa <= 165: return 12.0   # boost medio (~7 PSI)
    return 11.5                       # boost alto (~12 PSI)


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


def _dwell_filter(samples: list, min_seconds: int = 2) -> tuple[list, list]:
    """
    Filtra samples de una celda VE por permanencia mínima.

    Retorna (stable_flat, stable_groups):
      - stable_flat  : lista plana de todos los samples que pasaron el filtro
      - stable_groups: lista de grupos, cada grupo es un dict con:
          'samples'   : lista de samples del tramo
          'secl_start': primer SecL del tramo
          'secl_end'  : último SecL del tramo
          'n_secs'    : segundos únicos del tramo
          'n'         : cantidad de samples

    SecL en MegaSquirt es un entero en segundos (no sub-segundo): a 20 Hz hay
    ~20 muestras por segundo con el mismo SecL. Por eso el criterio usa
    segundos distintos, no cantidad de muestras.

    Un grupo pasa si abarca al menos min_seconds segundos distintos consecutivos
    (máximo salto de 1 s entre segundos adyacentes del grupo).

    Si todos los SecL son 0 (log sin columna SecL), se omite el filtro y se
    devuelve un único grupo con todos los samples.
    """
    if not samples:
        return [], []
    if max(s['secl'] for s in samples) == 0:
        return samples, [{'samples': samples, 'secl_start': 0, 'secl_end': 0,
                          'n_secs': 0, 'n': len(samples)}]

    s = sorted(samples, key=lambda x: (x['fi'], x['secl']))

    # Agrupar por archivo y segundos consecutivos (gap ≤ 1 s)
    raw_groups = []
    group = [s[0]]
    for i in range(1, len(s)):
        same_file = s[i]['fi'] == s[i - 1]['fi']
        gap_ok    = s[i]['secl'] - s[i - 1]['secl'] <= 1
        if same_file and gap_ok:
            group.append(s[i])
        else:
            raw_groups.append(group)
            group = [s[i]]
    raw_groups.append(group)

    stable_flat   = []
    stable_groups = []
    for g in raw_groups:
        secs = sorted({x['secl'] for x in g})
        if len(secs) < min_seconds:
            continue
        stable_flat.extend(g)
        stable_groups.append({
            'samples':    g,
            'secl_start': secs[0],
            'secl_end':   secs[-1],
            'n_secs':     len(secs),
            'n':          len(g),
        })

    return stable_flat, stable_groups


def analyze(rows: list, ve_data: dict, ae_cfg: dict,
            min_samples: int = 5, history: list = None,
            inj_cfg: dict = None) -> dict:
    """Calcula AFR promedio por celda, separando muestras con/sin AE activo."""
    rpm_bins   = ve_data['rpm_bins']
    map_bins   = ve_data['map_bins']
    ve         = ve_data['ve']
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0
    history    = history or []
    inj_open   = (inj_cfg or {}).get('inj_open', 1.0)
    batt_fac   = (inj_cfg or {}).get('batt_fac', 0.1)
    volt_ref   = (inj_cfg or {}).get('volt_ref', 13.2)
    MIN_EFF_PW = 0.5   # ms — por debajo de esto el inyector no entrega combustible controlable

    # Separar muestras
    ae_on  = [r for r in rows if r['accel_pw'] > 0.05]
    ae_off = [r for r in rows if r['accel_pw'] <= 0.05]
    # Falsos positivos AE: accel_pw > 0 pero tpsdot bajo (período de tapering)
    ae_taper = [r for r in ae_on if abs(r['tpsdot']) < tps_thresh]

    # Acumular samples por celda (solo sin AE para correcciones limpias)
    # Guardamos dicts completos para poder aplicar el filtro de permanencia.
    cell_samples = {}
    for row in ae_off:
        mi = find_bin(row['map'], map_bins)
        ri = find_bin(row['rpm'], rpm_bins)
        cell_samples.setdefault((mi, ri), []).append({
            'afr':     row['afr'],
            'afr_tgt': row.get('afr_tgt'),
            'rpm':     row['rpm'],
            'fi':      row.get('file_idx', 0),
            'secl':    row.get('secl', 0) or 0,
            'batt_v':  row.get('batt_v'),
            'pw':      row.get('pw'),
        })

    # Calcular correcciones
    lean_cells = []
    rich_cells = []
    ok_cells   = []
    skipped    = []

    for (mi, ri), raw_samples in sorted(cell_samples.items()):
        m   = map_bins[mi]
        r   = rpm_bins[ri]
        req = zone_min_samples(m, min_samples)

        # Filtro de permanencia mínima: descartar "drive-throughs"
        stable, stable_groups = _dwell_filter(raw_samples)
        afrs   = [s['afr'] for s in stable]

        if len(afrs) < req:
            continue

        # Chequeo de piso de inyector: si el PW efectivo mediano es menor que
        # MIN_EFF_PW el inyector no entrega combustible controlable y la lectura
        # del wideband no refleja el VE de la celda.
        eff_pws = []
        for s in stable:
            bv = s.get('batt_v')
            pw = s.get('pw')
            if bv is not None and pw is not None:
                dt = inj_open + batt_fac * (volt_ref - bv)
                eff_pws.append(pw - dt)
        if eff_pws:
            eff_pws_sorted = sorted(eff_pws)
            ep_mid = len(eff_pws_sorted) // 2
            eff_pw_med = (eff_pws_sorted[ep_mid - 1] + eff_pws_sorted[ep_mid]) / 2 \
                         if len(eff_pws_sorted) % 2 == 0 else eff_pws_sorted[ep_mid]
            if eff_pw_med < MIN_EFF_PW:
                skipped.append({
                    'mi': mi, 'ri': ri, 'map': m, 'rpm': r,
                    'afr_avg': 0, 'target': 0, 'n': len(afrs), 'n_secs': 0,
                    'n_raw': len(raw_samples), 've_cur': ve[mi][ri], 've_new': ve[mi][ri],
                    'delta': 0, 'damped': False, 'groups': [],
                    'skip_reason': f'piso inyector (eff_pw={eff_pw_med:.2f}ms)',
                })
                continue

        # Mediana en vez de media — más robusta ante spikes de sonda lambda
        afrs_sorted = sorted(afrs)
        mid = len(afrs_sorted) // 2
        avg = (afrs_sorted[mid - 1] + afrs_sorted[mid]) / 2 if len(afrs_sorted) % 2 == 0 \
              else afrs_sorted[mid]

        # AFR target: mediana de "AFR Target 1" del log; si no hay, función interna
        tgt_vals = [s['afr_tgt'] for s in stable if s.get('afr_tgt') is not None]
        if tgt_vals:
            tgt_sorted = sorted(tgt_vals)
            tmid = len(tgt_sorted) // 2
            tgt = (tgt_sorted[tmid - 1] + tgt_sorted[tmid]) / 2 if len(tgt_sorted) % 2 == 0 \
                  else tgt_sorted[tmid]
        else:
            tgt = target_afr(m)
        vc    = ve[mi][ri]
        raw_delta = vc * avg / tgt - vc
        damp  = cell_damping(mi, ri, history)
        # Cap por sesión: máximo 10 VE units — evita picos por transitorios o pocas muestras
        MAX_DELTA = 10
        delta = max(-MAX_DELTA, min(MAX_DELTA, round(raw_delta * damp)))
        vn    = round(vc) + delta

        n_secs = len({s['secl'] for s in stable if s.get('secl', 0) > 0})

        # Stats por grupo para vista detallada
        def _grp_median(vals):
            sv = sorted(vals)
            mid = len(sv) // 2
            return (sv[mid - 1] + sv[mid]) / 2 if len(sv) % 2 == 0 else sv[mid]

        groups_detail = []
        for g in stable_groups:
            g_afrs = [s['afr'] for s in g['samples']]
            groups_detail.append({
                'secl_start': g['secl_start'],
                'secl_end':   g['secl_end'],
                'n_secs':     g['n_secs'],
                'n':          g['n'],
                'afr_med':    round(_grp_median(g_afrs), 2),
                'afr_min':    round(min(g_afrs), 2),
                'afr_max':    round(max(g_afrs), 2),
            })

        entry = {
            'mi': mi, 'ri': ri, 'map': m, 'rpm': r,
            'afr_avg': avg, 'target': tgt, 'n': len(afrs), 'n_secs': n_secs,
            'n_raw': len(raw_samples),
            've_cur': vc, 've_new': vn, 'delta': delta,
            'damped': damp < 1.0,
            'groups': groups_detail,
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

def print_cell_detail(cell: dict) -> None:
    """Imprime breakdown de tramos temporales para una celda."""
    groups = cell.get('groups', [])
    if not groups:
        print("    (sin datos de tramos)")
        return
    print(f"    {'Tramo':>6}  {'SecL ini':>8}  {'SecL fin':>8}  {'dur(s)':>6}  "
          f"{'n':>4}  {'AFR med':>7}  {'AFR min':>7}  {'AFR max':>7}")
    print("    " + "-"*62)
    for i, g in enumerate(groups, 1):
        print(f"    {i:>6}  {g['secl_start']:>8}  {g['secl_end']:>8}  "
              f"{g['n_secs']:>6}  {g['n']:>4}  "
              f"{g['afr_med']:>7.2f}  {g['afr_min']:>7.2f}  {g['afr_max']:>7.2f}")
    print(f"    {'─'*62}")
    print(f"    {'GLOBAL':>6}  {'':>8}  {'':>8}  "
          f"{cell['n_secs']:>6}  {cell['n']:>4}  "
          f"{cell['afr_avg']:>7.2f}  {'':>7}  {'':>7}  "
          f"target={cell['target']:.1f}  Δ={cell['delta']:+d}")
    print()


def print_report(result: dict, ae_cfg: dict, log_files: list, ve_data: dict,
                 include_idle: bool = False, detail: bool = False):
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
    afr_on_str  = f"{ae['afr_on_avg']:.2f}"  if ae['afr_on_avg']  is not None else "N/A"
    afr_off_str = f"{ae['afr_off_avg']:.2f}" if ae['afr_off_avg'] is not None else "N/A"
    print(f"  Con AE activo   : {ae['ae_on_pct']:.1f}%  "
          f"(AFR avg {afr_on_str}  — "
          f"pobres {ae['lean_on_pct']:.0f}%  ricos {ae['rich_on_pct']:.0f}%)")
    print(f"  Sin AE          : {ae['ae_off_pct']:.1f}%  "
          f"(AFR avg {afr_off_str}  — "
          f"pobres {ae['lean_off_pct']:.0f}%)")
    print(f"  Tapering (tpsdot<thresh con AE>0): {ae['taper_pct']:.0f}% del AE total")
    print()

    def _fmt_n(c):
        n, s = c['n'], c.get('n_secs', 0)
        return f"{n}(~{s}s)" if s else str(n)

    def table_header():
        print(f"  {'MAP':>5} {'RPM':>6} {'AFR':>7} {'Target':>7} {'n(~s)':>9}  "
              f"{'VE_act':>6} {'VE_nuevo':>8} {'Δ':>4}")
        print("  " + "-"*58)

    if lean:
        print(f"── ZONAS POBRES (AFR>14.5) — {len(lean)} celdas ──────────")
        table_header()
        for c in sorted(lean, key=lambda x: -x['delta']):
            print(f"  {c['map']:>5.0f} {c['rpm']:>6} {c['afr_avg']:>7.2f} "
                  f"{c['target']:>7.1f} {_fmt_n(c):>9}  "
                  f"{c['ve_cur']:>6.0f} {c['ve_new']:>8} {c['delta']:>+4}")
            if detail:
                print_cell_detail(c)
        print()

    if rich:
        print(f"── ZONAS RICAS (AFR<13.0) — {len(rich)} celdas ──────────")
        table_header()
        for c in sorted(rich, key=lambda x: x['delta']):
            print(f"  {c['map']:>5.0f} {c['rpm']:>6} {c['afr_avg']:>7.2f} "
                  f"{c['target']:>7.1f} {_fmt_n(c):>9}  "
                  f"{c['ve_cur']:>6.0f} {c['ve_new']:>8} {c['delta']:>+4}")
            if detail:
                print_cell_detail(c)
        print()

    if not lean and not rich:
        print("  ✓ Sin zonas fuera de objetivo. Mezcla dentro de rango.\n")

    skipped = result.get('skipped', [])
    floor_cells  = [c for c in skipped if c.get('skip_reason', '').startswith('piso')]
    dband_cells  = [c for c in skipped if not c.get('skip_reason', '').startswith('piso')]
    if floor_cells:
        print(f"── PISO DE INYECTOR (sin corrección) — {len(floor_cells)} celdas ──")
        for c in sorted(floor_cells, key=lambda x: (x['map'], x['rpm'])):
            print(f"  MAP={c['map']:5.0f} RPM={c['rpm']:5d}  "
                  f"VE={c['ve_cur']:.0f}  {c['skip_reason']}")
        print()
    if dband_cells:
        print(f"── IGNORADAS (dead band / amortiguadas) — {len(dband_cells)} celdas ──")
        for c in sorted(dband_cells, key=lambda x: (x['map'], x['rpm'])):
            damp_tag = ' [amortiguada]' if c.get('damped') else ''
            print(f"  MAP={c['map']:5.0f} RPM={c['rpm']:5d}  "
                  f"AFR={c['afr_avg']:.2f}  Δ={c['delta']:+d}{damp_tag}")
        print()


# ─────────────────────────────────────────────
# 4. GENERACIÓN DEL .table CORREGIDO
# ─────────────────────────────────────────────

def generate_table(result: dict, ve_data: dict, out_path: str):
    """Aplica correcciones al VE y guarda un nuevo .table con metadatos de anclas."""
    import copy
    ve_new = copy.deepcopy(ve_data['ve'])

    corrected_cells = []
    for c in result['lean'] + result['rich']:
        ve_new[c['mi']][c['ri']] = float(c['ve_new'])
        corrected_cells.append((c['mi'], c['ri']))

    # Incluir anclas heredadas de sesiones anteriores (acumulativo)
    inherited = set(result.get('inherited_anchors', []))
    all_anchors = inherited | set(corrected_cells)
    anchors_str = ','.join(f"{mi}:{ri}" for mi, ri in sorted(all_anchors))

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
<anchors>{anchors_str}</anchors>
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
    print(f"  Anclas acumuladas embebidas: {len(all_anchors)} celdas")


# ─────────────────────────────────────────────
# 5. HISTORIAL DE CORRECCIONES
# ─────────────────────────────────────────────

def _parse_table_file(path: str) -> dict | None:
    """Lee bins, valores VE y anclas embebidas de un .table XML."""
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

    # Leer anclas embebidas (generadas por generate_table a partir de v1.7.1)
    anchors: set = set()
    am = re.search(r'<anchors>(.*?)</anchors>', content)
    if am and am.group(1).strip():
        for token in am.group(1).strip().split(','):
            parts = token.strip().split(':')
            if len(parts) == 2:
                try:
                    anchors.add((int(parts[0]), int(parts[1])))
                except ValueError:
                    pass

    return {'rpm_bins': rpm_bins, 'map_bins': map_bins,
            'values': values, 'anchors': anchors}


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


def smooth_table(project_dir: str, table_num: int,
                 max_delta: float = 3.0, passes: int = 2) -> None:
    """
    Suaviza la tabla VE con límite de cambio máximo por celda.

    Todas las celdas se mueven suavemente hacia el promedio de sus 4 vecinos
    cardinales, pero nunca más de max_delta puntos VE por pasada.
    - Picos genuinos se preservan (solo ceden max_delta pts por paso)
    - Ruido de celda única (1-3 pts) se corrige en 1-2 pasadas
    - No necesita clasificar celdas en anclas vs libres
    """
    pattern = os.path.join(project_dir, f'veTable{table_num}Tbl_*_corrected.table')
    files   = sorted(glob.glob(pattern), key=os.path.getmtime)

    if not files:
        print("No se encontraron archivos _corrected.table en el directorio.")
        return

    latest = _parse_table_file(files[-1])
    if not latest:
        print(f"Error leyendo {files[-1]}")
        return

    n_cols  = len(latest['rpm_bins'])
    n_rows  = len(latest['map_bins'])
    n_total = n_rows * n_cols

    print(f"\n── SUAVIZADO DE TABLA VE ────────────────────────────")
    print(f"  Base:          {os.path.basename(files[-1])}")
    print(f"  Pasadas:       {passes}")
    print(f"  Cambio máx:    ±{max_delta} pts VE por pasada")

    ve   = [[latest['values'][mi * n_cols + ri] for ri in range(n_cols)]
            for mi in range(n_rows)]
    orig = [row[:] for row in ve]

    for _pass in range(passes):
        ve_next = [row[:] for row in ve]
        for mi in range(n_rows):
            for ri in range(n_cols):
                neighbors = []
                for nmi, nri in [(mi - 1, ri), (mi + 1, ri),
                                 (mi, ri - 1), (mi, ri + 1)]:
                    if 0 <= nmi < n_rows and 0 <= nri < n_cols:
                        neighbors.append(ve[nmi][nri])
                if not neighbors:
                    continue
                navg  = sum(neighbors) / len(neighbors)
                delta = navg - ve[mi][ri]
                # Mover hacia el promedio pero no más de max_delta
                move  = max(-max_delta, min(max_delta, delta))
                ve_next[mi][ri] = round(ve[mi][ri] + move, 1)
        ve = ve_next

    # ── Reporte de cambios ──
    changed = [(mi, ri, orig[mi][ri], ve[mi][ri])
               for mi in range(n_rows) for ri in range(n_cols)
               if abs(ve[mi][ri] - orig[mi][ri]) >= 0.5]
    print(f"  Celdas modificadas (≥0.5 pts): {len(changed)}/{n_total}")
    if changed:
        deltas = [abs(v_new - v_old) for _, _, v_old, v_new in changed]
        print(f"  Delta promedio: {sum(deltas)/len(deltas):.1f}  "
              f"Delta máximo: {max(deltas):.1f}")

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


def predict_uncovered_cells(project_dir: str, table_num: int) -> None:
    """
    Predice VE para celdas sin cobertura de logs usando el patrón de
    correcciones observado en las celdas con datos.

    Estrategia:
    1. Carga zero.table (punto de partida antes de cualquier corrección)
    2. Carga el último _corrected.table (estado calibrado actual)
    3. Identifica celdas calibradas (con datos de log) via historial
    4. Calcula delta[mi][ri] = current - zero  para celdas calibradas
    5. Interpola la superficie de deltas a las celdas sin datos (Laplaciano)
    6. predicted[mi][ri] = zero[mi][ri] + delta_interpolado[mi][ri]
    7. Las celdas calibradas quedan intactas; solo se tocan las sin datos
    """
    # ── Archivos ──
    zero_path = os.path.join(project_dir, 'zero.table')
    if not os.path.exists(zero_path):
        print("  ERROR: no se encontró zero.table en el directorio.")
        print("  Copia tu tabla de partida como 'zero.table' en la carpeta de tablas.")
        return

    pattern = os.path.join(project_dir, f'veTable{table_num}Tbl_*_corrected.table')
    files   = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        print("  No se encontraron archivos _corrected.table.")
        return

    zero    = _parse_table_file(zero_path)
    current = _parse_table_file(files[-1])
    if not zero or not current:
        print("  Error leyendo tablas.")
        return

    n_cols = len(current['rpm_bins'])
    n_rows = len(current['map_bins'])

    if len(zero['values']) != len(current['values']):
        print("  zero.table y el _corrected.table tienen dimensiones distintas.")
        return

    # ── Celdas calibradas (anclas del delta) ──
    # Fuente 1: anclas embebidas en los _corrected.table
    calibrated: set = set()
    for path in files:
        parsed = _parse_table_file(path)
        if parsed and parsed.get('anchors'):
            calibrated |= parsed['anchors']
    # Fuente 2: fallback a diffs consecutivos
    if not calibrated:
        history = load_history_from_tables(project_dir, table_num)
        freq = {}
        for s in history:
            for c in s['corrections']:
                k = (c['mi'], c['ri'])
                freq[k] = freq.get(k, 0) + 1
                calibrated.add(k)
    else:
        history = load_history_from_tables(project_dir, table_num)
        freq = {}
        for s in history:
            for c in s['corrections']:
                k = (c['mi'], c['ri'])
                freq[k] = freq.get(k, 0) + 1

    calibrated = {(mi, ri) for mi, ri in calibrated
                  if 0 <= mi < n_rows and 0 <= ri < n_cols}
    uncovered  = {(mi, ri)
                  for mi in range(n_rows) for ri in range(n_cols)
                  if (mi, ri) not in calibrated}

    print(f"\n── PREDICCIÓN DE CELDAS SIN COBERTURA ──────────────")
    print(f"  Base zero:     {os.path.basename(zero_path)}")
    print(f"  Estado actual: {os.path.basename(files[-1])}")
    print(f"  Celdas con datos (anclas): {len(calibrated)}/256")
    print(f"  Celdas a predecir:         {len(uncovered)}/256")

    if not calibrated:
        print("\n  No hay celdas calibradas. Toma logs primero.")
        return

    # ── Grid de deltas ──
    # Para celdas calibradas: delta real (ancla fija)
    # Para celdas sin datos: inicializar a 0, luego Laplaciano
    delta = [[current['values'][mi * n_cols + ri] - zero['values'][mi * n_cols + ri]
              for ri in range(n_cols)]
             for mi in range(n_rows)]

    # Resetear deltas de celdas no calibradas a 0 (serán interpoladas)
    for mi in range(n_rows):
        for ri in range(n_cols):
            if (mi, ri) not in calibrated:
                delta[mi][ri] = 0.0

    # ── Interpolación Laplaciana sobre superficie de deltas ──
    MAX_ITER = 2000
    TOL      = 0.02
    for iteration in range(MAX_ITER):
        max_change = 0.0
        delta_next = [row[:] for row in delta]
        for mi in range(n_rows):
            for ri in range(n_cols):
                if (mi, ri) in calibrated:
                    continue  # ancla — delta conocido, no tocar
                neighbors = []
                for nmi, nri in [(mi-1, ri), (mi+1, ri),
                                 (mi, ri-1), (mi, ri+1)]:
                    if 0 <= nmi < n_rows and 0 <= nri < n_cols:
                        neighbors.append(delta[nmi][nri])
                if neighbors:
                    new_val    = sum(neighbors) / len(neighbors)
                    max_change = max(max_change, abs(new_val - delta[mi][ri]))
                    delta_next[mi][ri] = new_val
        delta = delta_next
        if max_change < TOL:
            break

    # ── Construir tabla predicha ──
    # Calibradas: mantener current  |  No calibradas: zero + delta_interpolado
    ve_zero    = [[zero['values'][mi * n_cols + ri]    for ri in range(n_cols)]
                  for mi in range(n_rows)]
    ve_current = [[current['values'][mi * n_cols + ri] for ri in range(n_cols)]
                  for mi in range(n_rows)]

    ve_pred = [[0.0] * n_cols for _ in range(n_rows)]
    for mi in range(n_rows):
        for ri in range(n_cols):
            if (mi, ri) in calibrated:
                ve_pred[mi][ri] = ve_current[mi][ri]
            else:
                raw = ve_zero[mi][ri] + delta[mi][ri]
                ve_pred[mi][ri] = round(max(1.0, min(150.0, raw)), 1)

    # ── Calcular distancia mínima a celda calibrada (confianza) ──
    def min_dist(mi, ri):
        return min(((mi - cmi)**2 + (ri - cri)**2) ** 0.5
                   for cmi, cri in calibrated)

    # ── Reporte de celdas predichas ──
    predicted_cells = sorted(uncovered, key=lambda k: (k[0], k[1]))
    print(f"\n  {'MAP':>5}  {'RPM':>6}  {'zero':>5}  {'Δ_pred':>7}  {'pred':>5}  {'conf':>6}")
    print(f"  {'---':>5}  {'---':>6}  {'----':>5}  {'------':>7}  {'----':>5}  {'----':>6}")
    for mi, ri in predicted_cells:
        z   = ve_zero[mi][ri]
        d   = delta[mi][ri]
        p   = ve_pred[mi][ri]
        dist = min_dist(mi, ri)
        conf = "alta" if dist <= 1.5 else ("media" if dist <= 2.5 else "baja")
        print(f"  {current['map_bins'][mi]:>5.0f}  {current['rpm_bins'][ri]:>6}  "
              f"{z:>5.1f}  {d:>+7.1f}  {p:>5.1f}  {conf:>6}")

    # Resumen por confianza
    alta  = sum(1 for mi, ri in uncovered if min_dist(mi, ri) <= 1.5)
    media = sum(1 for mi, ri in uncovered if 1.5 < min_dist(mi, ri) <= 2.5)
    baja  = sum(1 for mi, ri in uncovered if min_dist(mi, ri) > 2.5)
    print(f"\n  Confianza alta  (dist ≤1.5 celdas): {alta}")
    print(f"  Confianza media (dist ≤2.5 celdas): {media}")
    print(f"  Confianza baja  (dist >2.5 celdas): {baja}")

    # ── Guardar _predicted.table ──
    ts  = datetime.now().strftime('%Y-%m-%d_%H.%M')
    out = os.path.join(project_dir, f'veTable{table_num}Tbl_{ts}_predicted.table')
    z_rows  = "\n".join(
        "         " + " ".join(f"{v:.1f}" for v in row) + " "
        for row in ve_pred)
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
        + "\n".join(f"         {r} " for r in current['rpm_bins']) + "\n"
        '      </xAxis>\n'
        f'<yAxis name="fuelload" rows="{n_rows}">\n'
        + "\n".join(f"         {m} " for m in current['map_bins']) + "\n"
        '      </yAxis>\n'
        f'<zValues cols="{n_cols}" rows="{n_rows}">\n'
        f'{z_rows}\n'
        '      </zValues>\n'
        '</table>\n'
        '</tableData>\n'
    )
    with open(out, 'w') as f:
        f.write(xml)
    print(f"\n  Tabla predicha guardada: {os.path.basename(out)}")
    print(f"  Importala en TunerStudio, toma logs en las zonas predichas")
    print(f"  y confirma con el análisis VE normal.")


def fuse_definitive_table(project_dir: str, table_num: int = 3,
                          base_percentile: float = 0.50,
                          near_radius: int = 4,
                          outlier_factor_threshold: float = 1.20,
                          max_gradient: float = 8.0,
                          blend_neighbor_weight: float = 0.20) -> None:
    """
    Genera una tabla VE definitiva fusionando TODAS las _corrected.table del flujo.

    Estrategia:
    1. T1 = primera _corrected (estado tras primer log)
       T6 = última _corrected (estado actual)
    2. Factor multiplicativo por celda: factor[mi][ri] = T6/T1
    3. Cap outliers: factor > outlier_factor_threshold → P75 normal
    4. Para celdas no tocadas: factor proyectado mezclando IDW (vecinos tocados,
       peso 1/d²) con factor base (P50 — mediana del lean global detectado).
       Mezcla suave α = max(0, 1 − (d_nearest−1)/(near_radius−1)).
    5. Aplicar factor: tocadas → T6 (con outlier limpiado), no tocadas → T6×factor
    6. Suavizar gradiente: máx max_gradient puntos VE entre celdas adyacentes
    7. Blend liviano (blend_neighbor_weight vecinos 3×3) solo en no tocadas

    Hipótesis: el motor está lean GLOBALMENTE por sesgo del reqFuel base. El
    lean detectado en celdas con datos se proyecta al resto de la tabla.
    """
    pattern = os.path.join(project_dir, f'veTable{table_num}Tbl_*_corrected.table')
    files   = sorted(glob.glob(pattern), key=os.path.getmtime)

    if len(files) < 2:
        print(f"  Se necesitan al menos 2 archivos _corrected.table "
              f"(encontrados: {len(files)})")
        return

    print(f"\n── FUSIÓN DEFINITIVA — Tabla VE{table_num} ─────────────────")
    print(f"  Tablas en flujo: {len(files)}")
    print(f"  Primera (T1) : {os.path.basename(files[0])}")
    print(f"  Última (Tn)  : {os.path.basename(files[-1])}")

    t1 = _parse_table_file(files[0])
    tn = _parse_table_file(files[-1])
    if not (t1 and tn):
        print("  Error leyendo archivos")
        return

    rpm_bins = t1['rpm_bins']
    map_bins = t1['map_bins']
    n_cols = len(rpm_bins)
    n_rows = len(map_bins)

    T1 = [t1['values'][mi*n_cols:(mi+1)*n_cols] for mi in range(n_rows)]
    T6 = [tn['values'][mi*n_cols:(mi+1)*n_cols] for mi in range(n_rows)]

    # ── 1. Factor por celda + identificar tocadas ──
    factor  = [[T6[mi][ri]/T1[mi][ri] if T1[mi][ri] > 0 else 1.0
                for ri in range(n_cols)] for mi in range(n_rows)]
    touched = [[abs(T6[mi][ri] - T1[mi][ri]) >= 0.5
                for ri in range(n_cols)] for mi in range(n_rows)]

    touched_cells = [(mi, ri) for mi in range(n_rows) for ri in range(n_cols)
                     if touched[mi][ri]]

    if not touched_cells:
        print("  No hay diferencias entre T1 y Tn → no hay datos para fusionar")
        return

    # ── 2. Percentiles de factor normal + cap outliers ──
    factors_normal = sorted(factor[mi][ri] for mi, ri in touched_cells
                            if factor[mi][ri] < outlier_factor_threshold)
    if not factors_normal:
        print("  Todos los factores son outliers → no se puede determinar P50")
        return

    def percentile(arr, p):
        return arr[min(int(len(arr)*p), len(arr)-1)]

    p25 = percentile(factors_normal, 0.25)
    p50 = percentile(factors_normal, 0.50)
    p75 = percentile(factors_normal, 0.75)
    base_factor = percentile(factors_normal, base_percentile)

    print(f"  Celdas con datos directos: {len(touched_cells)} / {n_rows*n_cols}")
    print(f"  Factores normales: P25=×{p25:.3f}  P50=×{p50:.3f}  P75=×{p75:.3f}")
    print(f"  Factor base aplicado a toda la tabla: ×{base_factor:.3f} "
          f"(percentil {base_percentile*100:.0f}, lean +{(base_factor-1)*100:.1f}%)")

    # Cap outliers
    clean_factor = [row[:] for row in factor]
    outliers_capped = []
    for mi in range(n_rows):
        for ri in range(n_cols):
            if factor[mi][ri] > outlier_factor_threshold:
                clean_factor[mi][ri] = p75
                outliers_capped.append((mi, ri, factor[mi][ri], p75))

    if outliers_capped:
        print(f"\n  Outliers capados (factor > ×{outlier_factor_threshold:.2f}):")
        for mi, ri, orig, capped in outliers_capped:
            ve_orig = T6[mi][ri]
            ve_new  = T1[mi][ri] * capped
            print(f"    MAP={map_bins[mi]:>5.0f} RPM={rpm_bins[ri]:>5}: "
                  f"×{orig:.3f} → ×{capped:.3f}  (VE {ve_orig:.0f} → {ve_new:.0f})")

    # ── 3. Proyección de factor a celdas no tocadas ──
    def cheb(a, b):
        return max(abs(a[0]-b[0]), abs(a[1]-b[1]))

    projected_factor = [[base_factor]*n_cols for _ in range(n_rows)]
    for mi in range(n_rows):
        for ri in range(n_cols):
            if touched[mi][ri]:
                projected_factor[mi][ri] = clean_factor[mi][ri]
                continue
            ws, fs = 0.0, 0.0
            nearest = None
            for tmi, tri in touched_cells:
                d = cheb((mi, ri), (tmi, tri))
                if nearest is None or d < nearest:
                    nearest = d
                if d > near_radius:
                    continue
                w = 1.0 / (d * d)
                ws += w * clean_factor[tmi][tri]
                fs += w
            if fs > 0:
                idw = ws / fs
                # Mezcla IDW (cerca) ↔ base_factor (lejos)
                alpha = max(0.0, 1.0 - (nearest - 1) / max(near_radius - 1, 1))
                projected_factor[mi][ri] = idw*alpha + base_factor*(1.0 - alpha)
            else:
                projected_factor[mi][ri] = base_factor

    # ── 4. Aplicar factor ──
    ve_proj = [[0.0]*n_cols for _ in range(n_rows)]
    for mi in range(n_rows):
        for ri in range(n_cols):
            if touched[mi][ri] and factor[mi][ri] > outlier_factor_threshold:
                ve_proj[mi][ri] = T1[mi][ri] * clean_factor[mi][ri]
            elif touched[mi][ri]:
                ve_proj[mi][ri] = T6[mi][ri]
            else:
                ve_proj[mi][ri] = T6[mi][ri] * projected_factor[mi][ri]

    # ── 5. Suavizado de gradiente máx max_gradient entre adyacentes ──
    ve_smooth = [row[:] for row in ve_proj]
    for _it in range(30):
        chgd = False
        for mi in range(n_rows):
            for ri in range(n_cols):
                for nmi, nri in [(mi-1, ri), (mi+1, ri), (mi, ri-1), (mi, ri+1)]:
                    if not (0 <= nmi < n_rows and 0 <= nri < n_cols):
                        continue
                    diff = ve_smooth[mi][ri] - ve_smooth[nmi][nri]
                    if abs(diff) > max_gradient:
                        # Mover preferentemente celdas sin datos directos
                        if not touched[mi][ri] and touched[nmi][nri]:
                            ve_smooth[mi][ri] = ve_smooth[nmi][nri] + (
                                max_gradient if diff > 0 else -max_gradient)
                        elif touched[mi][ri] and not touched[nmi][nri]:
                            ve_smooth[nmi][nri] = ve_smooth[mi][ri] + (
                                -max_gradient if diff > 0 else max_gradient)
                        else:
                            if diff > 0:
                                ve_smooth[mi][ri] -= 0.5
                                ve_smooth[nmi][nri] += 0.5
                            else:
                                ve_smooth[mi][ri] += 0.5
                                ve_smooth[nmi][nri] -= 0.5
                        chgd = True
        if not chgd:
            break

    # ── 6. Blend liviano (1 pasada) solo en celdas no tocadas ──
    def neigh_avg_3x3(grid, mi, ri):
        vals = []
        for d_mi in (-1, 0, 1):
            for d_ri in (-1, 0, 1):
                if d_mi == 0 and d_ri == 0:
                    continue
                nmi, nri = mi + d_mi, ri + d_ri
                if 0 <= nmi < n_rows and 0 <= nri < n_cols:
                    vals.append(grid[nmi][nri])
        return sum(vals)/len(vals) if vals else None

    new = [row[:] for row in ve_smooth]
    for mi in range(n_rows):
        for ri in range(n_cols):
            if touched[mi][ri]:
                continue
            navg = neigh_avg_3x3(ve_smooth, mi, ri)
            if navg is None:
                continue
            new[mi][ri] = ve_smooth[mi][ri] * (1.0 - blend_neighbor_weight) \
                          + navg * blend_neighbor_weight
    ve_final = [[round(new[mi][ri], 1) for ri in range(n_cols)]
                for mi in range(n_rows)]

    # ── Estadísticas finales ──
    cells_changed = 0
    cells_projected = 0
    for mi in range(n_rows):
        for ri in range(n_cols):
            if abs(ve_final[mi][ri] - T6[mi][ri]) >= 0.5:
                cells_changed += 1
                if not touched[mi][ri]:
                    cells_projected += 1

    print(f"\n  Celdas modificadas vs Tn: {cells_changed} / {n_rows*n_cols}")
    print(f"  De ellas, proyectadas (sin datos directos): {cells_projected}")

    # Picos residuales
    peaks = []
    for mi in range(n_rows):
        for ri in range(n_cols):
            avg = neigh_avg_3x3(ve_final, mi, ri)
            if avg is not None and abs(ve_final[mi][ri] - avg) > 5:
                peaks.append((mi, ri, ve_final[mi][ri], avg))
    if peaks:
        print(f"\n  Picos residuales (|cell − vecinos 3×3| > 5): {len(peaks)}")
        for mi, ri, v, avg in peaks:
            print(f"    MAP={map_bins[mi]:>5.0f} RPM={rpm_bins[ri]:>5}: "
                  f"VE={v:.1f}  vecinos={avg:.1f}  Δ={v-avg:+.1f}")

    # ── Guardar .table ──
    ts  = datetime.now().strftime('%Y-%m-%d_%H.%M')
    out = os.path.join(project_dir,
                       f'veTable{table_num}Tbl_{ts}_definitive.table')

    z_rows = "\n".join(
        "         " + " ".join(f"{v:.1f}" for v in row) + " "
        for row in ve_final
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
        + "\n".join(f"         {r} " for r in rpm_bins) + "\n"
        '      </xAxis>\n'
        f'<yAxis name="fuelload" rows="{n_rows}">\n'
        + "\n".join(f"         {m} " for m in map_bins) + "\n"
        '      </yAxis>\n'
        f'<zValues cols="{n_cols}" rows="{n_rows}">\n'
        f'{z_rows}\n'
        '      </zValues>\n'
        '</table>\n'
        '</tableData>\n'
    )
    with open(out, 'w') as f:
        f.write(xml)

    print(f"\n  Tabla definitiva guardada: {os.path.basename(out)}")
    print(f"  Importar a TunerStudio → Tabla VE {table_num} → Save MSQ.")


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
        'RPMdot':                     'rpmdot',
        'Accel PW':                   'accel_pw',
        'PW':                         'pw',
        'SecL':                       'secl',
        'fuel_pressure':              'fuel_pressure',
        'OilPressure':                'oil_pressure',
    }
    all_rows = []
    for fi, fname in enumerate(log_files):
        with open(fname, 'rb') as fh:
            raw = fh.read()

        # ── Formato MLG ──────────────────────────────────────────
        if raw.startswith(_MLG_MAGIC):
            channels, data_start = _mlg_parse_header(raw)
            if channels and data_start is not None:
                for row_raw in _mlg_iter_records(raw, channels, data_start):
                    row = {key: row_raw.get(mlg_name)
                           for mlg_name, key in _MLG_FULL_MAP.items()}
                    row['file_idx'] = fi
                    all_rows.append(row)
            continue

        # ── Formato MSL ──────────────────────────────────────────
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
    # Motor en marcha (no cranking) — cap RPM < 10000 descarta registros MLG basura
    running   = [r for r in rows if 600 < (r.get('rpm') or 0) < 10000]
    # Cranking: motor intentando arrancar
    cranking  = [r for r in rows if 50 < (r.get('rpm') or 0) <= 600]
    # Motor caliente en marcha
    warm_run  = [r for r in running if (r.get('clt') or 0) > 70]
    # Motor frío en marcha (incluye calentamiento)
    cold_run  = [r for r in rows if 200 < (r.get('rpm') or 0) and (r.get('clt') or 99) < 60]

    health = {}

    # ── VOLTAJE ──
    # Solo evaluado con motor en marcha — cranking siempre baja voltaje (normal)
    # Filtro 5-30V: excluye valores basura de registros MLG con motor apagado
    batt_run  = [v for v in fv('batt', running)  if 5 < v < 30]
    batt_warm = [v for v in fv('batt', warm_run) if 5 < v < 30]
    batt_all  = [v for v in fv('batt')           if 5 < v < 30]
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

    # ── REGULADOR DE PRESIÓN 1:1 ──
    # Con sensor de presión disponible, calculamos ΔP directamente:
    #   ΔP = P_rail_absoluta − P_manifold
    #      = (FP_gauge_PSI × 6.895 + Baro) − MAP   [todo en kPa]
    # Con regulador 1:1 funcionando, ΔP debe ser constante independiente de MAP.
    # Diagnóstico por tendencia ΔP vs MAP:
    #   slope ≈ 0         → regulador OK
    #   slope > 0 (ΔP sube a MAP bajo) → vacío desconectado: sin referencia, ΔP
    #                                     sube en ralentí → inyecta de más
    #   slope < 0 (ΔP baja a MAP bajo) → diafragma roto: combustible en línea de vacío
    # Sin sensor: inferencia indirecta por desviación AFR vs MAP.
    PSI_TO_KPA = 6.895

    fp_rows = [r for r in warm_run
               if (r.get('ae_pct') or 100) < 106
               and (r.get('tps')    or 0)   > 0.5
               and abs(r.get('mapdot') or 0) < 15]

    has_fp_sensor = any(r.get('fuel_pressure') is not None for r in fp_rows)

    MAP_BUCKETS = [(20, 35), (35, 50), (50, 65), (65, 80), (80, 95)]
    fp_buckets  = []

    if has_fp_sensor:
        for lo, hi in MAP_BUCKETS:
            mid  = (lo + hi) / 2
            bucket = [r for r in fp_rows
                      if lo <= (r.get('map') or 0) < hi
                      and r.get('fuel_pressure') is not None
                      and 10 < r['fuel_pressure'] < 120]   # rango válido PSI
            if len(bucket) < 20:
                continue
            delta_p = [r['fuel_pressure'] * PSI_TO_KPA
                       + (r.get('baro') or 94.0)
                       - r['map']
                       for r in bucket]
            fp_buckets.append({
                'lo': lo, 'hi': hi, 'mid': mid,
                'n':       len(bucket),
                'fp_avg':  _mean([r['fuel_pressure'] for r in bucket]),
                'dp_avg':  _mean(delta_p),
                'dp_std':  _stdev(delta_p),
                'dp_min':  min(delta_p),
                'dp_max':  max(delta_p),
            })
    else:
        # Sin sensor: comparar desviación AFR vs MAP como proxy
        for lo, hi in MAP_BUCKETS:
            mid  = (lo + hi) / 2
            afrs = [r['afr'] for r in fp_rows
                    if r.get('afr') and 8 < r['afr'] < 20
                    and lo <= (r.get('map') or 0) < hi]
            if len(afrs) >= 20:
                avg = _mean(afrs)
                tgt = target_afr(mid)
                fp_buckets.append({
                    'lo': lo, 'hi': hi, 'mid': mid,
                    'n': len(afrs), 'afr_avg': avg,
                    'target': tgt, 'deviation': avg - tgt,
                })

    # Regresión lineal: métrica principal vs MAP
    fp_slope = None
    if len(fp_buckets) >= 3:
        maps = [b['mid'] for b in fp_buckets]
        if has_fp_sensor:
            vals = [b['dp_avg'] for b in fp_buckets]
        else:
            vals = [b['deviation'] for b in fp_buckets]
        mx  = _mean(maps);  mv = _mean(vals)
        num = sum((maps[i] - mx) * (vals[i] - mv) for i in range(len(maps)))
        den = sum((maps[i] - mx) ** 2              for i in range(len(maps)))
        fp_slope = num / den if den > 0 else 0.0

    health['fuel_pressure'] = {
        'n_total':        len(fp_rows),
        'buckets':        fp_buckets,
        'slope':          fp_slope,
        'n_buckets':      len(fp_buckets),
        'has_fp_sensor':  has_fp_sensor,
    }

    # ── PRESIÓN DE ACEITE vs RPM ──
    # Sensor OilPressure en PSI. Solo con motor caliente (CLT>70°C).
    # La presión de aceite debe subir con las RPM; presión baja en ralentí
    # puede indicar bomba desgastada, strainer obstruido, o rodamientos flojos.
    OIL_VALID_PSI = (5, 120)
    OIL_RPM_BUCKETS = [(600, 1000), (1000, 1500), (1500, 2000),
                       (2000, 2800), (2800, 4500)]

    oil_rows = [r for r in warm_run
                if r.get('oil_pressure') is not None
                and OIL_VALID_PSI[0] < r['oil_pressure'] < OIL_VALID_PSI[1]]

    has_oil = len(oil_rows) > 10
    oil_buckets = []

    if has_oil:
        for lo, hi in OIL_RPM_BUCKETS:
            bucket = [r for r in oil_rows if lo <= (r.get('rpm') or 0) < hi]
            if len(bucket) < 10:
                continue
            vals = [r['oil_pressure'] for r in bucket]
            oil_buckets.append({
                'lo': lo, 'hi': hi,
                'mid': (lo + hi) / 2,
                'n':    len(bucket),
                'avg':  _mean(vals),
                'min':  min(vals),
                'max':  max(vals),
                'std':  _stdev(vals),
            })

    oil_slope = None
    if len(oil_buckets) >= 3:
        rpms = [b['mid'] for b in oil_buckets]
        oils = [b['avg'] for b in oil_buckets]
        mx = _mean(rpms); mv = _mean(oils)
        num = sum((rpms[i] - mx) * (oils[i] - mv) for i in range(len(rpms)))
        den = sum((rpms[i] - mx) ** 2              for i in range(len(rpms)))
        oil_slope = num / den if den > 0 else 0.0

    health['oil_pressure'] = {
        'has_sensor':  has_oil,
        'n_total':     len(oil_rows),
        'buckets':     oil_buckets,
        'slope':       oil_slope,
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

    # ── REGULADOR DE PRESIÓN 1:1 ──
    sec('REGULADOR PRESIÓN 1:1  (vacío-referenciado)')
    fp = health.get('fuel_pressure', {})
    has_sensor = fp.get('has_fp_sensor', False)

    if fp.get('n_buckets', 0) < 2:
        row(f"Datos insuficientes por zona MAP — muestras: {fp.get('n_total', 0)}")
        note("Necesita logs con variedad de carga (MAP 20–95 kPa)")
    elif has_sensor:
        # ── Con sensor de presión: mostrar ΔP por zona MAP ──
        row(f"Sensor: fuel_pressure (PSI)   Muestras: {fp['n_total']:,}")
        row(f"Principio: ΔP = FP_gauge×6.895 + Baro − MAP  →  debe ser constante")
        row(f"  {'Zona MAP':>12}  {'n':>5}  {'FP (PSI)':>9}  {'ΔP (kPa)':>9}  "
            f"{'std':>6}  {'min–max':>14}")
        row(f"  " + "─" * 62)
        for b in fp['buckets']:
            dp_range = f"{b['dp_min']:.0f}–{b['dp_max']:.0f}"
            flag = '  ⚠' if b['dp_std'] > 8 else ''
            row(f"  {b['lo']:.0f}–{b['hi']:.0f} kPa:   "
                f"{b['n']:>5}  "
                f"{b['fp_avg']:>9.1f}  "
                f"{b['dp_avg']:>9.1f}  "
                f"{b['dp_std']:>6.1f}  "
                f"{dp_range:>14}{flag}")

        slope   = fp.get('slope')
        dp_avgs = [b['dp_avg'] for b in fp['buckets']]
        dp_stds = [b['dp_std'] for b in fp['buckets']]
        overall_std = _stdev(dp_avgs)   # variación del ΔP promedio entre zonas

        if slope is not None and abs(slope) > 0.3:    # >0.3 kPa/kPa MAP = ~2% variación
            if slope > 0:
                row(f"\n  ΔP sube a MAP bajo (slope={slope:+.2f} kPa/kPa)   ⚠ REVISAR")
                note("Vacío del regulador desconectado o con fuga:")
                note("sin referencia de vacío, la presión no baja en ralentí")
                note("→ ΔP más alto de lo normal a baja carga → inyecta de más")
                note("Revisar manguera regulador ↔ intake manifold")
            else:
                row(f"\n  ΔP baja a MAP bajo (slope={slope:+.2f} kPa/kPa)   ⚠ REVISAR")
                note("Posible diafragma roto — combustible entrando a línea de vacío")
                note("O caída de presión de bomba a baja demanda (obstrucción retorno)")
        elif overall_std > 5:
            row(f"\n  ΔP variable entre zonas (std={overall_std:.1f} kPa)   ⚠ REVISAR")
            note("Alta dispersión — posible regulador con histéresis o sello parcial")
        else:
            row(f"\n  ΔP estable entre zonas (std={overall_std:.1f} kPa)   ✓")
            note("Regulador 1:1 funcionando correctamente")
    else:
        # ── Sin sensor: inferencia indirecta por AFR vs MAP ──
        row(f"Sin sensor fuel_pressure — inferencia por desviación AFR vs MAP")
        row(f"  {'Zona MAP':>12}  {'n':>5}  {'AFR':>7}  {'Target':>7}  {'Desv':>7}")
        row(f"  " + "─" * 46)
        for b in fp['buckets']:
            sign = '+' if b['deviation'] >= 0 else ''
            flag = '  ⚠' if abs(b['deviation']) > 0.8 else ''
            row(f"  {b['lo']:.0f}–{b['hi']:.0f} kPa:   "
                f"{b['n']:>5}  "
                f"{b['afr_avg']:>7.2f}  "
                f"{b['target']:>7.1f}  "
                f"{sign}{b['deviation']:>6.2f}{flag}")

        slope = fp.get('slope')
        devs  = [b['deviation'] for b in fp['buckets']]
        if slope is not None and abs(slope) > 0.025:
            if slope > 0:
                row(f"\n  Tendencia AFR: RICO en MAP bajo → normal en MAP alto   ⚠ REVISAR")
                note("Patrón compatible con línea de vacío desconectada")
            else:
                row(f"\n  Tendencia AFR: POBRE en MAP bajo → normal en MAP alto   ⚠ REVISAR")
                note("Posible diafragma roto o caída de presión a baja carga")
        elif all(d < -0.5 for d in devs):
            row(f"\n  Sesgo uniforme RICO en todas las zonas MAP   ⚠ REVISAR")
            note("Presión base alta, o VE sobredimensionada globalmente")
        elif all(d > 0.5 for d in devs):
            row(f"\n  Sesgo uniforme POBRE en todas las zonas MAP   ⚠ REVISAR")
            note("Presión base baja, bomba débil, o VE subestimada globalmente")
        else:
            row(f"\n  Sin tendencia sistemática por MAP   ✓")
            note("Comportamiento consistente con regulador 1:1 funcionando")

    # ── PRESIÓN DE ACEITE ──
    # Umbrales proporcionales al RPM (PSI mínimo esperado para 4G15):
    #   600-1000: >=10 PSI   1000-1500: >=15   1500-2000: >=20
    #   2000-2800: >=28      2800-4500: >=38
    OIL_MIN = {(600,1000):10, (1000,1500):15, (1500,2000):20,
               (2000,2800):28, (2800,4500):38}

    sec('PRESIÓN DE ACEITE vs RPM  (motor caliente, PSI)')
    op = health.get('oil_pressure', {})
    if not op.get('has_sensor'):
        row('Sin canal OilPressure en los logs.')
        note('Conectar sensor de presión de aceite para monitoreo de salud mecánica')
    elif not op.get('buckets'):
        row(f"Sensor detectado pero muestras insuficientes ({op.get('n_total',0)}) con motor caliente")
    else:
        row(f"Muestras válidas (CLT>70°C): {op['n_total']:,}")
        row(f"  {'Rango RPM':>14}  {'n':>5}  {'Prom':>7}  {'Min':>7}  {'Max':>7}  {'std':>6}  {'Mín OK?':>9}")
        row('  ' + '─' * 66)
        for b in op['buckets']:
            min_ok = OIL_MIN.get((b['lo'], b['hi']), 10)
            flag   = '  ⚠' if b['avg'] < min_ok else '  ✓'
            row(f"  {b['lo']:.0f}–{b['hi']:.0f} RPM:"
                f"  {b['n']:>5}  {b['avg']:>7.1f}  "
                f"{b['min']:>7.1f}  {b['max']:>7.1f}  {b['std']:>6.1f}"
                f"  >={min_ok} PSI{flag}")

        slope  = op.get('slope')
        idle_b = next((b for b in op['buckets'] if b['lo'] < 1000), None)
        if idle_b:
            if idle_b['avg'] < 7:
                row(f"\n  Presión en ralentí: {idle_b['avg']:.1f} PSI   ⚠ CRÍTICO — PARAR MOTOR")
            elif idle_b['avg'] < 10:
                row(f"\n  Presión en ralentí: {idle_b['avg']:.1f} PSI   ⚠ BAJA")
                note('Verificar nivel de aceite, bomba y rodamientos')
            else:
                row(f"\n  Presión en ralentí: {idle_b['avg']:.1f} PSI   ✓")

        if slope is not None:
            if slope < 0.003:
                row(f"  Presión no sube con RPM (slope={slope:+.4f} PSI/RPM)   ⚠ REVISAR")
                note('Posible strainer obstruido o bomba desgastada')
            else:
                row(f"  Presión aumenta con RPM (slope={slope:+.4f} PSI/RPM)   ✓")

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
# 7. CALIBRACIÓN AE (TAE – Throttle Accel Enrichment)
# ─────────────────────────────────────────────

AE_TARGET_AFR  = 13.5   # AFR objetivo durante aceleración — balance respuesta/potencia
AE_SENSOR_LAG  = 6      # samples de lag AEM X-Series (~300 ms @ 20 Hz)
AE_CLT_MIN     = 70.0
AE_RPM_MIN     = 700.0
AE_MIN_SAMPLES = 2      # muestras mínimas con AE activo para contar el evento

_MAP_EVENT_MIN_RISE_KPA = 8.0   # subida mínima de MAP para registrar un evento
_MAP_EVENT_WINDOW_SECS  = 5.0   # duración máxima de un evento (s)
_MAP_EVENT_PRE_FRAMES   = 20    # frames previos para calcular baseline MAP
_MAP_EVENT_AE_SEARCH    = 12    # frames extra pre-evento para buscar TAE temprano


def detect_ae_events(rows: list, ae_cfg: dict,
                     sensor_lag: int = AE_SENSOR_LAG) -> list:
    """
    Detecta eventos AE individuales en la secuencia de filas de load_msl_full.

    Para cada evento retorna:
      tpsdot_max   — pico de TPSdot durante el evento (%/s)
      rpm/map/clt  — condiciones en el onset
      afr_baseline — AFR promedio antes del onset (sin AE activo)
      afr_ae_lag   — mínimo AFR en ventana lag-ajustada (pico de enriquecimiento real)
      afr_delta    — afr_ae_lag − afr_baseline (negativo = se enriqueció, correcto)
      accel_pw_avg — PW de AE promedio durante el evento (ms)
      base_pw_avg  — PW base (total − AE) promedio durante el evento (ms)
      n_samples    — muestras con AE activo
      bin_idx      — índice del bin taeRates más cercano al tpsdot_max
    """
    tae_rates = ae_cfg.get('taeRates') or [11, 300, 629, 884]
    tae_time  = ae_cfg.get('taeTime')  or 0.4
    # Ventana de búsqueda post-lag: taeTime + margen de 5 muestras
    tae_samp  = int(tae_time * 20) + 5
    PRE_BUF   = 6

    events  = []
    in_ae   = False
    ae_st   = 0
    pre_buf = []

    def process_event(st, en, pb):
        n_ae = en - st
        if n_ae < AE_MIN_SAMPLES:
            return

        # Baseline: AFR en el mismo archivo de log, antes del onset
        ae_file  = rows[st].get('file_idx', 0)
        pre_afrs = [rows[j]['afr'] for j in pb
                    if j < st
                    and rows[j].get('file_idx', 0) == ae_file
                    and rows[j].get('afr') and 10 < rows[j]['afr'] < 20]
        if len(pre_afrs) < 2:
            return
        afr_baseline = _mean(pre_afrs)

        # Mínimo AFR en ventana lag-ajustada = pico de enriquecimiento real
        lag_s    = st + sensor_lag
        lag_e    = lag_s + n_ae + tae_samp
        lag_afrs = [rows[j]['afr'] for j in range(lag_s, min(lag_e, len(rows)))
                    if rows[j].get('afr') and 10 < rows[j]['afr'] < 20]
        if not lag_afrs:
            return
        afr_ae_lag = min(lag_afrs)

        ae_sl     = rows[st:en]
        tpsdot_mx = max((abs(r.get('tpsdot') or 0) for r in ae_sl), default=0)
        onset     = rows[st]

        accel_pws = [r.get('accel_pw') or 0.0 for r in ae_sl]
        pws       = [r.get('pw')       or 0.0 for r in ae_sl]
        base_vals = [pw - apw for pw, apw in zip(pws, accel_pws) if pw > apw > 0]

        bin_idx = min(range(len(tae_rates)),
                      key=lambda k: abs(tpsdot_mx - tae_rates[k]))

        events.append({
            'tpsdot_max':   tpsdot_mx,
            'rpm':          onset.get('rpm') or 0,
            'map':          onset.get('map') or 0,
            'clt':          onset.get('clt') or 0,
            'afr_baseline': afr_baseline,
            'afr_ae_lag':   afr_ae_lag,
            'afr_delta':    afr_ae_lag - afr_baseline,
            'accel_pw_avg': _mean(accel_pws),
            'base_pw_avg':  _mean(base_vals) if base_vals else 0.0,
            'n_samples':    n_ae,
            'bin_idx':      bin_idx,
        })

    for i, row in enumerate(rows):
        ae_active = (row.get('ae_pct') or 100.0) > 105.0
        if not ae_active:
            pre_buf = (pre_buf + [i])[-PRE_BUF:]
        if ae_active and not in_ae:
            if (row.get('clt') or 0) >= AE_CLT_MIN and (row.get('rpm') or 0) >= AE_RPM_MIN:
                in_ae = True
                ae_st = i
        elif not ae_active and in_ae:
            in_ae = False
            process_event(ae_st, i, list(pre_buf))

    # Evento que llega hasta el fin del log
    if in_ae:
        process_event(ae_st, len(rows), list(pre_buf))

    return events


def analyze_ae_calibration(events: list, ae_cfg: dict) -> dict:
    """
    Agrupa eventos por bin taeRates y calcula correcciones de taeBins.

    Fórmula (VE ya calibrado → desviación AFR es 100% responsabilidad del AE):
      desired_accel_pw = (base_pw + accel_pw) × (afr_ae / target_ae) − base_pw
      new_taeBin = current_taeBin × (desired_accel_pw / accel_pw)
    """
    tae_rates  = ae_cfg.get('taeRates') or [11, 300, 629, 884]
    tae_bins   = ae_cfg.get('taeBins')  or [0.5, 1.2, 1.8, 2.5]
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0
    MIN_EV     = 3

    per_bin = []
    for b in range(len(tae_rates)):
        evs = [e for e in events if e['bin_idx'] == b]
        if not evs:
            per_bin.append({'rate': tae_rates[b], 'current': tae_bins[b],
                            'n': 0, 'has_data': False,
                            'afr_base': None, 'afr_ae': None, 'afr_delta': None,
                            'accel_pw': None, 'base_pw': None,
                            'suggested': None, 'status': 'sin datos'})
            continue

        afr_bases  = [e['afr_baseline'] for e in evs]
        afr_aes    = [e['afr_ae_lag']   for e in evs]
        afr_deltas = [e['afr_delta']     for e in evs]
        a_pws      = [e['accel_pw_avg']  for e in evs if e['accel_pw_avg'] > 0]
        b_pws      = [e['base_pw_avg']   for e in evs if e['base_pw_avg']  > 0]

        afr_base_avg  = _mean(afr_bases)
        afr_ae_avg    = _mean(afr_aes)
        afr_delta_avg = _mean(afr_deltas)
        accel_pw_avg  = _mean(a_pws) if a_pws else None
        base_pw_avg   = _mean(b_pws) if b_pws else None

        suggested = None
        if (len(evs) >= MIN_EV
                and afr_ae_avg   is not None
                and accel_pw_avg and accel_pw_avg > 0
                and base_pw_avg  and base_pw_avg  > 0):
            desired_apw = ((base_pw_avg + accel_pw_avg) * afr_ae_avg
                           / AE_TARGET_AFR) - base_pw_avg
            if desired_apw > 0:
                scale     = desired_apw / accel_pw_avg
                suggested = max(0.1, min(round(tae_bins[b] * scale, 1), 10.0))

        diff = (afr_ae_avg - AE_TARGET_AFR) if afr_ae_avg is not None else 0
        if afr_ae_avg is None:
            status = 'sin AFR válido'
        elif len(evs) < MIN_EV:
            status = f'pocos eventos ({len(evs)}) — no confiable'
        elif diff >  1.5:
            status = '⚠ muy pobre — aumentar AE'
        elif diff < -1.5:
            status = '⚠ muy rico — reducir AE'
        elif abs(diff) > 0.5:
            status = '→ ajuste fino'
        else:
            status = '✓ OK'

        per_bin.append({
            'rate': tae_rates[b], 'current': tae_bins[b], 'n': len(evs),
            'has_data':  True,
            'afr_base':  afr_base_avg,
            'afr_ae':    afr_ae_avg,
            'afr_delta': afr_delta_avg,
            'accel_pw':  accel_pw_avg,
            'base_pw':   base_pw_avg,
            'suggested': suggested,
            'status':    status,
        })

    # Distribución de TPSdot observada — edges ordenados y deduplicados
    all_tp = [e['tpsdot_max'] for e in events]
    raw_edges = sorted(set([0.0, tps_thresh] + list(tae_rates)))
    max_tp = max(all_tp) * 1.1 if all_tp else 1000.0
    if raw_edges[-1] < max_tp:
        raw_edges.append(max_tp)
    brackets = []
    for j in range(len(raw_edges) - 1):
        lo, hi = raw_edges[j], raw_edges[j + 1]
        if hi <= lo:
            continue
        cnt = sum(1 for t in all_tp if lo <= t < hi)
        brackets.append({'lo': lo, 'hi': hi,
                         'n': cnt, 'pct': _pct(cnt, len(all_tp))})

    # Detectar intervalos con concentración alta pero bins muy separados
    rate_sugg = []
    for j in range(len(tae_rates) - 1):
        lo, hi   = tae_rates[j], tae_rates[j + 1]
        width    = hi - lo
        cnt      = sum(1 for t in all_tp if lo <= t < hi)
        pct      = _pct(cnt, len(all_tp))
        if width > 150 and pct > 35:
            mid = round((lo + hi) / 2 / 10) * 10
            rate_sugg.append({
                'lo': lo, 'hi': hi, 'pct': pct, 'suggested_mid': mid,
                'note': (f"{pct:.0f}% de eventos en rango {width:.0f} %/s "
                         f"({lo:.0f}–{hi:.0f}) sin bin intermedio"),
            })

    # Colocación óptima de 4 bins basada en la distribución real observada.
    # Objetivo: cubrir el rango real con resolución pareja.
    # Usa p10, p40, p70 y max_observed como puntos de anclaje —
    # no los cuartiles, que colapsarían si la distribución es estrecha.
    optimal_rates = None
    if len(all_tp) >= 8:
        filtered = sorted(t for t in all_tp if t >= tps_thresh)
        if len(filtered) >= 4:
            n    = len(filtered)
            p10  = filtered[max(0, n * 1 // 10)]
            p40  = filtered[n * 4 // 10]
            p70  = filtered[n * 7 // 10]
            pmax = filtered[-1]
            cand = [
                max(int(tps_thresh) + 5, round(p10  / 10) * 10),
                round(p40  / 10) * 10,
                round(p70  / 10) * 10,
                round(pmax / 10) * 10,
            ]
            # Garantizar separación mínima de 30 %/s entre bins consecutivos
            for k in range(1, len(cand)):
                if cand[k] < cand[k - 1] + 30:
                    cand[k] = cand[k - 1] + 30
            optimal_rates = cand

    return {
        'n_total':       len(events),
        'per_bin':       per_bin,
        'brackets':      brackets,
        'rate_sugg':     rate_sugg,
        'optimal_rates': optimal_rates,
        'tae_rates':     tae_rates,
        'tae_bins':      tae_bins,
        'tps_thresh':    tps_thresh,
        '_raw_events':   events,   # para diagnóstico de parámetros
    }


def _ae_table_str(added_ms: list, tpsdot: list, label_added: str = 'Added (ms)',
                  label_tps: str = 'TPSdot (%/s)', notes: list = None) -> str:
    """
    Genera string de tabla TAE en formato visual TunerStudio.
    notes: lista opcional de strings por columna (se imprime debajo).
    """
    n  = len(added_ms)
    # Ancho de cada columna: máximo entre header y valor
    col_w = []
    for i in range(n):
        a_s = f"{added_ms[i]:.1f}" if added_ms[i] is not None else " ?"
        t_s = f"{tpsdot[i]:.0f}"   if tpsdot[i]   is not None else " ?"
        col_w.append(max(len(a_s), len(t_s), 5))

    def row_str(values, fmt_fn):
        cells = ' │ '.join(fmt_fn(v, col_w[i]) for i, v in enumerate(values))
        return '  │ ' + cells + ' │'

    sep = '  ├─' + '─┼─'.join('─' * w for w in col_w) + '─┤'
    top = '  ┌─' + '─┬─'.join('─' * w for w in col_w) + '─┐'
    bot = '  └─' + '─┴─'.join('─' * w for w in col_w) + '─┘'

    def fmt_added(v, w):
        s = f"{v:.1f}" if v is not None else '?'
        return s.rjust(w)

    def fmt_tps(v, w):
        s = f"{v:.0f}" if v is not None else '?'
        return s.rjust(w)

    lines = [top,
             row_str(added_ms, fmt_added),
             sep,
             row_str(tpsdot,   fmt_tps),
             bot]

    # Leyenda de filas
    lbl_w = max(len(label_added), len(label_tps))
    lines[1] = f"  {label_added:<{lbl_w}} " + lines[1][2:]
    lines[3] = f"  {label_tps:<{lbl_w}} " + lines[3][2:]
    lines[0] = ' ' * (lbl_w + 2) + lines[0][2:]
    lines[2] = ' ' * (lbl_w + 2) + lines[2][2:]
    lines[4] = ' ' * (lbl_w + 2) + lines[4][2:]

    if notes:
        note_row = ' ' * (lbl_w + 5)
        note_row += '   '.join(
            (n[:col_w[i]]).ljust(col_w[i]) for i, n in enumerate(notes)
        )
        lines.append(note_row)

    return '\n'.join(lines)


def print_ae_calibration(result: dict, ae_cfg: dict):
    """Imprime reporte de calibración AE con tablas en formato TunerStudio."""
    tae_rates  = result['tae_rates']
    tae_bins   = result['tae_bins']
    tps_thresh = result['tps_thresh']

    print('\n' + '=' * 60)
    print('  CALIBRACIÓN AE — TAE (Throttle Acceleration Enrichment)')
    print('=' * 60)
    print(f"  tpsThresh  : {ae_cfg.get('tpsThresh')} %/s  "
          f"(TPSdot mínimo para disparar AE)")
    print(f"  taeTime    : {ae_cfg.get('taeTime')} s   "
          f"aeTaperTime: {ae_cfg.get('aeTaperTime')} s")
    cold_a = ae_cfg.get('taeColdA')
    cold_m = ae_cfg.get('taeColdM')
    if cold_a is not None and cold_m is not None:
        print(f"  taeColdA/M : +{cold_a}ms / ×{cold_m:.0f}%  "
              f"(solo motor frío — excluido del análisis)")
    print(f"  AFR target : {AE_TARGET_AFR}   "
          f"Lag sensor: {AE_SENSOR_LAG} muestras (~{AE_SENSOR_LAG * 50}ms AEM X-Series)")
    print(f"  Eventos    : {result['n_total']} aceleraciones válidas "
          f"(CLT≥{AE_CLT_MIN:.0f}°C, RPM≥{AE_RPM_MIN:.0f})")
    print()

    if result['n_total'] == 0:
        print("  Sin eventos AE válidos. Incluye logs con aceleraciones "
              "a temperatura de operación (CLT>70°C).")
        return

    # ── Tabla ACTUAL (idéntica a TunerStudio) ──
    print("── TABLA TAE ACTUAL ─────────────────── (como en TunerStudio)")
    print(_ae_table_str(tae_bins, tae_rates))
    print()

    # ── Tabla SUGERIDA (solo taeBins, mismos taeRates) ──
    sug_bins  = []
    bin_notes = []
    any_change = False
    for b in result['per_bin']:
        if b['suggested'] is not None:
            sug_bins.append(b['suggested'])
            diff = b['suggested'] - b['current']
            bin_notes.append(f"{diff:+.1f}" if abs(diff) >= 0.1 else '=')
            if abs(diff) >= 0.1:
                any_change = True
        else:
            sug_bins.append(b['current'])
            ev_tag = f"n={b['n']}" if b['n'] > 0 else 'sin datos'
            bin_notes.append(ev_tag)

    print("── TABLA TAE SUGERIDA ───────────────── (solo cambiar Added)")
    print(_ae_table_str(sug_bins, tae_rates, notes=bin_notes))
    print()

    # ── Diagnóstico por bin ──
    print("── QUÉ ENCONTRÓ EL ANÁLISIS ────────────────────────────────")
    for bidx, b in enumerate(result['per_bin']):
        rate = b['rate']
        if not b['has_data']:
            print(f"  Bin {bidx+1} ({rate:.0f} %/s): sin aceleraciones en logs — no hay datos")
        else:
            afr_b = f"{b['afr_base']:.2f}" if b['afr_base'] else "?"
            afr_a = f"{b['afr_ae']:.2f}"   if b['afr_ae']   else "?"
            n     = b['n']
            print(f"  Bin {bidx+1} ({rate:.0f} %/s):  {n} eventos  "
                  f"AFR antes = {afr_b}  →  AFR con AE = {afr_a}  "
                  f"(obj {AE_TARGET_AFR})   {b['status']}")
    print()

    # ── Distribución: cuántos eventos cayeron en cada zona ──
    print("── DÓNDE ESTÁN TUS ACELERACIONES (TPSdot) ──────────────────")
    max_n = max(bk['n'] for bk in result['brackets']) or 1
    for bk in result['brackets']:
        bar   = '█' * round(bk['pct'] / 3) if bk['n'] > 0 else ''
        lo_s  = f"{bk['lo']:.0f}"
        hi_s  = f"{bk['hi']:.0f}" if bk['hi'] < 5000 else '→'
        label = f"{lo_s:>5}–{hi_s:<5} %/s"
        is_bin = any(abs(bk['hi'] - r) < 3 for r in tae_rates)
        is_thr = abs(bk['hi'] - tps_thresh) < 3
        tag   = f"  ← bin {next((str(k+1) for k,r in enumerate(tae_rates) if abs(r-bk['hi'])<3),'')}" if is_bin else ''
        tag   = '  ← tpsThresh' if is_thr else tag
        print(f"  {label} : {bk['n']:>4} ev  {bar}{tag}")
    print()

    # ── Reorganización taeRates si hace falta ──
    if result['rate_sugg'] and result['optimal_rates']:
        opt  = result['optimal_rates']
        # Para los nuevos rates, interpolar taeBins sugeridos desde los actuales
        # (usamos los sug_bins ya calculados como referencia del bin 0,
        #  y los bins 1-3 quedan como actuales hasta tener datos)
        opt_bins = list(sug_bins)   # copia; sug_bins ya tiene correcciones donde hay datos
        print("── SUGERENCIA: REDISTRIBUIR taeRates ───────────────────────")
        print("  El 98%+ de tus aceleraciones no pasan del primer bin.")
        print("  Con los taeRates actuales no tienes resolución real.")
        print()
        print("  Opción A — solo corregir Added (sin mover rates):")
        print("  " + "─" * 48)
        print(_ae_table_str(sug_bins, tae_rates, notes=bin_notes))
        print()
        print("  Opción B — redistribuir taeRates a tu rango real de manejo:")
        print("  " + "─" * 48)
        opt_notes = ['?' if v == b['current'] else f"{v-b['current']:+.1f}"
                     for v, b in zip(opt_bins, result['per_bin'])]
        print(_ae_table_str(opt_bins, opt,
                            notes=['ajustar' if i > 0 else
                                   (f"{opt_bins[0]:+.1f}" if opt_bins[0] != tae_bins[0] else '=')
                                   for i in range(len(opt))]))
        print()
        print("  Con Opción B necesitas tomar logs con aceleraciones más fuertes")
        print("  para calibrar los bins 2-4 (actualmente sin datos).")
        print()
    elif any_change:
        print("── ACCIÓN RECOMENDADA ──────────────────────────────────────")
        print("  Importar en TunerStudio:")
        print("  Fuel → Acceleration Enrichment → TAE curve → fila Added (ms)")
        print()

    # ── Validación de parámetros adicionales de AE ────────────────
    _print_ae_param_validation(result, ae_cfg)


def _print_ae_param_validation(result: dict, ae_cfg: dict) -> None:
    """Valida los parámetros de AE del MSQ contra los datos observados."""
    events = result.get('_raw_events', [])
    tae_time     = ae_cfg.get('taeTime')     or 0.4
    taper_time   = ae_cfg.get('aeTaperTime') or 0.3
    ae_end_pw    = ae_cfg.get('aeEndPW')     or 0.0
    tps_thresh   = ae_cfg.get('tpsThresh')   or 20.0
    tps_prop     = ae_cfg.get('tpsProportion') or 100.0
    map_thresh   = ae_cfg.get('mapThresh')   or 100.0
    mae_rates    = ae_cfg.get('maeRates')    or []
    mae_bins     = ae_cfg.get('maeBins')     or []

    print("── DIAGNÓSTICO DE PARÁMETROS AE ───────────────────────────")
    print(f"  {'Parámetro':<22}  {'Valor':>8}  Estado")
    print("  " + "─" * 58)

    def row(name, val, ok, note):
        flag = "✓" if ok else "⚠"
        print(f"  {name:<22}  {str(val):>8}  {flag}  {note}")

    # tpsThresh
    row("tpsThresh", f"{tps_thresh:.0f} %/s",
        tps_thresh <= 20,
        "OK" if tps_thresh <= 15 else
        "Considerar bajar a 8-12 %/s si hay near-stalls frecuentes")

    # taeTime
    if events:
        avg_dur = _mean([e.get('n_samples', 8) / 20.0 for e in events])
        tae_ok = abs(tae_time - avg_dur) < 0.15
    else:
        avg_dur = None
        tae_ok = True
    dur_str = f"~{avg_dur:.2f}s obs." if avg_dur else "sin datos"
    row("taeTime", f"{tae_time} s",
        tae_ok,
        f"OK ({dur_str})" if tae_ok else
        f"Duración AE obs. {dur_str} → ajustar a {avg_dur:.1f}s" if avg_dur else "OK")

    # aeTaperTime
    row("aeTaperTime", f"{taper_time} s",
        taper_time >= 0.2,
        "OK" if taper_time >= 0.2 else
        "Demasiado corto — lean al final del AE; subir a 0.3-0.5s")

    # aeEndPW
    row("aeEndPW", f"{ae_end_pw} ms",
        ae_end_pw == 0.0 or ae_end_pw < 0.2,
        "OK" if ae_end_pw < 0.2 else
        f"Impide PW de AE < {ae_end_pw}ms — puede cortar enriquecimiento pequeño")

    # tpsProportion / mapThresh
    row("tpsProportion", f"{tps_prop:.0f}%",
        tps_prop < 100,
        "OK" if tps_prop < 100 else
        "100% = solo TAE; MAE ignorado — considerar blend 70-80%")

    row("mapThresh", f"{map_thresh:.0f} kPa/s",
        map_thresh <= (mae_rates[0] if mae_rates else 30),
        "OK" if map_thresh <= (mae_rates[0] if mae_rates else 30) else
        f"Por encima del primer maeRates bin ({mae_rates[0] if mae_rates else '?'} kPa/s)"
        f" → MAE nunca activa")

    # maeBins no-zero
    if mae_bins:
        all_zero = all((v or 0) == 0 for v in mae_bins)
        row("maeBins", str(mae_bins),
            not all_zero,
            "OK — MAE tiene valores configurados" if not all_zero else
            "Todos en 0 — MAE no enrichece nada")

    print()


_STALL_RPM_DROP      = 500    # RPM mínima previa para considerar un apagón real
_STALL_RPM_STALL     = 200    # RPM a la que se considera apagón completo
_NEAR_STALL_RPM      = 1000   # RPM umbral para near-stall
_NEAR_STALL_MAP_RISE = 8.0    # kPa: subida mínima en near-stall para ser significativo
_NEAR_STALL_AFR_LEAN = 1.5    # AFR sobre target para clasificar near-stall como lean
_STALL_PRE_SECS      = 8.0    # ventana de análisis antes del apagón


def detect_stall_events(rows: list, ae_cfg: dict) -> list:
    """
    Detecta apagones completos (RPM → 0) y near-stalls (RPM < 950 con MAP
    subiendo sin AE y mezcla lean).  Solo analiza ventanas cortas para evitar
    contaminación con eventos anteriores.
    """
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0

    events: list = []
    by_file: dict = {}
    for r in rows:
        s = r.get('secl')
        if s is None or s != s:
            continue
        by_file.setdefault(r.get('file_idx', 0), []).append(r)

    for fi, file_rows in by_file.items():
        n = len(file_rows)
        if n < 20:
            continue

        # Detect sample rate
        secls = [r.get('secl') or 0 for r in file_rows[:50] if r.get('rpm')]
        hz = 12.0
        if len(secls) >= 10:
            span = secls[-1] - secls[0]
            if span > 0:
                hz = (len(secls) - 1) / span

        win2  = max(3, int(hz * 2.0))   # 2-second window in frames
        win6  = max(8, int(hz * 6.0))   # 6-second context window

        i = win2
        while i < n - win2:
            r    = file_rows[i]
            rpm  = r.get('rpm') or 0
            secl = r.get('secl') or 0

            # ── Candidatos ──────────────────────────────────────
            # Apagón: RPM hit 0 from running
            pre_rpms = [file_rows[j].get('rpm') or 0 for j in range(max(0,i-win2), i)]
            rpm_pre  = max(pre_rpms) if pre_rpms else 0

            is_stall = rpm_pre > _STALL_RPM_DROP and rpm < _STALL_RPM_STALL

            # Near-stall: use a tight 2s window centered around the RPM dip
            is_near_stall = False
            if (not is_stall
                    and rpm_pre > _STALL_RPM_DROP
                    and rpm < _NEAR_STALL_RPM       # < 1000
                    and rpm > _STALL_RPM_STALL       # > 200
                    and (r.get('tps') or 0) < 20):  # not WOT
                # 2s window: only running frames, no atmospheric MAP
                win = [file_rows[j] for j in range(max(0,i-win2), i+win2)
                       if (file_rows[j].get('rpm') or 0) > 200
                       and (file_rows[j].get('map') or 0) < 80]
                if len(win) >= 4:
                    # RPM must reach < 950 in this window (not just a transient frame)
                    rpm_min_win = min(x.get('rpm') or 9999 for x in win)
                    # MAP rose >= 8 kPa
                    maps_win = [x.get('map') or 0 for x in win]
                    map_rise = max(maps_win) - min(maps_win)
                    # No AE in window
                    ae_win = any((x.get('ae_pct') or 100.0) > 105.0 for x in win)
                    # AFR went lean vs target of peak MAP
                    afrs_win = [x.get('afr') for x in win
                                if x.get('afr') and 10 < x['afr'] < 20]
                    afr_pk = max(afrs_win) if afrs_win else 0
                    afr_lean = (afr_pk - target_afr(max(maps_win))
                                if maps_win else 0)

                    if (rpm_min_win < _NEAR_STALL_RPM
                            and map_rise >= _NEAR_STALL_MAP_RISE
                            and not ae_win
                            and afr_lean >= _NEAR_STALL_AFR_LEAN):
                        is_near_stall = True

            if not (is_stall or is_near_stall):
                i += 1
                continue

            # Dedup
            if events and abs(secl - events[-1]['secl_stall']) < 5:
                i += 1
                continue

            # ── Ventana pre-stall (6s, solo frames válidos) ──────
            pre = [file_rows[j] for j in range(max(0, i-win6), i)
                   if (file_rows[j].get('rpm') or 0) > 200
                   and (file_rows[j].get('map') or 0) < 80]
            if not pre:
                i += 1
                continue

            maps     = [x.get('map') or 0 for x in pre]
            map_min  = min(maps); map_max = max(maps)
            map_at_s = file_rows[max(0,i-1)].get('map') or 0
            rpm_trend = _mean([x.get('rpm') or 0 for x in pre[-10:]])
            first_rpm = (pre[0].get('rpm') or 0)
            in_decel  = first_rpm > rpm_trend + 200

            ae_fired  = any((x.get('ae_pct') or 100.0) > 105.0 for x in pre)
            accel_max = max((x.get('accel_pw') or 0) for x in pre)
            tpsdot_mx = max((abs(x.get('tpsdot') or 0) for x in pre), default=0)
            mapdot_mx = max((abs(x.get('mapdot') or 0) for x in pre), default=0)

            # Actual minimum RPM during the event
            rpm_min_event = min(
                (file_rows[j].get('rpm') or 9999 for j in range(max(0,i-win2), i+win2)),
                default=rpm)

            parts_diag = []
            if in_decel:
                parts_diag.append(f"RPM en decel ({first_rpm:.0f}→{int(rpm_trend)})")
            if map_max - map_min > 3:
                parts_diag.append(f"MAP subió {map_max-map_min:.1f} kPa")
            if not ae_fired:
                if tpsdot_mx >= tps_thresh:
                    parts_diag.append(
                        f"AE no activó (TPSdot={tpsdot_mx:.0f} ≥ {tps_thresh:.0f} %/s — filtro ECU)")
                elif tpsdot_mx > 0:
                    parts_diag.append(
                        f"AE no activó (TPSdot={tpsdot_mx:.0f} < {tps_thresh:.0f} %/s)")
                else:
                    parts_diag.append("TPS cerrado — apagón sin transición de carga")
            else:
                parts_diag.append(f"AE activó ({accel_max:.2f} ms) pero insuficiente")

            events.append({
                'secl_stall':   round(secl,          1),
                'file_idx':     fi,
                'event_type':   'stall' if is_stall else 'near-stall',
                'rpm_pre':      round(rpm_pre,        0),
                'rpm_min':      round(rpm_min_event,  0),
                'rpm_trend':    round(rpm_trend,      0),
                'in_decel':     in_decel,
                'map_pre':      round(map_at_s,       1),
                'map_rise':     round(map_max - map_min, 1),
                'map_peak':     round(map_max,        1),
                'tpsdot_max':   round(tpsdot_mx,      1),
                'mapdot_max':   round(mapdot_mx,      1),
                'ae_fired':     ae_fired,
                'accel_pw_max': round(accel_max,      3),
                'diagnosis':    '; '.join(parts_diag) if parts_diag else 'Ver datos',
                'pre_rows':     pre[-12:],
            })

            i += win2

    return events


def print_stall_events(events: list, ae_cfg: dict) -> None:
    """
    Imprime análisis de apagones con condiciones AE pre-stall.
    """
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0

    print('\n' + '=' * 60)
    print('  APAGONES — Condiciones AE Pre-Stall')
    print('=' * 60)

    if not events:
        print("  No se detectaron apagones ni near-stalls en el log.")
        return

    n_stall = sum(1 for e in events if e['event_type'] == 'stall')
    n_near  = sum(1 for e in events if e['event_type'] == 'near-stall')
    ae_gap  = sum(1 for e in events if not e['ae_fired'])
    ae_weak = sum(1 for e in events if     e['ae_fired'])

    print(f"  Apagones completos   : {n_stall}  (RPM → 0)")
    print(f"  Near-stalls          : {n_near}   (RPM < {_NEAR_STALL_RPM}, MAP subió sin AE, mezcla lean)")
    ae_gap  = ae_gap   # redeclare to avoid duplicate
    if ae_gap:
        print(f"  Sin AE en ventana pre-stall : {ae_gap}")
    if ae_weak:
        print(f"  Con AE pero insuficiente    : {ae_weak}")

    for e in events:
        tipo = "APAGÓN" if e['event_type'] == 'stall' else "NEAR-STALL"
        print(f"\n  {'─'*56}")
        rpm_min_disp = e.get('rpm_min', e['rpm_pre'])
        print(f"  {tipo} @ SecL {e['secl_stall']:.1f}   "
              f"RPM mínima: {rpm_min_disp:.0f}  (RPM previa: {e['rpm_pre']:.0f})")
        print(f"  {'─'*56}")
        if e['in_decel']:
            print(f"  Motor venía en decel — RPM promedio ventana: {e['rpm_trend']:.0f}")

        print(f"\n  Ventana pre-stall ({_STALL_PRE_SECS:.0f}s):")
        print(f"    MAP: {e['map_pre']:.1f} kPa  "
              f"(subida de +{e['map_rise']:.1f} kPa en la ventana)")
        print(f"    TPSdot máx: {e['tpsdot_max']:.0f} %/s   "
              f"tpsThresh: {tps_thresh:.0f} %/s")
        print(f"    MAPdot máx: {e['mapdot_max']:.0f} kPa/s")

        ae_str = (f"NO activó  (ae_pct=100% todo el tiempo)"
                  if not e['ae_fired']
                  else f"Activó — accel_pw_max={e['accel_pw_max']:.2f} ms")
        print(f"    AE:         {ae_str}")

        # Tabla de frames pre-stall
        pre = e['pre_rows']
        if pre:
            print(f"\n  Últimos frames antes del apagón:")
            print(f"    {'SecL':>6} {'RPM':>5} {'MAP':>5} {'TPS':>5} "
                  f"{'AFR':>6} {'AE_PW':>6} {'ae%':>5} {'MAPdt':>6}")
            print("    " + "─" * 52)
            prev_s = -99
            for r in pre:
                s = r.get('secl') or 0
                if s == prev_s: continue
                print(f"    {s:6.1f} {r.get('rpm') or 0:5.0f}"
                      f" {r.get('map') or 0:5.1f} {r.get('tps') or 0:5.1f}"
                      f" {r.get('afr') or 0:6.2f} {r.get('accel_pw') or 0:6.3f}"
                      f" {r.get('ae_pct') or 100:5.0f}"
                      f" {r.get('mapdot') or 0:6.0f}")
                prev_s = s

        print(f"\n  Diagnóstico: {e['diagnosis']}")

    # Recomendaciones
    if ae_gap:
        print(f"\n  {'─'*56}")
        print(f"  ACCIÓN: Los apagones ocurrieron sin AE activo.")
        print(f"  Son la consecuencia directa de los eventos MAP sin cobertura")
        print(f"  reportados arriba. Mismo fix aplica:")
        print(f"    · Bajar tpsThresh ({tps_thresh:.0f} → {max(5,int(tps_thresh*0.65)):.0f} %/s)")
        print(f"    · Activar MAE blend para cubrir transiciones de MAP")


def detect_map_transient_events(rows: list, ae_cfg: dict) -> list:
    """
    Detecta subidas de MAP donde el AE no cubrió el transitorio.

    La búsqueda de accel_pw incluye _MAP_EVENT_AE_SEARCH frames ANTES del
    inicio del evento, porque TAE dispara al inicio del movimiento de TPS
    (cuando MAP aún no ha subido _MAP_EVENT_MIN_RISE_KPA).

    Solo retorna eventos donde accel_pw_max < 0.1 ms en toda la ventana
    ampliada.  Los eventos con TAE activo ya son analizados por
    detect_ae_events / analyze_ae_calibration.
    """
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0
    PRE  = _MAP_EVENT_PRE_FRAMES
    SRCH = _MAP_EVENT_AE_SEARCH

    events: list = []
    by_file: dict = {}
    for r in rows:
        s = r.get('secl')
        if s is None or s != s:
            continue
        by_file.setdefault(r.get('file_idx', 0), []).append(r)

    for fi, file_rows in by_file.items():
        valid = [r for r in file_rows
                 if (r.get('rpm') or 0) > 400 and (r.get('clt') or 0) >= AE_CLT_MIN]
        n = len(valid)
        if n < 10:
            continue

        i = 0
        while i < n:
            row     = valid[i]
            secl    = row.get('secl') or 0
            cur_map = row.get('map')  or 0

            pre = valid[max(0, i - PRE): i]
            if len(pre) < 3:
                i += 1
                continue
            base_map = _mean([r.get('map') or 0 for r in pre])

            if cur_map - base_map < _MAP_EVENT_MIN_RISE_KPA:
                i += 1
                continue

            # ── Frames del evento ─────────────────────────────────
            event_start = i
            secl_start  = secl
            peak_map    = cur_map

            j = i
            while j < n:
                r_j = valid[j]
                m   = r_j.get('map') or 0
                s_j = r_j.get('secl') or 0
                if m > peak_map:
                    peak_map = m
                if m <= base_map + _MAP_EVENT_MIN_RISE_KPA / 2:
                    break
                if s_j - secl_start > _MAP_EVENT_WINDOW_SECS:
                    break
                j += 1

            event_rows = valid[event_start: j + 1]
            if len(event_rows) < 3:
                i = j + 1
                continue

            # ── AE en ventana AMPLIADA ────────────────────────────
            # Misma detección que detect_ae_events: ae_pct > 105
            broad_rows   = valid[max(0, event_start - SRCH): j + 1]
            ae_active    = any((r.get('ae_pct') or 100.0) > 105.0 for r in broad_rows)
            accel_pw_max = max((r.get('accel_pw') or 0) for r in broad_rows)

            # Saltar si TAE ya cubrió el evento — lo analiza el TAE calibration
            if ae_active:
                i = j + 1
                continue

            # ── Métricas ──────────────────────────────────────────
            tpsdot_max = max((abs(r.get('tpsdot') or 0) for r in broad_rows), default=0)
            mapdot_max = max((abs(r.get('mapdot') or 0) for r in event_rows), default=0)
            tps_avg    = _mean([r.get('tps') or 0 for r in event_rows])
            secl_end   = valid[min(j, n - 1)].get('secl') or secl_start
            pw_vals    = [r.get('pw') for r in event_rows if r.get('pw') and r['pw'] > 0]
            pw_avg     = _mean(pw_vals) if pw_vals else None

            # AFR lean vs target del pico MAP (no vs baseline)
            ev_afrs = [r.get('afr') for r in event_rows
                       if r.get('afr') and 10 < r['afr'] < 20]
            afr_peak_lean   = max(ev_afrs) if ev_afrs else None
            afr_tgt_at_peak = target_afr(peak_map)
            lean_vs_target  = ((afr_peak_lean - afr_tgt_at_peak)
                                if afr_peak_lean else 0.0)

            # Diagnóstico
            if tpsdot_max < tps_thresh:
                diag = (f"TPSdot {tpsdot_max:.0f} < {tps_thresh:.0f} \u2014 "
                        + (f"lean +{lean_vs_target:.1f} AFR \u2192 MAE candidato"
                           if lean_vs_target > 0.5
                           else "mezcla aceptable"))
            else:
                diag = (f"TPSdot {tpsdot_max:.0f} \u2265 {tps_thresh:.0f} %/s "
                        f"\u2014 ECU no activó AE (posible filtro/delay interno)")

            events.append({
                'secl_start':      round(secl_start,       1),
                'secl_end':        round(secl_end,          1),
                'file_idx':        fi,
                'map_base':        round(base_map,          1),
                'map_peak':        round(peak_map,          1),
                'map_rise':        round(peak_map - base_map, 1),
                'tps_avg':         round(tps_avg,           1),
                'tpsdot_max':      round(tpsdot_max,        1),
                'mapdot_max':      round(mapdot_max,        1),
                'accel_pw_max':    round(accel_pw_max,      3),
                'afr_peak_lean':   round(afr_peak_lean,     2) if afr_peak_lean else None,
                'afr_tgt_at_peak': round(afr_tgt_at_peak,  1),
                'lean_vs_target':  round(lean_vs_target,   2),
                'pw_avg':          round(pw_avg, 2) if pw_avg else None,
                'diagnosis':       diag,
            })

            i = j + 1

    return events


def print_map_transient_events(events: list, ae_cfg: dict,
                               tae_event_count: int = 0) -> None:
    """
    Reporta transitorios MAP sin cobertura TAE y evalúa si activar MAE
    o ajustar tpsThresh.
    """
    tps_thresh = ae_cfg.get('tpsThresh') or 20.0
    mae_rates  = ae_cfg.get('maeRates')
    mae_bins   = ae_cfg.get('maeBins')

    print('\n' + '=' * 60)
    print('  COBERTURA AE EN TRANSITORIOS MAP')
    print('=' * 60)

    if not events:
        print("  TAE cubrió todos los transitorios MAP detectados.")
        print("  No se encontraron subidas de MAP sin enriquecimiento.")
        return

    lean_events = [e for e in events if e['lean_vs_target'] > 0.5]
    mae_cand    = [e for e in events if e['tpsdot_max'] < tps_thresh]

    print(f"  Transitorios sin cobertura TAE     : {len(events)}")
    print(f"  Con mezcla lean (vs target MAP)    : {len(lean_events)}")
    print(f"  TPSdot < tpsThresh (candidatos MAE): {len(mae_cand)}")
    print()

    if lean_events:
        print(f"  {'SecL':>6}  {'ΔMAP':>5}  {'Pico':>5}  {'TPSdt':>6}  {'MAPdt':>6}  {'Lean/obj':>8}  Diagnóstico")
        print("  " + "─" * 74)
        shown = sorted(lean_events, key=lambda x: -x['lean_vs_target'])[:20]
        for e in shown:
            ls = f"+{e['lean_vs_target']:.1f}" if e['lean_vs_target'] > 0 else "OK"
            print(f"  {e['secl_start']:6.1f}  {e['map_rise']:5.1f}  {e['map_peak']:5.1f}"
                  f"  {e['tpsdot_max']:6.0f}  {e['mapdot_max']:6.0f}  {ls:>8}"
                  f"  {e['diagnosis']}")
        if len(lean_events) > 20:
            print(f"  ... (+{len(lean_events)-20} eventos más)")
        print()

    # Siempre mostrar distribución MAPdot (útil incluso sin candidatos MAE puros)
    if not mae_cand:
        # Usar todos los eventos para la distribución
        mae_cand = events

    # ── Nota sobre "TPSdot ≥ tpsThresh pero sin AE" ──────────────
    no_mae_pure = [e for e in events if e['tpsdot_max'] >= tps_thresh]
    if no_mae_pure and not any(e['tpsdot_max'] < tps_thresh for e in events):
        avg_tps_gap = _mean([e['tpsdot_max'] for e in no_mae_pure])
        print(f"  Todos los eventos sin cobertura tienen TPSdot ≥ {tps_thresh:.0f} %/s.")
        print(f"  TPSdot promedio: {avg_tps_gap:.0f} %/s — el ECU debería activar TAE.")
        print(f"  Posibles causas:")
        print(f"    · Filtro/debounce interno del ECU más agresivo que el log")
        print(f"    · Bajar tpsThresh ({tps_thresh:.0f} → {max(5, int(tps_thresh*0.65)):.0f} %/s) da más margen")
        print(f"    · MAE como respaldo MAPdot también cubre estos casos")
        print()

    # ── Distribución MAPdot para eventos candidatos MAE ──
    print("── DISTRIBUCIÓN MAPdot EN EVENTOS SIN TAE ─────────────────")
    mae_rate_vals = list(mae_rates or [15, 30, 60, 86])
    bin_edges = [0] + mae_rate_vals
    counts    = [0] * len(mae_rate_vals)
    for e in mae_cand:
        md = e['mapdot_max']
        for k in range(len(mae_rate_vals) - 1, -1, -1):
            if md >= bin_edges[k]:
                counts[k] += 1
                break
        else:
            counts[0] += 1
    for k, rate in enumerate(mae_rate_vals):
        bar = '█' * min(counts[k], 25)
        lo  = int(bin_edges[k])
        print(f"  MAPdot {lo:>3}–{int(rate):>3} kPa/s : {counts[k]:3d}  {bar}")
    print()

    avg_tpsdot = _mean([e['tpsdot_max']    for e in mae_cand])
    avg_mapdot = _mean([e['mapdot_max']    for e in mae_cand])
    avg_lean   = _mean([e['lean_vs_target'] for e in lean_events]) if lean_events else 0

    # ── Cálculo del blend sugerido ───────────────────────────────
    cur_tps_prop  = ae_cfg.get('tpsProportion') or 100.0
    cur_map_thresh = ae_cfg.get('mapThresh')    or 100.0
    mae_rate_vals = list(mae_rates or [15, 30, 60, 86])

    total_events = len(events) + tae_event_count
    uncov_frac   = len(events) / max(total_events, 1)

    # tpsProportion sugerido: MAE mínimo 20% para que los bins sean razonables.
    # Bajo 20% los maeBins necesitan ser tan altos (>3.5ms) que se vuelven impráctcos.
    raw_mae_weight = uncov_frac * 100
    sug_mae_weight = min(50, max(20, int((raw_mae_weight + 9) / 10) * 10))  # ceil a 10%, min 20
    sug_tps_prop   = 100 - sug_mae_weight

    # mapThresh sugerido: percentil 20 de los MAPdot de eventos sin cobertura
    # mínimo = primer bin maeRates, máximo = mapThresh actual
    mapdots = sorted([e['mapdot_max'] for e in mae_cand if e['mapdot_max'] > 0])
    if mapdots:
        p20 = mapdots[max(0, int(len(mapdots) * 0.20))]
        sug_map_thresh = max(5, int(p20 / 5) * 5)   # redondear al múltiplo de 5
        sug_map_thresh = min(sug_map_thresh, int(mae_rate_vals[0]))
    else:
        sug_map_thresh = int(mae_rate_vals[0]) if mae_rate_vals else 15

    print("── CONFIGURACIÓN ACTUAL vs SUGERIDA ────────────────────────")
    print(f"  {'Parámetro':<22}  {'Actual':>8}  {'Sugerido':>9}  Ubicación en TunerStudio")
    print("  " + "─" * 68)
    tps_flag  = "✓" if cur_tps_prop == sug_tps_prop else "⚠"
    map_flag  = "✓" if cur_map_thresh <= sug_map_thresh + 5 else "⚠"
    print(f"  {'tpsProportion (TPS%)':<22}  {cur_tps_prop:>7.0f}%  {sug_tps_prop:>8}%"
          f"  {tps_flag}  Fuel→Accel Enrichment→TPS%")
    print(f"  {'mapThresh':<22}  {cur_map_thresh:>6.0f} kPa/s  {sug_map_thresh:>6} kPa/s"
          f"  {map_flag}  Fuel→Accel Enrichment→MAP Threshold")
    print()

    # ── Barra visual del blend ────────────────────────────────────
    BAR = 30
    def _bar(tps_pct):
        tae_w = round(BAR * tps_pct / 100)
        mae_w = BAR - tae_w
        return f"TAE [{'█'*tae_w}{'░'*mae_w}] {tps_pct:.0f}%  /  MAE [{'░'*tae_w}{'█'*mae_w}] {100-tps_pct:.0f}%"

    print(f"  Blend actual   : {_bar(cur_tps_prop)}")
    print(f"  Blend sugerido : {_bar(sug_tps_prop)}")
    print()

    # ── Explicación ───────────────────────────────────────────────
    if cur_tps_prop > sug_tps_prop or cur_map_thresh > sug_map_thresh + 5:
        print(f"  Motivo del ajuste:")
        if len(events) > 0:
            print(f"    · {len(events)} transitorios MAP ({uncov_frac*100:.0f}% del total) no cubiertos por TAE")
            print(f"    · Lean promedio en esos eventos: +{avg_lean:.1f} AFR vs target")
        if cur_map_thresh > sug_map_thresh + 5:
            print(f"    · mapThresh actual ({cur_map_thresh:.0f}) supera el MAPdot de los eventos")
            print(f"      problemáticos ({avg_mapdot:.0f} kPa/s) — MAE nunca activa en ellos")
        print()
        print(f"  Pasos en TunerStudio:")
        print(f"    1. Fuel → Accel Enrichment → MAP AE Threshold = {sug_map_thresh} kPa/s")
        print(f"    2. Fuel → Accel Enrichment → TPS% = {sug_tps_prop}")
        print(f"    3. Tomar log, correr análisis AE, verificar que near-stalls desaparezcan")
    else:
        print(f"  Blend y mapThresh parecen adecuados para la cobertura observada.")

    # ── Evaluación y sugerencia de maeBins ───────────────────────
    mae_weight = (100 - sug_tps_prop) / 100.0   # fracción de peso MAE en el blend

    print()
    print("── EVALUACIÓN maeBins ──────────────────────────────────────")

    # Para cada bin de maeRates, recolectar eventos en ese rango de MAPdot
    # y calcular el PW adicional necesario: needed_pw = pw_avg * (lean / afr_target)
    # maeBins_sug = needed_pw / mae_weight  (para que la contribución efectiva sea suficiente)
    bin_edges_mae = [0] + list(mae_rate_vals)
    cur_mae_bins  = list(mae_bins or [])

    # Calcular needed_pw por bin (solo bins con datos de PW)
    raw_sug = []    # (lo, hi, n_ev, sug_or_None)
    for k in range(len(mae_rate_vals)):
        lo = bin_edges_mae[k]
        hi = mae_rate_vals[k]
        # Último bin: sin límite superior
        bin_evs = [e for e in mae_cand
                   if (lo <= e['mapdot_max'] < hi if k < len(mae_rate_vals)-1
                       else e['mapdot_max'] >= lo)
                   and e.get('pw_avg') and e['lean_vs_target'] > 0.3]
        if bin_evs:
            pw_med   = sorted([e['pw_avg']           for e in bin_evs])[len(bin_evs)//2]
            lean_med = _mean([e['lean_vs_target']    for e in bin_evs])
            afr_tgt  = _mean([e['afr_tgt_at_peak']  for e in bin_evs])
            needed_pw = pw_med * (lean_med / afr_tgt)
            sug = max(0.3, round(needed_pw / mae_weight, 1)) if mae_weight > 0 else needed_pw
        else:
            sug = None
        raw_sug.append((lo, int(hi), len(bin_evs), sug))

    any_data = any(r[3] is not None for r in raw_sug)

    if not any_data:
        # Sin PW en el log: los maeBins se escalan para dar una contribución efectiva
        # (maeBin × mae_weight) comparable a los taeBins a tasas equivalentes.
        # Techo explícito: nunca más de 3× el taeBin máximo para evitar valores absurdos.
        tae_bins_l = ae_cfg.get('taeBins') or [0.1, 0.2, 0.3, 0.7]
        tae_max    = max(tae_bins_l) if tae_bins_l else 0.7
        mae_max_r  = mae_rate_vals[-1] if mae_rate_vals else 86
        max_bin    = tae_max * 3.0   # techo absoluto
        sug_bins   = [max(0.3, min(max_bin, round(tae_max * (r / mae_max_r) / mae_weight, 1)))
                      for r in mae_rate_vals]
        note = "(estimado proporcional a taeBins — log sin columna PW)"
    else:
        # Rellenar bins sin datos interpolando desde los bins con datos
        sug_bins_raw = [r[3] for r in raw_sug]

        # Encuentra el primer y último bin con datos para interpolar el resto
        data_idx = [k for k, v in enumerate(sug_bins_raw) if v is not None]
        if data_idx:
            v_ref_hi = sug_bins_raw[data_idx[0]]  # primer valor con datos
            v_ref_lo = sug_bins_raw[data_idx[-1]]  # último valor con datos

        filled = list(sug_bins_raw)
        for k in range(len(filled)):
            if filled[k] is None:
                # Interpolar: escalar proporcionalmente al MAPdot rate
                rate_k = mae_rate_vals[k]
                # Buscar vecinos con datos
                prev = next((filled[j] for j in range(k-1,-1,-1) if filled[j] is not None), None)
                nxt  = next((filled[j] for j in range(k+1, len(filled)) if filled[j] is not None), None)
                if prev is not None and nxt is not None:
                    # Interpolación lineal en el espacio de tasas
                    pk = next(j for j in range(k-1,-1,-1) if filled[j] is not None)
                    nk = next(j for j in range(k+1, len(filled)) if filled[j] is not None)
                    t = (rate_k - mae_rate_vals[pk]) / (mae_rate_vals[nk] - mae_rate_vals[pk])
                    filled[k] = max(0.3, round(prev + t * (nxt - prev), 1))
                elif prev is not None:
                    # Extrapolar hacia adelante (bin más alto)
                    pk = next(j for j in range(k-1,-1,-1) if filled[j] is not None)
                    scale = rate_k / mae_rate_vals[pk] if mae_rate_vals[pk] > 0 else 1
                    filled[k] = max(0.3, round(prev * scale, 1))
                elif nxt is not None:
                    # Extrapolar hacia atrás (bin más bajo)
                    nk = next(j for j in range(k+1, len(filled)) if filled[j] is not None)
                    scale = rate_k / mae_rate_vals[nk] if mae_rate_vals[nk] > 0 else 1
                    filled[k] = max(0.3, round(nxt * scale, 1))
                else:
                    filled[k] = 0.5

        sug_bins = [max(0.3, round(v, 1)) for v in filled]
        note = "(calculado: PW_necesario ÷ peso_MAE, interpolado para bins sin datos)"

    print(f"  Peso MAE en el blend sugerido: {mae_weight*100:.0f}%")
    print(f"  Un maeBin activa así: contribución_efectiva = maeBin × {mae_weight*100:.0f}%")
    print()
    print(f"  {'MAPdot':>10}  {'n':>3}  {'maeBin actual':>14}  {'maeBin sugerido':>16}  Δ")
    print("  " + "─" * 56)
    for k, (lo, hi, n_ev, _sug) in enumerate(raw_sug):
        hi_str = f"{hi}+" if k == len(raw_sug)-1 else str(hi)
        cur = cur_mae_bins[k] if k < len(cur_mae_bins) else "—"
        sug = sug_bins[k] if k < len(sug_bins) else "?"
        if isinstance(cur, float) and isinstance(sug, float):
            d = sug - cur
            delta = f"{d:+.1f}" if abs(d) > 0.05 else "="
        else:
            delta = "?"
        eff_cur = f"→{cur*mae_weight:.2f}ms ef." if isinstance(cur, float) else ""
        eff_sug = f"→{sug*mae_weight:.2f}ms ef."
        print(f"  {lo:>3}–{hi_str:>4} kPa/s  {n_ev:>3}  {str(cur):>6}ms {eff_cur:>14}  "
              f"{sug:>6}ms {eff_sug:>14}  {delta}")
    print()
    print(f"  {note}")

    # ¿Los actuales son planos? (rango max-min < 20% del máximo)
    if cur_mae_bins and len(cur_mae_bins) >= 2:
        span = max(cur_mae_bins) - min(cur_mae_bins)
        if span < max(cur_mae_bins) * 0.20:
            print(f"  ⚠ maeBins actuales casi planos (rango {span:.2f}ms sobre máx {max(cur_mae_bins):.1f}ms).")
            print(f"  Deberían escalar con MAPdot, como taeBins escala con TPSdot.")

    if sug_bins != list(cur_mae_bins):
        print()
        sug_str = "[" + ", ".join(f"{v:.1f}" for v in sug_bins) + "]"
        print(f"  Sugerido para TunerStudio → maeBins = {sug_str}")
        print(f"  (Fuel → Accel Enrichment → MAE curve → fila Added (ms))")


# ─────────────────────────────────────────────
# 7b. CALIBRACIÓN WOT (plena carga — sección independiente)
# ─────────────────────────────────────────────

_WOT_TPS_THRESH     = 85.0   # % TPS mínimo para considerar "plena carga"
_WOT_MIN_RPM        = 1500   # rpm mínimo — evita arranque/ralentí alto
_WOT_MIN_PULL_SECS  = 2.0    # duración mínima de un tramo para contarlo como "pull"
_WOT_LEAN_DELTA_AFR = 0.3    # desvío AFR real-objetivo para marcar celda "pobre"
_WOT_RICH_DELTA_AFR = 0.3    # ídem para "rica"

# Umbrales para el diagnóstico de salud específico de WOT (sección 7c).
# Son pisos orientativos para un 4G15 N/A — no reemplazan el manual del
# fabricante, pero permiten detectar condiciones claramente anormales.
_WOT_OIL_WARN_PSI    = 25.0  # presión de aceite mínima esperable en plena carga
_WOT_OIL_CRIT_PSI    = 15.0  # por debajo de esto: riesgo real de daño a rodamientos
_WOT_OIL_SAG_PSI     = 8.0   # caída aceite (rpm baja→alta) que amerita revisión
_WOT_FP_SAG_PSI      = 5.0   # caída de presión de combustible bajo demanda creciente
_WOT_DC_WARN_PCT     = 85.0  # duty cycle de inyectores cerca del límite
_WOT_DC_CRIT_PCT     = 95.0  # duty cycle prácticamente saturado — VE ya no alcanza
_WOT_VOLT_WARN       = 13.0  # voltaje bajo carga — alternador al límite
_WOT_VOLT_CRIT       = 12.5  # voltaje bajo carga — crítico (afecta dwell/inyección)
_WOT_MAT_RISE_WARN   = 8.0   # °C de aumento de MAT dentro de un mismo pull (heat-soak)


def load_wot_rows(log_files: list, tps_thresh: float = _WOT_TPS_THRESH) -> list:
    """
    Parsea logs .msl/.mlg capturando frames de plena carga (WOT) SIN aplicar
    los filtros de estabilidad de crucero (rango de MAT, estabilidad de MAP,
    límites de mapdot/rpmdot) que usa load_msl_logs.

    Esos filtros existen para aislar celdas en condición estacionaria — pero
    en un pull de WOT el MAP sube con fuerza y el motor barre el rango de RPM
    rápidamente: son exactamente la condición que se quiere calibrar, no
    "ruido" a descartar. Aplicarlos descarta el tramo de interés completo
    (p.ej. un MAT de invierno fuera del rango [38-58°C] excluye el log
    entero sin avisar — justo lo que le pasó a este analizador).

    Conserva: motor caliente, AFR válido, TPS ≥ tps_thresh, sin AE activo y
    fuera del breve período de asentamiento posterior al AE (la mezcla tarda
    ~1.5 s en estabilizarse tras apagarse el enriquecimiento).
    """
    all_rows = []
    _last_ae_secl: dict = {}
    for fi, fname in enumerate(log_files):
        with open(fname, 'rb') as fh:
            raw = fh.read()

        # ── MLG ──────────────────────────────────────────────────
        if raw.startswith(_MLG_MAGIC):
            channels, data_start = _mlg_parse_header(raw)
            if channels is None or data_start is None:
                continue
            for row_raw in _mlg_iter_records(raw, channels, data_start):
                rpm      = row_raw.get('RPM', 0)
                afr      = row_raw.get('AFR', 0)
                clt      = row_raw.get('CLT', 0)
                tps      = row_raw.get('TPS', 0)
                accel_pw = row_raw.get('Accel PW', 0)
                secl_val = row_raw.get('SecL', 0) or 0
                if rpm < _WOT_MIN_RPM or not (8.0 < afr < 20.0) or clt < 70:
                    continue
                if tps < tps_thresh:
                    continue
                if accel_pw > 0.05:
                    _last_ae_secl[fi] = secl_val
                    continue
                if secl_val - _last_ae_secl.get(fi, -9999) < _POST_AE_COOLDOWN_SECS:
                    continue
                all_rows.append({
                    'rpm':           rpm,
                    'map':           row_raw.get('MAP', 0),
                    'afr':           afr,
                    'tps':           tps,
                    'clt':           clt,
                    'mat':           row_raw.get('MAT'),
                    'tpsdot':        row_raw.get('TPSdot', 0),
                    'mapdot':        row_raw.get('MAPdot', 0),
                    'rpmdot':        row_raw.get('RPMdot', 0),
                    'accel_pw':      accel_pw,
                    'ego_cor':       row_raw.get('EGO cor1', 100.0),
                    'afr_tgt':       row_raw.get('AFR Target 1'),
                    'secl':          secl_val,
                    'batt_v':        row_raw.get('Batt V'),
                    'pw':            row_raw.get('PW'),
                    'oil_pressure':  row_raw.get('OilPressure'),
                    'fuel_pressure': row_raw.get('fuel_pressure'),
                    'knock_retard':  row_raw.get('SPK: Knock retard'),
                    'duty_cycle':    row_raw.get('DutyCycle1'),
                    'dwell':         row_raw.get('Dwell'),
                    'baro':          row_raw.get('Barometer'),
                    'adv':           row_raw.get('SPK: Spark Advance'),
                    'mat_retard':    row_raw.get('SPK: MAT Retard'),
                    'file_idx':      fi,
                })
            continue

        # ── MSL ──────────────────────────────────────────────────
        text_start = raw.find(b'Time')
        if text_start < 0:
            continue
        text  = raw[text_start:].decode('latin-1', errors='replace')
        lines = text.split('\n')
        if len(lines) < 3:
            continue
        headers = lines[0].strip().split('\t')
        cols    = {h.strip(): i for i, h in enumerate(headers)}
        if any(c not in cols for c in ('RPM', 'MAP', 'AFR', 'TPS', 'CLT')):
            continue

        for line in lines[2:]:
            parts = line.strip().split('\t')
            if len(parts) < 10:
                continue
            try:
                rpm = float(parts[cols['RPM']])
                afr = float(parts[cols['AFR']])
                tps = float(parts[cols['TPS']])
                clt = float(parts[cols['CLT']])
            except (ValueError, IndexError):
                continue
            if rpm < _WOT_MIN_RPM or not (8.0 < afr < 20.0) or clt < 70:
                continue
            if tps < tps_thresh:
                continue
            try:
                accel_pw = float(parts[cols['Accel PW']]) if 'Accel PW' in cols else 0.0
                secl_val = float(parts[cols['SecL']])     if 'SecL'     in cols else 0.0
            except (ValueError, IndexError):
                continue
            if accel_pw > 0.05:
                _last_ae_secl[fi] = secl_val
                continue
            if secl_val - _last_ae_secl.get(fi, -9999) < _POST_AE_COOLDOWN_SECS:
                continue
            try:
                row = {
                    'rpm': rpm, 'map': float(parts[cols['MAP']]), 'afr': afr,
                    'tps': tps, 'clt': clt,
                    'mat':      float(parts[cols['MAT']])          if 'MAT'          in cols else None,
                    'tpsdot':   float(parts[cols['TPSdot']])       if 'TPSdot'       in cols else 0.0,
                    'mapdot':   float(parts[cols['MAPdot']])       if 'MAPdot'       in cols else 0.0,
                    'rpmdot':   float(parts[cols['RPMdot']])       if 'RPMdot'       in cols else 0.0,
                    'accel_pw': accel_pw,
                    'ego_cor':  float(parts[cols['EGO cor1']])     if 'EGO cor1'     in cols else 100.0,
                    'afr_tgt':  float(parts[cols['AFR Target 1']]) if 'AFR Target 1' in cols else None,
                    'secl':     secl_val,
                    'batt_v':   float(parts[cols['Batt V']])       if 'Batt V'       in cols else None,
                    'pw':       float(parts[cols['PW']])           if 'PW'           in cols else None,
                    'oil_pressure':  float(parts[cols['OilPressure']])         if 'OilPressure'         in cols else None,
                    'fuel_pressure': float(parts[cols['fuel_pressure']])       if 'fuel_pressure'       in cols else None,
                    'knock_retard':  float(parts[cols['SPK: Knock retard']])   if 'SPK: Knock retard'   in cols else None,
                    'duty_cycle':    float(parts[cols['DutyCycle1']])          if 'DutyCycle1'          in cols else None,
                    'dwell':         float(parts[cols['Dwell']])               if 'Dwell'               in cols else None,
                    'baro':          float(parts[cols['Barometer']])           if 'Barometer'           in cols else None,
                    'adv':           float(parts[cols['SPK: Spark Advance']])  if 'SPK: Spark Advance'  in cols else None,
                    'mat_retard':    float(parts[cols['SPK: MAT Retard']])     if 'SPK: MAT Retard'     in cols else None,
                    'file_idx': fi,
                }
            except (ValueError, IndexError):
                continue
            all_rows.append(row)

    return all_rows


def detect_wot_pulls(rows: list, min_duration_secs: float = _WOT_MIN_PULL_SECS) -> list:
    """Agrupa filas WOT consecutivas (mismo archivo, gap de SecL ≤ 1.5 s) en 'pulls'."""
    if not rows:
        return []
    rows_s = sorted(rows, key=lambda r: (r.get('file_idx', 0), r.get('secl', 0) or 0))
    groups = []
    g = [rows_s[0]]
    for i in range(1, len(rows_s)):
        prev, cur = rows_s[i - 1], rows_s[i]
        if cur.get('file_idx', 0) == prev.get('file_idx', 0) and \
           (cur.get('secl', 0) or 0) - (prev.get('secl', 0) or 0) <= 1.5:
            g.append(cur)
        else:
            groups.append(g)
            g = [cur]
    groups.append(g)

    pulls = []
    for g in groups:
        secs = sorted({r.get('secl', 0) or 0 for r in g})
        duration = secs[-1] - secs[0]
        if duration < min_duration_secs:
            continue
        rpms  = [r['rpm'] for r in g]
        afrs  = [r['afr'] for r in g]
        diffs = [r['afr'] - r['afr_tgt'] for r in g if r.get('afr_tgt') is not None]
        tgts  = [r['afr_tgt'] for r in g if r.get('afr_tgt') is not None]
        pulls.append({
            'file_idx':    g[0].get('file_idx', 0),
            'secl_start':  secs[0],
            'secl_end':    secs[-1],
            'duration':    duration,
            'n':           len(g),
            'rpm_start':   rpms[0],
            'rpm_end':     rpms[-1],
            'afr_avg':     sum(afrs) / len(afrs),
            'afr_tgt_avg': sum(tgts) / len(tgts) if tgts else None,
            'lean_avg':    sum(diffs) / len(diffs) if diffs else None,
            'lean_max':    max(diffs) if diffs else None,
        })
    return pulls


def analyze_wot_calibration(rows: list, ve_data: dict, min_samples: int = 3) -> dict:
    """
    Corrección de VE específica para celdas alcanzadas en WOT (plena carga).

    Dos diferencias clave frente a analyze():
    1. Clasifica pobre/rica RELATIVO al AFR objetivo de cada celda, no con
       los umbrales absolutos de crucero (~14.2-14.5 / ~13.0): a plena carga
       el objetivo ronda 12.5-13.0, así que un AFR real de 13.8 ya es ~1
       punto más pobre que el objetivo aunque esté lejos de 14.5.
    2. No aplica _dwell_filter: durante un pull el motor atraviesa cada
       celda en 1-2 s — exigir 2 s continuos descartaría casi todo el dato.
    """
    rpm_bins = ve_data['rpm_bins']
    map_bins = ve_data['map_bins']
    ve       = ve_data['ve']

    cell_samples: dict = {}
    for row in rows:
        mi = find_bin(row['map'], map_bins)
        ri = find_bin(row['rpm'], rpm_bins)
        cell_samples.setdefault((mi, ri), []).append(row)

    lean_cells, rich_cells, ok_cells = [], [], []
    for (mi, ri), samples in sorted(cell_samples.items()):
        m, r = map_bins[mi], rpm_bins[ri]
        if len(samples) < min_samples:
            continue

        afrs = sorted(s['afr'] for s in samples)
        mid  = len(afrs) // 2
        avg  = (afrs[mid - 1] + afrs[mid]) / 2 if len(afrs) % 2 == 0 else afrs[mid]

        tgt_vals = [s['afr_tgt'] for s in samples if s.get('afr_tgt') is not None]
        if tgt_vals:
            tv = sorted(tgt_vals)
            tm = len(tv) // 2
            tgt = (tv[tm - 1] + tv[tm]) / 2 if len(tv) % 2 == 0 else tv[tm]
        else:
            tgt = target_afr(m)

        vc = ve[mi][ri]
        raw_delta = vc * avg / tgt - vc
        MAX_DELTA = 10
        delta = max(-MAX_DELTA, min(MAX_DELTA, round(raw_delta)))
        vn    = round(vc) + delta

        secs = sorted({s.get('secl', 0) or 0 for s in samples})
        entry = {
            'mi': mi, 'ri': ri, 'map': m, 'rpm': r,
            'afr_avg': avg, 'target': tgt, 'n': len(afrs), 'n_secs': len(secs),
            've_cur': vc, 've_new': vn, 'delta': delta,
        }

        diff = avg - tgt
        if diff > _WOT_LEAN_DELTA_AFR:
            lean_cells.append(entry)
        elif diff < -_WOT_RICH_DELTA_AFR:
            rich_cells.append(entry)
        else:
            ok_cells.append(entry)

    return {
        'lean':    lean_cells,
        'rich':    rich_cells,
        'ok':      ok_cells,
        'n_rows':  len(rows),
        'n_cells': len(cell_samples),
    }


def print_wot_calibration(result: dict, pulls: list, tps_thresh: float = _WOT_TPS_THRESH) -> None:
    lean = result['lean']
    rich = result['rich']
    ok   = result['ok']

    print()
    print("═" * 70)
    print("  CALIBRACIÓN WOT (plena carga) — sección independiente")
    print("═" * 70)
    print(f"  Umbral TPS considerado WOT  : ≥ {tps_thresh:.0f}%")
    print(f"  Pulls de WOT detectados     : {len(pulls)}")
    print(f"  Filas WOT analizadas        : {result['n_rows']:,}")
    print(f"  Celdas con datos suficientes: {result['n_cells']}")
    print(f"  Celdas POBRES               : {len(lean)}")
    print(f"  Celdas RICAS                : {len(rich)}")
    print(f"  Celdas OK                   : {len(ok)}")
    print()
    print("  Esta sección IGNORA los filtros de MAT, estabilidad de MAP y")
    print("  mapdot/rpmdot del análisis VE de crucero — en un pull de WOT esas")
    print("  variaciones SON la condición a calibrar, no ruido a descartar.")
    print("  La clasificación pobre/rica es relativa al AFR objetivo de cada")
    print(f"  celda (no a los umbrales de crucero): un desvío de ±{_WOT_LEAN_DELTA_AFR:.1f} AFR")
    print("  ya es señal de corrección a esta carga.")

    if pulls:
        print()
        print("  ── PULLS DETECTADOS ──")
        print(f"  {'#':>3}  {'arch':>4}  {'SecL':>11}  {'dur':>5}  {'RPM':>13}  "
              f"{'AFR prom':>9}  {'obj prom':>9}  {'Δ prom':>7}  {'Δ máx':>7}")
        for i, p in enumerate(pulls, 1):
            secl_r = f"{p['secl_start']:.0f}-{p['secl_end']:.0f}"
            rpm_r  = f"{p['rpm_start']:.0f}→{p['rpm_end']:.0f}"
            tgt_s  = f"{p['afr_tgt_avg']:.2f}" if p['afr_tgt_avg'] is not None else "  -  "
            lean_s = f"{p['lean_avg']:+.2f}"   if p['lean_avg']   is not None else "  -  "
            leax_s = f"{p['lean_max']:+.2f}"   if p['lean_max']   is not None else "  -  "
            print(f"  {i:3d}  {p['file_idx']:4d}  {secl_r:>11}  {p['duration']:4.1f}s  "
                  f"{rpm_r:>13}  {p['afr_avg']:9.2f}  {tgt_s:>9}  {lean_s:>7}  {leax_s:>7}")

    if not lean and not rich:
        print()
        print("  ✓ Sin celdas WOT fuera de objetivo (con los datos disponibles).")
        if result['n_rows'] == 0:
            print("  ⚠ No se encontraron filas de WOT — verifica que algún tramo del")
            print(f"    log supere TPS ≥ {tps_thresh:.0f}% con el motor caliente (CLT > 70°C).")
        return

    def _table(cells, title, sort_key, reverse):
        print()
        print(f"  ── {title} ──")
        print(f"  {'MAP':>5}  {'RPM':>5}  {'AFR real':>9}  {'objetivo':>9}  "
              f"{'Δ AFR':>7}  {'n':>5}  {'VE actual':>10}  {'VE sug.':>8}  {'Δ VE':>6}")
        for c in sorted(cells, key=sort_key, reverse=reverse):
            print(f"  {c['map']:5.0f}  {c['rpm']:5.0f}  {c['afr_avg']:9.2f}  {c['target']:9.2f}  "
                  f"{c['afr_avg'] - c['target']:+7.2f}  {c['n']:5d}  "
                  f"{c['ve_cur']:10.0f}  {c['ve_new']:8.0f}  {c['delta']:+6d}")

    if lean:
        _table(lean, "CELDAS POBRES (AFR real > objetivo)",
               lambda x: x['afr_avg'] - x['target'], True)
    if rich:
        _table(rich, "CELDAS RICAS (AFR real < objetivo)",
               lambda x: x['afr_avg'] - x['target'], False)

    print()
    print("  Recordatorio: estas sugerencias de VE son orientativas — confirma")
    print("  con varios pulls bajo distintas condiciones antes de aplicar")
    print("  cambios definitivos a la tabla.")


def _pressure_profile(rows: list, key: str, valid_range: tuple) -> dict | None:
    """
    Compara la presión (aceite o combustible) entre la mitad de menor RPM y
    la mitad de mayor RPM dentro del rango de WOT observado.

    Por qué RPM y no tiempo: en un pull, la RPM es un proxy directo de la
    demanda — más RPM = más caudal de aceite requerido por el motor y más
    caudal de combustible exigido a la bomba/regulador. Una presión que cae
    justo cuando la demanda sube ('sag') es la firma clásica de una bomba al
    límite, un filtro restringido, o (en aceite) un pickup que se descubre.
    Retorna None si no hay sensor o hay muy pocos datos válidos.
    """
    pts = sorted(((r['rpm'], r[key]) for r in rows
                  if r.get(key) is not None and valid_range[0] < r[key] < valid_range[1]),
                 key=lambda x: x[0])
    if len(pts) < 20:
        return None

    mid = len(pts) // 2
    lo, hi = pts[:mid], pts[mid:]
    rpm_lo, val_lo = [r for r, _ in lo], [v for _, v in lo]
    rpm_hi, val_hi = [r for r, _ in hi], [v for _, v in hi]
    vals = [v for _, v in pts]
    return {
        'n':          len(pts),
        'avg':        _mean(vals),
        'min':        min(vals),
        'max':        max(vals),
        'rpm_lo_avg': _mean(rpm_lo),
        'rpm_hi_avg': _mean(rpm_hi),
        'val_lo_rpm': _mean(val_lo),
        'val_hi_rpm': _mean(val_hi),
        'sag':        _mean(val_lo) - _mean(val_hi),   # positivo = la presión cae al subir RPM
    }


def analyze_wot_health(rows: list, pulls: list) -> dict:
    """
    Diagnóstico de salud calculado SOLO con las filas de WOT (plena carga) —
    la condición de mayor exigencia mecánica y eléctrica del motor, y la que
    más rápido expone problemas marginales que en crucero pasan inadvertidos:
    presión de aceite o combustible que no sostienen el caudal bajo demanda,
    detonación, inyectores saturados, caída de voltaje, heat-soak de admisión.

    A diferencia de analyze_health() (panorama general del motor en todo el
    log), aquí cada métrica se aísla a la ventana de WOT — mezclar esas filas
    con ralentí o crucero diluiría justo las señales que interesa ver.
    """
    n = len(rows)
    health: dict = {'n_rows': n}
    if n == 0:
        return health

    def fv(key):
        return [r[key] for r in rows if r.get(key) is not None]

    # ── Presión de aceite y de combustible: ¿se sostienen al subir la demanda? ──
    health['oil_pressure']  = _pressure_profile(rows, 'oil_pressure',  (5, 150))
    health['fuel_pressure'] = _pressure_profile(rows, 'fuel_pressure', (5, 150))

    # ── Detonación — cualquier evento bajo carga máxima merece atención ──
    knock = fv('knock_retard')
    health['knock'] = {
        'has_sensor': len(knock) > 0,
        'events':     sum(1 for v in knock if v > 0),
        'max':        max(knock) if knock else 0,
    }

    # ── Saturación de inyectores: si el duty cycle ronda el límite, el motor ──
    # ── ya no puede recibir más combustible aunque la tabla VE lo pida       ──
    dc = fv('duty_cycle')
    health['injectors'] = {
        'has_sensor':     len(dc) > 0,
        'avg':            _mean(dc),
        'max':            max(dc) if dc else 0,
        'above_warn_pct': _pct(sum(1 for v in dc if v > _WOT_DC_WARN_PCT), len(dc)),
        'above_crit_pct': _pct(sum(1 for v in dc if v > _WOT_DC_CRIT_PCT), len(dc)),
    }

    # ── Voltaje bajo la mayor exigencia eléctrica (bomba + ignición a alta RPM) ──
    batt = [v for v in fv('batt_v') if 5 < v < 30]
    health['voltage'] = {
        'has_sensor': len(batt) > 0,
        'avg':        _mean(batt),
        'min':        min(batt) if batt else None,
        'below_warn': sum(1 for v in batt if v < _WOT_VOLT_WARN),
        'below_crit': sum(1 for v in batt if v < _WOT_VOLT_CRIT),
    }

    # ── Heat-soak: ¿sube la MAT de forma sostenida dentro de un mismo pull? ──
    rises = []
    for p in pulls:
        prows = [r for r in rows
                 if r.get('file_idx') == p['file_idx']
                 and p['secl_start'] <= (r.get('secl') or -1) <= p['secl_end']]
        mats = [r['mat'] for r in prows if r.get('mat') is not None]
        if len(mats) >= 3:
            rises.append(max(mats) - min(mats))
    health['heat_soak'] = {
        'n_pulls':  len(rises),
        'max_rise': max(rises) if rises else None,
        'avg_rise': _mean(rises) if rises else None,
    }

    # ── Dwell de bobina bajo carga (chispa débil pasa inadvertida en ralentí) ──
    dwell = [v for v in fv('dwell') if v > 0]
    health['dwell'] = {
        'has_sensor': len(dwell) > 0,
        'avg':        _mean(dwell),
        'min':        min(dwell) if dwell else None,
        'below_2':    sum(1 for v in dwell if v < 2.0),
    }

    return health


def print_wot_health(health: dict) -> None:
    if health.get('n_rows', 0) == 0:
        return

    def row(*parts):
        print('  ' + '  '.join(str(p) for p in parts))

    def note(text):
        print(f"  → {text}")

    def sec(title):
        print(f"\n  ── {title} ──")

    print()
    print("─" * 70)
    print("  SALUD DEL MOTOR EN WOT (plena carga)")
    print("─" * 70)
    print("  Cada métrica se calcula SOLO con las filas de WOT — son las")
    print("  condiciones de mayor exigencia mecánica y eléctrica, y las que")
    print("  más rápido revelan problemas que en crucero pasan inadvertidos.")
    print("  Los umbrales son pisos orientativos para un 4G15 N/A, no specs")
    print("  de fábrica: úsalos para decidir qué revisar, no como veredicto final.")

    any_critical = False

    # ── Presión de aceite ──
    sec('Presión de aceite')
    op = health['oil_pressure']
    if op:
        crit     = op['min'] < _WOT_OIL_CRIT_PSI
        warn     = (not crit) and op['min'] < _WOT_OIL_WARN_PSI
        sag_warn = op['sag'] > _WOT_OIL_SAG_PSI
        row(f"Prom: {op['avg']:.1f} PSI   Mín: {op['min']:.1f} PSI   Máx: {op['max']:.1f} PSI"
            f"   (n={op['n']:,})")
        row(f"A ~{op['rpm_lo_avg']:.0f} rpm: {op['val_lo_rpm']:.1f} PSI   →   "
            f"a ~{op['rpm_hi_avg']:.0f} rpm: {op['val_hi_rpm']:.1f} PSI"
            f"   (Δ {-op['sag']:+.1f} PSI)")
        if crit:
            any_critical = True
            row(f"⚠ CRÍTICO: mínimo {op['min']:.1f} PSI — bajo el piso orientativo de "
                f"{_WOT_OIL_CRIT_PSI:.0f} PSI en plena carga")
            note("Riesgo real de daño a rodamientos — revisa bomba, filtro/colador, "
                 "nivel y viscosidad de aceite antes de seguir exigiendo el motor")
        elif warn:
            row(f"⚠ REVISAR: mínimo {op['min']:.1f} PSI — bajo el piso orientativo de "
                f"{_WOT_OIL_WARN_PSI:.0f} PSI")
        else:
            row(f"✓ Se mantiene sobre {_WOT_OIL_WARN_PSI:.0f} PSI durante la plena carga")
        if sag_warn:
            row(f"⚠ La presión cae {op['sag']:.1f} PSI al subir de "
                f"~{op['rpm_lo_avg']:.0f} a ~{op['rpm_hi_avg']:.0f} rpm")
            note("Una bomba sana sostiene o sube la presión con la RPM — una caída "
                 "sugiere bomba desgastada, aireación del aceite o nivel bajo")
    else:
        row("Sin sensor OilPressure en el log — no se puede evaluar")

    # ── Presión de combustible ──
    sec('Presión de combustible')
    fp = health['fuel_pressure']
    if fp:
        sag_crit = fp['sag'] > _WOT_FP_SAG_PSI
        row(f"Prom: {fp['avg']:.1f} PSI   Mín: {fp['min']:.1f} PSI   Máx: {fp['max']:.1f} PSI"
            f"   (n={fp['n']:,})")
        row(f"A ~{fp['rpm_lo_avg']:.0f} rpm: {fp['val_lo_rpm']:.1f} PSI   →   "
            f"a ~{fp['rpm_hi_avg']:.0f} rpm: {fp['val_hi_rpm']:.1f} PSI"
            f"   (Δ {-fp['sag']:+.1f} PSI)")
        if sag_crit:
            any_critical = True
            row(f"⚠ CRÍTICO: la presión cae {fp['sag']:.1f} PSI justo al subir la demanda "
                f"(~{fp['rpm_lo_avg']:.0f} → ~{fp['rpm_hi_avg']:.0f} rpm)")
            note("Bomba al límite de caudal, filtro restringido, o regulador que no "
                 "compensa — esto empobrece la mezcla justo en plena carga, donde más "
                 "se necesita combustible y menos margen hay para error")
        else:
            row("✓ Presión sostenida — no cae de forma relevante con la demanda")
    else:
        row("Sin sensor de presión de combustible en el log — no se puede evaluar directo")
        note("Si la calibración WOT muestra empobrecimiento creciente con el MAP, "
             "considera la presión de combustible como sospechoso indirecto")

    # ── Detonación ──
    sec('Detonación (knock)')
    kn = health['knock']
    if kn['has_sensor']:
        if kn['events'] > 0:
            any_critical = True
            row(f"⚠ CRÍTICO: {kn['events']} evento(s) de retraso por detonación durante "
                f"WOT (máx {kn['max']:.1f}°)")
            note("Detonación sostenida en plena carga puede dañar pistones/bielas — "
                 "revisa octanaje del combustible, MAT, avance de encendido y la "
                 "mezcla en esas celdas específicas")
        else:
            row("✓ Sin eventos de detonación detectados durante los pulls de WOT")
    else:
        row("Sin canal de Knock retard en el log — no se puede confirmar ausencia de detonación")

    # ── Inyectores / duty cycle ──
    sec('Saturación de inyectores (duty cycle)')
    inj = health['injectors']
    if inj['has_sensor']:
        crit = inj['above_crit_pct'] > 0
        warn = (not crit) and inj['above_warn_pct'] > 0
        row(f"Prom: {inj['avg']:.1f}%   Máx: {inj['max']:.1f}%")
        if crit:
            any_critical = True
            row(f"⚠ CRÍTICO: {inj['above_crit_pct']:.1f}% del tiempo en WOT sobre "
                f"{_WOT_DC_CRIT_PCT:.0f}% — inyectores prácticamente saturados")
            note("Si una celda sale pobre aquí, NO se corrige solo subiendo VE: el "
                 "inyector ya no puede entregar más caudal (evalúa inyectores de mayor flujo)")
        elif warn:
            row(f"⚠ REVISAR: {inj['above_warn_pct']:.1f}% del tiempo en WOT sobre "
                f"{_WOT_DC_WARN_PCT:.0f}% — cerca del límite de los inyectores")
        else:
            row(f"✓ Margen suficiente — máximo observado {inj['max']:.1f}%")
    else:
        row("Sin canal DutyCycle1 en el log — no se puede evaluar")

    # ── Voltaje ──
    sec('Voltaje bajo carga máxima')
    v = health['voltage']
    if v['has_sensor']:
        crit = v['below_crit'] > 0
        warn = (not crit) and v['below_warn'] > 0
        row(f"Prom: {v['avg']:.2f}V   Mín: {v['min']:.2f}V")
        if crit:
            any_critical = True
            row(f"⚠ CRÍTICO: {v['below_crit']} lectura(s) bajo {_WOT_VOLT_CRIT:.1f}V durante WOT")
            note("Voltaje bajo en plena carga reduce el ancho de pulso real (battFac), "
                 "el dwell y la energía de chispa — justo cuando el motor más los exige")
        elif warn:
            row(f"⚠ REVISAR: {v['below_warn']} lectura(s) bajo {_WOT_VOLT_WARN:.1f}V durante WOT")
        else:
            row(f"✓ Voltaje sostenido sobre {_WOT_VOLT_WARN:.1f}V")
    else:
        row("Sin canal Batt V en el log — no se puede evaluar")

    # ── Heat-soak (MAT) ──
    sec('Heat-soak de admisión (subida de MAT durante el pull)')
    hs = health['heat_soak']
    if hs['n_pulls'] > 0:
        warn = hs['max_rise'] > _WOT_MAT_RISE_WARN
        row(f"Pulls evaluados: {hs['n_pulls']}   Subida prom: {hs['avg_rise']:+.1f}°C   "
            f"Subida máx: {hs['max_rise']:+.1f}°C")
        if warn:
            row(f"⚠ REVISAR: la MAT sube más de {_WOT_MAT_RISE_WARN:.0f}°C dentro de un mismo pull")
            note("El aire se calienta con el motor en marcha (radiación del múltiple, "
                 "ubicación del sensor, falta de aislación) — reduce densidad y potencia, "
                 "y sube el riesgo de detonación en pulls largos o repetidos")
        else:
            row("✓ MAT estable dentro de cada pull")
    else:
        row("Sin datos suficientes de MAT por pull para evaluar heat-soak")

    # ── Dwell ──
    sec('Dwell de bobina bajo carga')
    d = health['dwell']
    if d['has_sensor']:
        if d['below_2'] > 0:
            row(f"⚠ {d['below_2']} lectura(s) de dwell bajo 2.0 ms durante WOT "
                f"(prom {d['avg']:.2f} ms, mín {d['min']:.2f} ms)")
            note("Dwell bajo a alta RPM puede producir chispa débil y fallos de "
                 "combustión intermitentes justo bajo carga")
        else:
            row(f"✓ Dwell sostenido (prom {d['avg']:.2f} ms, mín {d['min']:.2f} ms)")
    else:
        row("Sin canal Dwell en el log — no se puede evaluar")

    print()
    if any_critical:
        print("  ⚠ Hay condiciones que conviene revisar ANTES de seguir afinando la")
        print("    mezcla en WOT — un problema mecánico o eléctrico de base puede")
        print("    producir lecturas de AFR engañosas (p.ej. presión de combustible")
        print("    cayendo simula una celda 'pobre' que no se arregla con VE).")
    else:
        print("  ✓ No se detectaron problemas críticos de salud durante los pulls de")
        print("    WOT analizados (con los sensores disponibles en el log).")


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
    parser.add_argument('--predict', action='store_true',
                        help='Predecir VE para celdas sin cobertura usando zero.table '
                             'y el patrón de correcciones observado')
    parser.add_argument('--fuse-definitive', action='store_true',
                        help='Fusionar todas las _corrected.table en una tabla definitiva '
                             'con proyección matemática del lean global a celdas sin datos')
    parser.add_argument('--base-percentile', type=float, default=0.50,
                        help='Percentil del factor lean a usar como base global '
                             '(default: 0.50 = mediana; 0.75 = más agresivo)')
    parser.add_argument('--ae-cal', action='store_true',
                        help='Calibrar AE: analiza eventos de aceleración y sugiere nuevos taeBins/taeRates')
    parser.add_argument('--include-idle', action='store_true',
                        help='Incluir ralentí estable (TPS<3%%, CLT>70°C) en correcciones VE')
    parser.add_argument('--detail', action='store_true',
                        help='Mostrar tramos temporales individuales por celda corregida')
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

    # ── Predicción de celdas sin cobertura (no requiere logs) ──
    if args.predict:
        predict_uncovered_cells(table_dir, table_num)
        return

    # ── Fusión definitiva (no requiere logs) ──
    if args.fuse_definitive:
        fuse_definitive_table(table_dir, table_num,
                              base_percentile=args.base_percentile)
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

    # ── Calibración AE ──
    if args.ae_cal:
        if not os.path.exists(msq_path):
            print(f"Error: no se encontró {args.msq} — necesario para calibración AE")
            sys.exit(1)
        ae_cfg_cal = load_ae_config(msq_path)
        ae_events  = detect_ae_events(rows_full, ae_cfg_cal)
        ae_cal_res = analyze_ae_calibration(ae_events, ae_cfg_cal)
        print_ae_calibration(ae_cal_res, ae_cfg_cal)

    if args.health_only:
        return

    if not os.path.exists(msq_path):
        print(f"Error: no se encontró {args.msq}")
        sys.exit(1)

    # ── Cargar tabla VE y config AE desde MSQ ──
    print(f"\nCargando tabla VE (tabla {table_num}):")
    ve_data  = load_ve_table(msq_path, table_num)
    ae_cfg   = load_ae_config(msq_path)
    inj_cfg  = load_inj_config(msq_path)

    # ── Cargar historial desde .table files ──
    history = load_history_from_tables(table_dir, table_num)

    # Cargar versión filtrada (motor caliente, AFR válido) para análisis VE
    rows = load_msl_logs(log_files, include_idle=args.include_idle)
    if not rows:
        print("No se encontraron muestras válidas para análisis VE.")
        sys.exit(1)

    # ── Análisis VE ──
    result = analyze(rows, ve_data, ae_cfg, min_samples=args.min_samples,
                     history=history, inj_cfg=inj_cfg)

    # ── Reporte VE ──
    print_report(result, ae_cfg, log_files, ve_data, include_idle=args.include_idle,
                 detail=args.detail)

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
