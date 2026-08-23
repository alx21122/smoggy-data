#!/usr/bin/env python3
"""
make_conditions.py — VIMA 2 tou #053.  (sti RIZA tou repo smoggy-data)

Katevazei ENA subset apo to ECMWF open data (t + r sta 250 hPa) GIA KATHE
step [0,3] tou idiou run, ypologizei tin katastasi contrail me tin IDIA
fysiki me tin app, kai grafei ENA PNG ANA step se provoli Web Mercator —
2 PNG/run, 8 PNG/mera synolika (idio cron, 4 runs/mera).

ECMWF open data: CC BY 4.0 — dorean, kai gia emporiki xrisi, ME anafora.
POTE olokliro arxeio: mia mera = ~726 GiB. Mono subset.
"""
import os
import sys
import datetime as dt

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoggy_grid as sg

LEVELS_HPA = [400, 300, 250, 200]   # #053 fix: AUTO (max-RHi anamesa se SAC-valid), oxi mono 250
# GR: O FAKELOS leg-etai "auto" (oxi pia "250hPa") giati DEN einai mono 250hPa
#     data — einai to AUTO apotelesma anamesa sta 4 ipsi. SYNTONISMENO DEPLOY:
#     auto to onoma PREPEI na tairiazei AKRIVOS me to COND_BASE sto index.html
#     (Netlify repo) — allios o xartis psaxnei se lathos fakelo kai vlepei keno.
WIDTH = HEIGHT = 1440
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "conditions", "auto")
GRIB = "/tmp/smoggy_ecmwf_step%d.grib2"
STEPS = [0, 3]                # VIMA 2 tou #053: kai +3h -> 3wro voima (00,03,06,...21Z)
                               # (idio run, 2 downloads anti gia 1 — kanena allo cron/kostos)


def download(step):
    from ecmwf.opendata import Client
    client = Client(source="ecmwf")
    target = GRIB % step
    result = client.retrieve(
        type="fc",
        step=step,
        param=["t", "r"],
        levelist=LEVELS_HPA,
        target=target,
    )
    print("step", step, "run  :", result.datetime, "UTC")
    print("step", step, "bytes:", os.path.getsize(target))
    return target, result.datetime + dt.timedelta(hours=step)


def read_grib(path, levels_hpa):
    import xarray as xr
    ds = xr.open_dataset(path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    t_k = np.asarray(ds["t"].values, dtype=np.float64)
    rh = np.asarray(ds["r"].values, dtype=np.float64)
    lats = np.asarray(ds["latitude"].values, dtype=np.float64)
    lons = np.asarray(ds["longitude"].values, dtype=np.float64)
    t_k = np.squeeze(t_k)
    rh = np.squeeze(rh)
    assert t_k.shape == (len(levels_hpa), len(lats), len(lons)), \
        "aprosdokito sxima %s vs (%d,%d,%d)" % (t_k.shape, len(levels_hpa), len(lats), len(lons))
    # GR: to cfgrib mporei na epistrepsei ta ipsi se DIAFORETIKI seira apo auti
    #     pou zitisame (levelist) — ftiaxnoume tin seira na tairiazei AKRIVOS
    #     me to levels_hpa, alliws to zip() sto process_one tha antistoixisei
    #     lathos T me lathos hPa.
    lvl_coord = np.asarray(ds["isobaricInhPa"].values)
    order = [int(np.where(lvl_coord == h)[0][0]) for h in levels_hpa]
    t_k = t_k[order]
    rh = rh[order]
    return t_k - 273.15, rh, lats, lons


def process_one(step):
    target, valid = download(step)
    t_stack, rh_stack, lats, lons = read_grib(target, LEVELS_HPA)
    print("plegma:", t_stack.shape, "| lat", lats[0], "->", lats[-1],
          "| lon", lons[0], "->", lons[-1])

    # GR: #053 fix — AUTO selection: gia KATHE pixel, anamesa sta 4 ipsi,
    #     diali auto me to megalytero RHi anamesa sta SAC-valid. Idia logiki
    #     me to _rhiAt() tou analyze.js, epalitheftike me test_multilevel.py.
    state = sg.ct_state_multilevel(list(t_stack), list(rh_stack), LEVELS_HPA)

    n = state.size
    for k, name in [(0, "tipota"), (1, "vraxyvio"),
                    (2, "monimo"), (3, "monimo+aplonei")]:
        print("  state %d %-16s %6.2f%%" % (k, name, 100.0 * (state == k).sum() / n))

    merc = sg.resample_to_mercator(state, lats, lons, WIDTH, HEIGHT)
    img = Image.fromarray(sg.state_to_rgba(merc), "RGBA")

    os.makedirs(OUTDIR, exist_ok=True)
    stamp = valid.strftime("%Y-%m-%dT%HZ")
    path = os.path.join(OUTDIR, stamp + ".png")
    img.save(path, optimize=True)
    print("\ngrafike:", os.path.relpath(path), os.path.getsize(path) // 1024, "KB")


def main():
    for step in STEPS:
        process_one(step)


if __name__ == "__main__":
    main()
