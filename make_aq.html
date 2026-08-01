<!DOCTYPE html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>make_aq.py</title>
<style>
  body{margin:0;background:#0e1116;color:#e6edf3;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  .bar{padding:12px 14px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0}
  .bar b{color:#44aaff}
  .bar span{color:#8b949e}
  pre{margin:0;padding:14px;white-space:pre;overflow-x:auto;
      font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      color:#e6edf3;-webkit-user-select:text;user-select:text}
</style>
</head>
<body>
<div class="bar"><b>make_aq.py</b> &middot; <span>&#8594; sti riza tou smoggy-data</span></div>
<pre>#!/usr/bin/env python3
&quot;&quot;&quot;make_aq.py — AQ snapshot (aristeri stili). GeoNames top-1000 -&gt; Open-Meteo +2meres.&quot;&quot;&quot;
import os, io, csv, json, zipfile, urllib.request, urllib.parse
import math, time, datetime as dt

HERE   = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, &quot;aq-forecast&quot;)

GEONAMES = &quot;https://download.geonames.org/export/dump/cities15000.zip&quot;
N_CITIES = 1000          # top-N kata plithysmo
DEDUP_KM = 20.0          # poleis pio konta apo afto = plenazousa
CHUNK    = 100           # topothesies ana Open-Meteo request
SLEEP    = 1.0           # pausi (orio 600/lepto)

FIELDS = [&quot;european_aqi&quot;,&quot;pm2_5&quot;,&quot;pm10&quot;,&quot;ozone&quot;,&quot;nitrogen_dioxide&quot;,
          &quot;sulphur_dioxide&quot;,&quot;carbon_monoxide&quot;,&quot;aerosol_optical_depth&quot;,&quot;dust&quot;,
          &quot;alder_pollen&quot;,&quot;birch_pollen&quot;,&quot;grass_pollen&quot;,&quot;ragweed_pollen&quot;]

AQ_URL = &quot;https://air-quality-api.open-meteo.com/v1/air-quality&quot;

def _urlopen_retry(url, timeout, tries=4, pause=5):
    last = None
    for k in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=timeout).read()
        except Exception as e:
            last = e
            print(&quot;  retry %d/%d: %s&quot; % (k + 1, tries, e))
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
    txt = zf.read(&quot;cities15000.txt&quot;).decode(&quot;utf-8&quot;)
    rows = []
    for c in csv.reader(io.StringIO(txt), delimiter=&quot;\t&quot;):
        try:                       # GeoNames: 2=asciiname, 4=lat, 5=lon, 14=population
            name = c[2]; lat = float(c[4]); lon = float(c[5]); pop = int(c[14])
        except (IndexError, ValueError):
            continue
        rows.append((pop, name, lat, lon))
    rows.sort(reverse=True)
    picked = []
    for pop, name, lat, lon in rows:
        if len(picked) &gt;= N_CITIES: break
        if any(haversine(lat, lon, p[1], p[2]) &lt; DEDUP_KM for p in picked): continue
        picked.append((name, lat, lon))
    print(&quot;poleis:&quot;, len(picked))
    return picked

def fetch_chunk(cities):
    qs = urllib.parse.urlencode({
        &quot;latitude&quot;:  &quot;,&quot;.join(&quot;%.4f&quot; % c[1] for c in cities),
        &quot;longitude&quot;: &quot;,&quot;.join(&quot;%.4f&quot; % c[2] for c in cities),
        &quot;hourly&quot;: &quot;,&quot;.join(FIELDS),
        &quot;forecast_days&quot;: 3, &quot;timezone&quot;: &quot;UTC&quot;,
    })
    raw = _urlopen_retry(AQ_URL + &quot;?&quot; + qs, 45)
    data = json.loads(raw.decode(&quot;utf-8&quot;))
    return data if isinstance(data, list) else [data]

def slice_plus2(hourly):
    out = []
    for f in FIELDS:
        arr = (hourly or {}).get(f) or []
        seg = arr[48:72]
        if len(seg) &lt; 24: seg = seg + [None]*(24-len(seg))
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
            out_cities.append({&quot;n&quot;: name, &quot;lat&quot;: round(lat,3), &quot;lon&quot;: round(lon,3),
                               &quot;v&quot;: slice_plus2(r.get(&quot;hourly&quot;))})
        print(&quot;  chunk %d/%d ok&quot; % (i//CHUNK+1, (len(cities)+CHUNK-1)//CHUNK))
        time.sleep(SLEEP)
    doc = {&quot;target&quot;: target.isoformat(),
           &quot;created&quot;: dt.datetime.utcnow().replace(microsecond=0).isoformat()+&quot;Z&quot;,
           &quot;fields&quot;: FIELDS, &quot;cities&quot;: out_cities}
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, target.isoformat()+&quot;.json&quot;)
    with open(path, &quot;w&quot;, encoding=&quot;utf-8&quot;) as f:
        json.dump(doc, f, separators=(&quot;,&quot;, &quot;:&quot;))
    print(&quot;grafike:&quot;, os.path.relpath(path), os.path.getsize(path)//1024, &quot;KB&quot;)

if __name__ == &quot;__main__&quot;:
    main()
</pre>
</body>
</html>
