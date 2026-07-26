#!/usr/bin/env python3
"""
make_conditions.py — VIMA 1 tou #053.  (sti RIZA tou repo smoggy-data)

Katevazei ENA subset apo to ECMWF open data (t + r sta 250 hPa), ypologizei
tin katastasi contrail me tin IDIA fysiki me tin app, kai grafei ENA PNG
se provoli Web Mercator.

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

LEVEL_HPA = 250
WIDTH = HEIGHT = 1440
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "conditions", "%dhPa" % LEVEL_HPA)
GRIB = "/tmp/smoggy_ecmwf.grib2"
STEP = 0                      # VIMA 1: mono to arxiko vima. Meta 0,3,6,...


def download():
    from ecmwf.opendata import Client
    client = Client(source="ecmwf")
    result = client.retrieve(
        type="fc",
        step=STEP,
        param=["t", "r"],
        levelist=[LEVEL_HPA],
        target=GRIB,
    )
    print("run  :", result.datetime, "UTC")
    print("bytes:", os.path.getsize(GRIB))
    return result.datetime + dt.timedelta(hours=STEP)


def read_grib():
    import xarray as xr
    ds = xr.open_dataset(GRIB, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    t_k = np.asarray(ds["t"].values, dtype=np.float64)
    rh = np.asarray(ds["r"].values, dtype=np.float64)
    lats = np.asarray(ds["latitude"].values, dtype=np.float64)
    lons = np.asarray(ds["longitude"].values, dtype=np.float64)
    t_k = np.squeeze(t_k)
    rh = np.squeeze(rh)
    assert t_k.shape == (len(lats), len(lons)), \
        "aprosdokito sxima %s vs (%d,%d)" % (t_k.shape, len(lats), len(lons))
    return t_k - 273.15, rh, lats, lons


def main():
    valid = download()
    t_c, rh, lats, lons = read_grib()
    print("plegma:", t_c.shape, "| lat", lats[0], "->", lats[-1],
          "| lon", lons[0], "->", lons[-1])

    r_ice = sg.rhi(rh, t_c)
    state = sg.ct_state(t_c, LEVEL_HPA, r_ice)

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


if __name__ == "__main__":
    main()
