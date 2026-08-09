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

# EN: Forced cities — ALWAYS included, independent of population (they fall
#     outside the global top-1000). Total stays 1000: (N_CITIES - len(FORCE))
#     dynamic top cities + these 32. Alex 08/08/2026.
# GR: Πολεις που μπαινουν ΠΑΝΤΑ, ανεξαρτητα πληθυσμου (ειναι εκτος του
#     παγκοσμιου top-1000). Συνολο μενει 1000: (N_CITIES - 32) δυναμικες + 32.
FORCE_CITIES = [
    ("Graz",47.07,15.44), ("Thessaloniki",40.64,22.94), ("Reading",51.45,-0.98),
    ("Bologna",44.49,11.34), ("Darmstadt",49.87,8.65), ("Toulouse",43.60,1.44),
    ("Delft",52.01,4.36), ("Zurich",47.38,8.54), ("Oxford",51.75,-1.26),
    ("Cambridge",52.21,0.12), ("Munich",48.14,11.58), ("Helsinki",60.17,24.94),
    ("Innsbruck",47.27,11.40), ("Wageningen",51.97,5.67), ("Amsterdam",52.37,4.90),
    ("Copenhagen",55.68,12.57), ("Stockholm",59.33,18.07), ("Oslo",59.91,10.75),
    ("Dublin",53.35,-6.26), ("Lisbon",38.72,-9.14), ("Brussels",50.85,4.35),
    ("Prague",50.08,14.44), ("Geneva",46.20,6.14), ("Edinburgh",55.95,-3.19),
    ("Florence",43.77,11.26), ("Venice",45.44,12.32), ("Tallinn",59.44,24.75),
    ("Reykjavik",64.15,-21.94), ("Zagreb",45.81,15.98), ("Ljubljana",46.06,14.51),
    ("Bratislava",48.15,17.11), ("Heidelberg",49.40,8.67),
]

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
    picked = list(FORCE_CITIES)   # EN: forced first, always / GR: forced πρωτα, παντα
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
