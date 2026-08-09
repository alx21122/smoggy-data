#!/usr/bin/env python3
"""make_aq.py — AQ snapshot (aristeri stili). GeoNames top-1000 -> Open-Meteo +2meres."""
import os, io, csv, json, zipfile, urllib.request, urllib.parse
import math, time, datetime as dt

HERE   = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "aq-forecast")

GEONAMES = "https://download.geonames.org/export/dump/cities15000.zip"
N_CITIES = 1000          # top-N kata plithysmo
DEDUP_KM = 20.0          # poleis pio konta apo afto = plenazousa
CHUNK    = 100           # topothesies ana Open-Meteo request
SLEEP    = 1.0           # pausi (orio 600/lepto)

FIELDS = ["european_aqi","pm2_5","pm10","ozone","nitrogen_dioxide",
          "sulphur_dioxide","carbon_monoxide","aerosol_optical_depth","dust",
          "alder_pollen","birch_pollen","grass_pollen","ragweed_pollen"]

AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

def _urlopen_retry(url, timeout, tries=4, pause=5):
    last = None
    for k in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=timeout).read()
        except Exception as e:
            last = e
            print("  retry %d/%d: %s" % (k + 1, tries, e))
            time.sleep(pause)
    raise last


def haversine(a_lat, a_lon, b_lat, b_lon):
    R = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat); dl = math.radians(b_lon - a_lon)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

def load_cities():
    raw = _urlopen_retry(GEONAMES, 120)
    zf  = zipfile.ZipFile(io.BytesIO(raw))
    txt = zf.read("cities15000.txt").decode("utf-8")
    rows = []
    for c in csv.reader(io.StringIO(txt), delimiter="\t"):
        try:                       # GeoNames: 2=asciiname, 4=lat, 5=lon, 14=population
            name = c[2]; lat = float(c[4]); lon = float(c[5]); pop = int(c[14])
        except (IndexError, ValueError):
            continue
        rows.append((pop, name, lat, lon))
    rows.sort(reverse=True)
    picked = []
    for pop, name, lat, lon in rows:
        if len(picked) >= N_CITIES: break
        if any(haversine(lat, lon, p[1], p[2]) < DEDUP_KM for p in picked): continue
        picked.append((name, lat, lon))
    print("poleis:", len(picked))
    return picked

def fetch_chunk(cities):
    qs = urllib.parse.urlencode({
        "latitude":  ",".join("%.4f" % c[1] for c in cities),
        "longitude": ",".join("%.4f" % c[2] for c in cities),
        "hourly": ",".join(FIELDS),
        "forecast_days": 3, "timezone": "UTC",
    })
    raw = _urlopen_retry(AQ_URL + "?" + qs, 45)
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, list) else [data]

def slice_plus2(hourly):
    out = []
    for f in FIELDS:
        arr = (hourly or {}).get(f) or []
        seg = arr[48:72]
        if len(seg) < 24: seg = seg + [None]*(24-len(seg))
        out.append([None if v is None else round(v, 2) for v in seg])
    return out

def main():
    cities = load_cities()
    target = dt.datetime.utcnow().date() + dt.timedelta(days=2)
    out_cities = []
    for i in range(0, len(cities), CHUNK):
        chunk = cities[i:i+CHUNK]
        res = fetch_chunk(chunk)
        for (name, lat, lon), r in zip(chunk, res):
            out_cities.append({"n": name, "lat": round(lat,3), "lon": round(lon,3),
                               "v": slice_plus2(r.get("hourly"))})
        print("  chunk %d/%d ok" % (i//CHUNK+1, (len(cities)+CHUNK-1)//CHUNK))
        time.sleep(SLEEP)
    doc = {"target": target.isoformat(),
           "created": dt.datetime.utcnow().replace(microsecond=0).isoformat()+"Z",
           "fields": FIELDS, "cities": out_cities}
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, target.isoformat()+".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("grafike:", os.path.relpath(path), os.path.getsize(path)//1024, "KB")

if __name__ == "__main__":
    main()
