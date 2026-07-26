"""
smoggy_grid.py — koini logiki gia to Conditions layer (#053).

PROSOXI: to _rhi / _tCrit / _ctState einai PISTI metafora apo to index-238.html
(gr. 2730-2746 kai 2855). Epalitheftike me node: 8/8 idies times.
AN ALLAKSEI TO ENA, PREPEI NA ALLAKSEI KAI TO ALLO — alliws to dataset dixotomeitai.
"""
import math
import numpy as np

# ---------------------------------------------------------------- FYSIKI
# Schmidt-Appleman (Schumann 1996). Idies statheres me tin app.
EI, CP, EPS, Q, ETA = 1.25, 1004.0, 0.622, 43e6, 0.30


def eW(t):
    return 6.112 * np.exp(17.62 * t / (243.12 + t))


def eI(t):
    return 6.112 * np.exp(22.46 * t / (272.62 + t))


def rhi(rh, t):
    """RH (mixed-phase, ECMWF paramId 157) -> RH pano apo pago. Vectorised."""
    rh = np.asarray(rh, dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    r = eW(t) / eI(t)
    a = ((t + 23.0) / 23.0) ** 2
    out = rh * (1.0 + a * (r - 1.0))          # -23 < t < 0
    out = np.where(t >= 0.0, rh * r, out)     # t >= 0
    out = np.where(t <= -23.0, rh, out)       # t <= -23 -> KAMIA metatropi
    return out


def t_crit(p_pa):
    """Kritiki thermokrasia SAC se C. p_pa se Pascal. Scalar."""
    g = (EI * CP * p_pa) / (EPS * Q * (1.0 - ETA))
    if g - 0.053 <= 0:
        return None
    x = math.log(g - 0.053)
    return -46.46 + 9.43 * x + 0.720 * x * x


def ct_state(t_c, hpa, rhi_pct):
    """0=tipota  1=vraxyvio  2=monimo  3=monimo+aplonei. Vectorised -> uint8."""
    tc = t_crit(hpa * 100.0)
    t_c = np.asarray(t_c, dtype=np.float64)
    rhi_pct = np.asarray(rhi_pct, dtype=np.float64)
    st = np.zeros(t_c.shape, dtype=np.uint8)
    if tc is None:
        return st
    sac = t_c < tc
    st = np.where(sac, 1, 0).astype(np.uint8)
    st = np.where(sac & (rhi_pct >= 100.0), 2, st).astype(np.uint8)
    st = np.where(sac & (rhi_pct > 120.0), 3, st).astype(np.uint8)
    return st


# ---------------------------------------------------------------- PROVOLI
MERC_LAT_MAX = 85.05112877980659   # to orio tou Web Mercator


def mercator_row_lats(height):
    """Gia kathe grammi tou PNG, poio geografiko platos antistoixei (kentro pixel)."""
    j = np.arange(height, dtype=np.float64)
    yn = (j + 0.5) / height
    return np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * yn))))


def mercator_col_lons(width):
    i = np.arange(width, dtype=np.float64)
    return -180.0 + 360.0 * (i + 0.5) / width


def resample_to_mercator(field, src_lats, src_lons, width, height):
    """
    field: 2D (nlat, nlon) sto kanoniko plegma lat/lon tou ECMWF.
    Epistrefei 2D (height, width) se Web Mercator, nearest-neighbour.
    Nearest-neighbour epitidis: DEN efevriskoume times pou den metrithikan.
    """
    out_lats = mercator_row_lats(height)
    out_lons = mercator_col_lons(width)

    src_lats = np.asarray(src_lats, dtype=np.float64)
    src_lons = np.asarray(src_lons, dtype=np.float64)

    # oi lat tou ECMWF pane 90 -> -90 (fthinouses)
    desc = src_lats[0] > src_lats[-1]
    lat_ref = src_lats[::-1] if desc else src_lats
    ri = np.searchsorted(lat_ref, out_lats)
    ri = np.clip(ri, 1, len(lat_ref) - 1)
    left = lat_ref[ri - 1]
    right = lat_ref[ri]
    ri = np.where(np.abs(out_lats - left) <= np.abs(out_lats - right), ri - 1, ri)
    row_idx = (len(lat_ref) - 1 - ri) if desc else ri

    # oi lon tou ECMWF pane 0 -> 359.75
    lon_q = np.mod(out_lons, 360.0)
    dlon = float(src_lons[1] - src_lons[0])
    col_idx = np.mod(np.round((lon_q - src_lons[0]) / dlon).astype(np.int64),
                     len(src_lons))

    return field[np.ix_(row_idx, col_idx)]


# ---------------------------------------------------------------- XROMATA
# LOCKED: kanena neo xroma. Xrisimopoioume to brand blue #44aaff me 3 alphas.
# POTE kokkino (kokkino = apoklisi apo forecast, mono).
BLUE = (0x44, 0xAA, 0xFF)
ALPHA = {0: 0, 1: 45, 2: 105, 3: 165}


def state_to_rgba(state):
    h, w = state.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[..., 0] = BLUE[0]
    img[..., 1] = BLUE[1]
    img[..., 2] = BLUE[2]
    a = np.zeros((h, w), dtype=np.uint8)
    for k, v in ALPHA.items():
        a = np.where(state == k, v, a).astype(np.uint8)
    img[..., 3] = a
    return img
