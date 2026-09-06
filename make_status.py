#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_status.py — SMOGGY DATA HEALTH DASHBOARD
Version 4 · 05/09/26 [Claude, approved by Alex]
Place in the ROOT of the smoggy-data repo.

WHAT IT DOES
------------
One file, three jobs:

  1. SCANS local data (conditions/auto, conditions/forecast, aq-forecast):
     what exists, what is missing, and WHY.

  2. PROBES the third parties (NASA GIBS, Copernicus CAMS, ECMWF open-data)
     asking EXACTLY what the app asks. For every request it records:
     when asked, when answered, how long, what came back, and which
     valid time it refers to.

  3. WRITES the dashboard:
        verification/index.html   <- open on your phone, EN / EL / DE
        verification/status.json  <- machine readable

"HOW DO I KNOW IT IS TELLING THE TRUTH?"
----------------------------------------
Three answers, all built in:

  a) SELF-TEST.  `python3 make_status.py --selftest` builds synthetic
     folders whose correct answer is known in advance, runs the SAME
     scan function used in production, and prints expected vs actual.
     If the scanner ever lies, this goes red.

  b) EVIDENCE ON SCREEN.  Every claim shows the raw fact behind it:
     the exact URL requested, the HTTP code, the byte count, the
     milliseconds, the file counts. Nothing asks to be believed.

  c) NEVER GUESS.  Verified-absent (red) is kept strictly separate from
     could-not-check (grey). A network failure on our side is NEVER
     reported as missing data.

SELF-CONTAINED
--------------
No imports from other repo files. Python standard library only
(no numpy, no pillow). Upload it and it runs.

READ-ONLY IN PRODUCTION
-----------------------
Never touches conditions/, aq-forecast/, the PNGs, or any production
script. Writes ONLY inside verification/.

USAGE
-----
    python3 make_status.py              # full run, with probes
    python3 make_status.py --no-net     # local files only
    python3 make_status.py --days 14    # days shown in the grid
    python3 make_status.py --selftest   # prove the scanner is correct
"""

import os
import sys
import json
import time
import shutil
import tempfile
import datetime as dt
import urllib.request
import urllib.error
from urllib.parse import urlencode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "verification")

SLOTS = [0, 3, 6, 9, 12, 15, 18, 21]
STAMP_FMT = "%Y-%m-%dT%HZ"          # same as make_conditions.py
PRESENT, MISSING, NOTDUE = 1, 0, 2

# which cron is responsible for each slot
SLOT_CRON = {0: "08:40Z", 3: "08:40Z", 6: "14:40Z", 9: "14:40Z",
             12: "20:40Z", 15: "20:40Z", 18: "02:40Z+1", 21: "02:40Z+1"}

# ── AQ: when is the file for day X actually OWED? ────────────────────────
# The file dated X is written by the cron of X-1. Before that cron has had
# its chance, an absent file is NOT a fault.
# PRODUCTION USES THE VERIFIED WORKFLOW TIME BELOW. The None branch is a
# documented FALLBACK only, kept for repos where the cron time is unknown:
#   None    -> owed once X-1 is over, i.e. 00:00Z of X. Never a false alarm,
#              but a failed cron only surfaces ~17h late.
#   "HH:MM" -> the UTC time the AQ workflow runs on X-1 (+10 min margin).
#              Catches a failure the SAME day. <-- what is in use.
AQ_DUE = "06:30"   # VERIFIED 05/09/26 against the two production sources:
#   .github/workflows/aq.yml  -> cron '30 6 * * *'   (runs 06:30 UTC daily)
#   make_aq.py main()         -> target = utcnow().date() + 1 day,
#                                path   = OUTDIR/<target>.json
# So the run on day X-1 at 06:30Z writes the file named X.json. The file
# for day X is therefore owed from 06:40Z of X-1 (+10 min run margin),
# which is exactly what aq_due() computes. If either the cron time or the
# target offset in make_aq.py changes, THIS CONSTANT MUST CHANGE TOO.

TIMEOUT = 12
UA = "smoggy-verify/1.0 (+https://smoggyapp.com)"

# ── EXACTLY the endpoints the app requests ───────────────────────────────
# GUARD: if these change in index.html they MUST change here too, otherwise
# the dashboard verifies something different from what the user sees.
# Source: index.html lines 6038-6058 (CAMS_WMS / _gibs / _VIIRS).
CAMS_WMS = "https://eccharts.ecmwf.int/wms/?token=public"
VIIRS = "VIIRS_NOAA20_CorrectedReflectance_TrueColor"
GIBS_TPL = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/{p}/default/"
            "{d}/GoogleMapsCompatible_Level9/2/1/2.jpg")
ECMWF_IDX = "https://data.ecmwf.int/forecasts/{ymd}/{hh}z/ifs/0p25/oper/"


def has_arg(n):
    return n in sys.argv


def arg_int(n, default, lo, hi):
    if n in sys.argv:
        try:
            return max(lo, min(hi, int(sys.argv[sys.argv.index(n) + 1])))
        except (IndexError, ValueError):
            pass
    return default


def slot3h(when):
    """Same as _slot3h() in index.html -> 'YYYY-MM-DDTHH:00:00Z'."""
    x = when.replace(minute=0, second=0, microsecond=0)
    return x.replace(hour=(x.hour // 3) * 3).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════
# LOCAL SCAN
# ══════════════════════════════════════════════════════════════════════════
def parse_stamps(dirpath):
    """
    Returns (valid stamps, bad filenames, empty files).

    NOTE 05/09/26 [after cross-audit]: a filename ending in .png does NOT
    prove the file is a usable image. This is a CHEAP HEADER CHECK only:
    a zero-byte file, or one lacking the PNG magic bytes, is not counted
    as a present slot.

    WHAT IT DOES NOT CATCH (stated plainly so nobody over-trusts it):
    a TRUNCATED file whose first 8 bytes are a correct PNG signature IS
    still counted as present here. Detecting that needs chunk/CRC
    validation (IHDR ... IEND), which lives in test_smoggy.py where PIL
    is available. This file deliberately stays on the standard library.
    """
    good, bad, empty = [], [], []
    if not os.path.isdir(dirpath):
        return good, bad, empty
    for fn in sorted(os.listdir(dirpath)):
        if not fn.endswith(".png"):
            continue
        try:
            stamp = (dt.datetime.strptime(fn[:-4], STAMP_FMT)
                     .replace(tzinfo=dt.timezone.utc))
        except ValueError:
            bad.append(fn)
            continue
        p = os.path.join(dirpath, fn)
        try:
            if os.path.getsize(p) < 8:
                empty.append(fn)
                continue
            with open(p, "rb") as fh:
                if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                    empty.append(fn)          # not a PNG at all
                    continue
        except OSError:
            empty.append(fn)
            continue
        good.append(stamp)
    return good, bad, empty


def slot_due(day, hour, now):
    """
    Has the time this slot SHOULD exist already passed?
    18Z/21Z are written by the 02:40Z cron of the NEXT day.
    (+10 min margin for the workflow itself to run.)
    """
    v = dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc)
    if hour in (18, 21):
        due = v.replace(hour=2, minute=50) + dt.timedelta(days=1)
    elif hour in (0, 3):
        due = v.replace(hour=8, minute=50)
    elif hour in (6, 9):
        due = v.replace(hour=14, minute=50)
    else:
        due = v.replace(hour=20, minute=50)
    return now >= due


def aq_due(day, now):
    """
    Is the AQ file for `day` already owed?

    FIX 05/09/26 [Finding 1]: this concept did not exist before. The AQ
    table had only present/absent, so the row for TOMORROW -- a file that
    today's cron has not yet had a reason to write -- was reported as
    MISSING every single day. On a perfectly healthy repo the dashboard
    showed AQI 80% and state WARN. That is exactly the "verified-absent vs
    could-not-check" confusion this whole file exists to prevent, and a
    dashboard that cries wolf daily gets ignored within a fortnight.

    The SMI grid already had this rule as slot_due(). AQ now has it too.
    """
    if AQ_DUE is None:
        due = dt.datetime(day.year, day.month, day.day,
                          tzinfo=dt.timezone.utc)
    else:
        hh, mm = (int(x) for x in AQ_DUE.split(":"))
        prev = day - dt.timedelta(days=1)
        due = (dt.datetime(prev.year, prev.month, prev.day, hh, mm,
                           tzinfo=dt.timezone.utc) + dt.timedelta(minutes=10))
    return now >= due


def scan_local(now, ndays, base=None):
    """`base` is a parameter so --selftest can point it at synthetic folders."""
    base = base or BASE_DIR
    auto_dir = os.path.join(base, "conditions", "auto")
    fc_dir = os.path.join(base, "conditions", "forecast")
    aq_dir = os.path.join(base, "aq-forecast")

    auto, auto_bad, auto_empty = parse_stamps(auto_dir)
    fc, fc_bad, fc_empty = parse_stamps(fc_dir)
    have = set((d.date(), d.hour) for d in auto)

    grid, miss_by_hour = [], {h: 0 for h in SLOTS}
    day = now.date() - dt.timedelta(days=ndays - 1)
    while day <= now.date():
        cells, npres, nmiss = [], 0, 0
        for h in SLOTS:
            if (day, h) in have:
                cells.append(PRESENT)
                npres += 1
            elif not slot_due(day, h, now):
                cells.append(NOTDUE)
            else:
                cells.append(MISSING)
                miss_by_hour[h] += 1
                nmiss += 1
        grid.append({"date": day.isoformat(), "label": day.strftime("%d/%m"),
                     "cells": cells, "present": npres, "missing_due": nmiss})
        day += dt.timedelta(days=1)

    due = sum(1 for r in grid for c in r["cells"] if c != NOTDUE)
    pres = sum(r["present"] for r in grid)
    cov = round(100.0 * pres / due, 1) if due else 0.0
    worst = (max(miss_by_hour, key=lambda k: miss_by_hour[k])
             if miss_by_hour and max(miss_by_hour.values()) >= 3 else None)

    newest = max(auto) if auto else None
    age = round((now - newest).total_seconds() / 3600.0, 1) if newest else None

    fdays = sorted(set(d.date() for d in fc))
    fhours = sorted(d.hour for d in fc)
    # FIX 05/09/26 [Claude, after cross-audit] #FCCOMPLETE: WAS
    #   "complete": (fhours == SLOTS)
    # which was WRONG for files spread over MORE THAN ONE day: e.g. day A
    # holding 00,03,06,09 and day B holding 12,15,18,21 gives a sorted hour
    # list equal to SLOTS, so 'complete' came out True for a forecast set
    # that is not one complete day at all. Now all three must hold:
    #   exactly ONE day  ·  exactly 8 files  ·  8 DISTINCT hours == SLOTS
    # (the set() also rules out duplicates of the same hour.)
    fc_complete = (len(fdays) == 1 and len(fc) == 8
                   and sorted(set(fhours)) == SLOTS)
    fc_info = {"date": fdays[0].isoformat() if len(fdays) == 1 else None,
               "slots": len(fc), "complete": fc_complete,
               "days_spanned": len(fdays),
               "duplicate_hours": len(fhours) - len(set(fhours)),
               "is_tomorrow": (len(fdays) == 1 and
                               fdays[0] == (now + dt.timedelta(days=1)).date()),
               "bad_names": fc_bad, "corrupt": fc_empty}

    # AQ: ONE file per day holding 24 hourly values. File X is made on X-1.
    aq_dates = set()
    if os.path.isdir(aq_dir):
        for f in sorted(os.listdir(aq_dir)):
            if f.endswith(".json"):
                try:
                    aq_dates.add(dt.datetime.strptime(f[:-5], "%Y-%m-%d").date())
                except ValueError:
                    pass
    aq_rows = []
    day = now.date() - dt.timedelta(days=ndays - 1)
    end = now.date() + dt.timedelta(days=1)
    while day <= end:
        owed = aq_due(day, now)
        r = {"date": day.isoformat(), "label": day.strftime("%d/%m"),
             "made_on": (day - dt.timedelta(days=1)).strftime("%d/%m"),
             "present": day in aq_dates, "due": owed,
             "cities": None, "hours": None, "note": ""}
        if r["present"]:
            try:
                with open(os.path.join(aq_dir, day.isoformat() + ".json")) as fh:
                    j = json.load(fh)
                cities = j.get("cities", [])
                r["cities"] = len(cities)
                hrs = None
                if cities and isinstance(cities[0].get("v"), list) and cities[0]["v"]:
                    f0 = cities[0]["v"][0]
                    hrs = len(f0) if isinstance(f0, list) else None
                r["hours"] = hrs
                if not cities:
                    r["note"] = "no_cities"
                elif hrs != 24:
                    r["note"] = "not_24h"
            except Exception as ex:
                r["note"] = type(ex).__name__
        else:
            # never call something missing that was never owed
            r["note"] = "absent" if owed else "not_due"
        aq_rows.append(r)
        day += dt.timedelta(days=1)
    # coverage is measured against what was OWED, exactly as the SMI grid
    # measures against slots that are due (NOTDUE cells are excluded there).
    aq_owed = [r for r in aq_rows if r["due"]]
    aq_ok = sum(1 for r in aq_owed if r["present"] and not r["note"])
    aq_cov = round(100.0 * aq_ok / len(aq_owed), 1) if aq_owed else 0.0

    fstat = None
    sp = os.path.join(base, ".forecast_status")
    if os.path.isfile(sp):
        try:
            with open(sp) as fh:
                fstat = fh.read().strip()
        except Exception:
            pass

    return {"smi": {"coverage_pct": cov, "days": ndays, "grid": grid,
                    "slots": SLOTS, "missing_by_hour": miss_by_hour,
                    "worst_hour": worst, "slot_cron": SLOT_CRON,
                    "newest": newest.strftime(STAMP_FMT) if newest else None,
                    "age_hours": age, "files": len(auto),
                    "bad_names": auto_bad, "corrupt": auto_empty},
            "forecast": fc_info, "forecast_status": fstat,
            "aq": {"coverage_pct": aq_cov, "rows": aq_rows}}


# ══════════════════════════════════════════════════════════════════════════
# PROBES
# ══════════════════════════════════════════════════════════════════════════
def fetch(url, method="GET"):
    """Returns REAL timings. Never raises."""
    t0 = time.time()
    out = {"url": url, "method": method,
           "asked_at": dt.datetime.now(dt.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
           "got_at": None, "ms": None, "http": 0, "bytes": 0,
           "server_date": None, "last_modified": None, "error": None}
    try:
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(65536) if method == "GET" else b""
            out["http"] = r.status
            out["bytes"] = len(body)
            out["server_date"] = r.headers.get("Date")
            out["last_modified"] = r.headers.get("Last-Modified")
    except urllib.error.HTTPError as ex:
        out["http"] = ex.code
        out["error"] = "HTTP %d" % ex.code
        try:
            out["server_date"] = ex.headers.get("Date")
        except Exception:
            pass
    except Exception as ex:
        out["error"] = "%s: %s" % (type(ex).__name__, str(ex)[:120])
    out["ms"] = int((time.time() - t0) * 1000)
    out["got_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def classify(r, head=False):
    """
    CRITICAL: "could not check" is NOT "does not exist". Confusing the two
    produces false alarms, and a dashboard that cries wolf gets ignored.

      hit         -> 200 with body (or 2xx/3xx for HEAD) : verified present
      absent      -> 404, or 200 with empty body         : verified MISSING
      blocked     -> 401/403/407/429                     : could not check
      upstream    -> 5xx                                 : could not check
      unreachable -> no response at all                  : could not check
    """
    h = r.get("http", 0)
    if head and h in (200, 301, 302):
        return "hit"
    if (not head) and h == 200 and r.get("bytes", 0) > 0:
        return "hit"
    if h == 0:
        return "unreachable"
    if h in (401, 403, 407, 429):
        return "blocked"
    if 500 <= h < 600:
        return "upstream"
    return "absent"


def _result(kind, newest, i, tries):
    if kind == "hit":
        return {"status": "ok" if i == 0 else "stale", "newest": newest,
                "lag_slots": i, "tries": tries}
    return {"status": kind, "newest": None, "lag_slots": None, "tries": tries}


def cams_url(layer, time_iso):
    """GetMap with the same layer/format/version L.tileLayer.wms sends.
    2x2 pixels: we test availability, we do not download a map."""
    q = urlencode({"service": "WMS", "request": "GetMap", "version": "1.3.0",
                   "layers": layer, "styles": "", "crs": "EPSG:3857",
                   "bbox": "-20037508,-20037508,20037508,20037508",
                   "width": "2", "height": "2", "format": "image/png",
                   "transparent": "true", "time": time_iso})
    return CAMS_WMS + "&" + q


def _walk(candidates, make_url, head=False):
    """Try newest first; stop at the first definite answer."""
    tries = []
    for i, (key, url) in enumerate(candidates):
        r = fetch(url, method="HEAD" if head else "GET")
        r["refers_to"] = key
        k = classify(r, head)
        r["verdict"] = k
        tries.append(r)
        if k == "hit":
            return _result("hit", key, i, tries)
        if k != "absent":
            return _result(k, None, i, tries)   # could not check: stop, don't guess
    return _result("absent", None, None, tries)


def probe_cams(layer, now, back=4):
    c = []
    for i in range(back):
        t = slot3h(now - dt.timedelta(hours=3 * i))
        c.append((t, cams_url(layer, t)))
    return _walk(c, None)


def probe_gibs(now, back=3):
    c = []
    for i in range(back):
        d = (now - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        c.append((d, GIBS_TPL.format(p=VIIRS, d=d)))
    return _walk(c, None)


def probe_ecmwf(now, back=4):
    """Answers definitively WHY 00Z/03Z are missing: if the 00z run is not
    published yet at 08:40, our cron is not at fault, the upstream is late."""
    c = []
    for i in range(back):
        t = now - dt.timedelta(hours=6 * i)
        hh = (t.hour // 6) * 6
        c.append((t.strftime("%Y-%m-%d") + " %02dz" % hh,
                  ECMWF_IDX.format(ymd=t.strftime("%Y%m%d"), hh="%02d" % hh)))
    return _walk(c, None, head=True)


def run_probes(now, enabled):
    if not enabled:
        return {"enabled": False, "layers": {}}
    L = {}
    jobs = (("clouds", lambda: probe_gibs(now)),
            ("aod", lambda: probe_cams("composition_aod550", now)),
            ("dust", lambda: probe_cams("composition_duaod550", now)),
            ("ecmwf", lambda: probe_ecmwf(now)))
    for key, fn in jobs:
        try:
            L[key] = fn()
        except Exception as ex:
            L[key] = {"status": "error", "newest": None, "tries": [],
                      "error": "%s: %s" % (type(ex).__name__, str(ex)[:100])}
    return {"enabled": True, "layers": L}


# ══════════════════════════════════════════════════════════════════════════
# VERDICT  (codes only — the HTML translates them)
# ══════════════════════════════════════════════════════════════════════════
def verdict(S):
    fails, warns = [], []
    smi, fc, aq = S["smi"], S["forecast"], S["aq"]

    empty = [r["label"] for r in smi["grid"]
             if r["present"] == 0 and r["missing_due"] > 0]
    if empty:
        fails.append({"c": "empty_days", "n": len(empty), "v": ", ".join(empty)})
    if not smi["files"]:
        fails.append({"c": "no_smi"})
    if smi["bad_names"]:
        fails.append({"c": "bad_names", "n": len(smi["bad_names"])})
    if smi.get("corrupt"):
        fails.append({"c": "corrupt", "n": len(smi["corrupt"])})
    a = smi["age_hours"]
    if a is not None and a > 24:
        fails.append({"c": "stale_data", "v": "%.1f" % a})
    elif a is not None and a > 12:
        warns.append({"c": "aging_data", "v": "%.1f" % a})

    # FIX 05/09/26 [cross-audit finding 4]: fc["bad_names"]/fc["corrupt"] were
    # collected by parse_stamps() but NEVER reported. A forecast folder full of
    # junk or 0-byte files could pass as healthy as long as 8 valid ones also
    # existed. Same rule as auto/: junk is a FAIL, not a silent shrug.
    if fc.get("bad_names"):
        fails.append({"c": "forecast_bad_names", "n": len(fc["bad_names"])})
    if fc.get("corrupt"):
        fails.append({"c": "forecast_corrupt", "n": len(fc["corrupt"])})

    if not fc["slots"]:
        warns.append({"c": "no_forecast"})
    elif not fc["complete"]:
        fails.append({"c": "forecast_partial", "n": fc["slots"]})
    elif not fc["is_tomorrow"]:
        # ESCALATED 05/09/26 [3rd cross-audit, ok Alex]: WAS a warn. The app
        # requests forecast tiles by the COMPUTED expected date (COND_BASE_FC
        # + that date), never "whatever is in the folder" -- so a wrong-day
        # forecast does not show silently-wrong data to the user, it 404s and
        # shows "not available" (verified against index.html COND_BASE_FC).
        # Even so, for MONITORING this is the same failure class as
        # forecast_partial: the pipeline did not produce a usable tomorrow.
        # Treated inconsistently before; now both are fail.
        fails.append({"c": "forecast_wrong_day", "v": fc["date"]})

    if smi["worst_hour"] is not None:
        wh = smi["worst_hour"]
        warns.append({"c": "slot_pattern", "v": "%02dZ" % wh,
                      "n": smi["missing_by_hour"][wh], "v2": SLOT_CRON[wh]})
    # only OWED files can be "missing" (see aq_due)
    miss = [r["label"] for r in aq["rows"]
            if r.get("due", True) and not r["present"]]
    if miss:
        warns.append({"c": "aq_missing", "v": ", ".join(miss)})
    # FIX 05/09/26 [cross-audit finding 5]: a file that EXISTS but is corrupt
    # (JSONDecodeError / no cities / not 24h) had present=True, so it fell out
    # of `miss` and was reported NOWHERE -- a repo with a broken AQ file came
    # back state "ok". Verified silently-broken is worse than absent: it is a
    # FAIL, and it names the day.
    aq_bad = [r["label"] for r in aq["rows"]
              if r.get("due", True) and r["present"]
              and r.get("note") not in ("", "not_due")]
    if aq_bad:
        fails.append({"c": "aq_invalid", "v": ", ".join(aq_bad)})
    if S.get("forecast_status") and S["forecast_status"] != "success":
        warns.append({"c": "forecast_status", "v": S["forecast_status"]})

    P = S.get("probes", {})
    if P.get("enabled"):
        for k, v in P.get("layers", {}).items():
            st = v.get("status")
            if st == "absent":
                fails.append({"c": "layer_absent", "v": k})
            elif st in ("blocked", "unreachable", "upstream", "error"):
                warns.append({"c": "layer_uncheckable", "v": k, "v2": st})
            elif st == "stale":
                warns.append({"c": "layer_stale", "v": k,
                              "n": v.get("lag_slots") or 0})
    return ("fail" if fails else ("warn" if warns else "ok")), fails, warns


# ══════════════════════════════════════════════════════════════════════════
# SELF-TEST — proves the scanner reports reality
# ══════════════════════════════════════════════════════════════════════════
PNG8 = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32   # correct SIGNATURE, not a
#                                              decodable image -- enough for
#                                              the header check this file does


def _mk(base, autos=(), fcs=(), aqs=(), status=None):
    for sub in ("conditions/auto", "conditions/forecast", "aq-forecast"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    for s in autos:
        open(os.path.join(base, "conditions/auto", s + ".png"), "wb").write(PNG8)
    for s in fcs:
        open(os.path.join(base, "conditions/forecast", s + ".png"), "wb").write(PNG8)
    doc = {"target": "t", "fields": ["european_aqi"],
           "cities": [{"n": "Graz", "v": [[10] * 24]}]}
    for d in aqs:
        json.dump(doc, open(os.path.join(base, "aq-forecast", d + ".json"), "w"))
    if status:
        open(os.path.join(base, ".forecast_status"), "w").write(status)


def _v(S):
    """verdict() needs probes present; selftest scans have none."""
    S = dict(S)
    S.setdefault("probes", {"enabled": False, "layers": {}})
    return verdict(S)


def selftest():
    """
    Builds synthetic folders whose correct answer is known IN ADVANCE, runs
    the SAME scan_local() used in production, and compares. If the scanner
    ever misreports, this goes red.
    """
    NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc)
    D0 = "2026-09-05"      # today
    D1 = "2026-09-04"      # yesterday
    D2 = "2026-09-06"      # tomorrow
    Dm1 = "2026-09-03"     # two days back (still owed when ndays=3)
    cases, results = [], []

    def case(name, build, checks, when=None):
        cases.append((name, build, checks, when or NOW))

    # 1. empty repo -> nothing claimed
    case("empty repo",
         lambda b: _mk(b),
         [("files", lambda S: S["smi"]["files"], 0),
          ("newest", lambda S: S["smi"]["newest"], None),
          ("age", lambda S: S["smi"]["age_hours"], None)])

    # 2. yesterday complete 8/8, all slots due -> 8/8 for that day
    case("full day counted as 8/8",
         lambda b: _mk(b, autos=["%sT%02dZ" % (D1, h) for h in SLOTS]),
         [("day present", lambda S: S["smi"]["grid"][-2]["present"], 8),
          ("day missing", lambda S: S["smi"]["grid"][-2]["missing_due"], 0)])

    # 3. at 12:00Z today only 00Z/03Z (due 08:50) and 06Z/09Z (due 14:50) matter.
    #    Everything from 12Z on is NOT DUE -> must never be flagged as missing.
    case("today: future slots are NOT DUE, never missing",
         lambda b: _mk(b, autos=["%sT%02dZ" % (D0, h) for h in (0, 3, 6, 9)]),
         [("12Z cell", lambda S: S["smi"]["grid"][-1]["cells"][4], NOTDUE),
          ("15Z cell", lambda S: S["smi"]["grid"][-1]["cells"][5], NOTDUE),
          ("18Z cell", lambda S: S["smi"]["grid"][-1]["cells"][6], NOTDUE),
          ("21Z cell", lambda S: S["smi"]["grid"][-1]["cells"][7], NOTDUE),
          ("present", lambda S: S["smi"]["grid"][-1]["present"], 4),
          ("no false alarm", lambda S: S["smi"]["grid"][-1]["missing_due"], 0)])

    # 3b. same day, but at 23:00Z: now 00..15Z ARE due -> real gaps must show
    case("today at 23:00Z: due slots missing ARE flagged",
         lambda b: _mk(b, autos=["%sT%02dZ" % (D0, h) for h in (0, 3)]),
         [("06Z missing", lambda S: S["smi"]["grid"][-1]["cells"][2], MISSING),
          ("12Z missing", lambda S: S["smi"]["grid"][-1]["cells"][4], MISSING),
          ("18Z still not due", lambda S: S["smi"]["grid"][-1]["cells"][6], NOTDUE),
          ("counted missing", lambda S: S["smi"]["grid"][-1]["missing_due"], 4)],
         when=dt.datetime(2026, 9, 5, 23, 0, tzinfo=dt.timezone.utc))

    # 4. yesterday 18Z/21Z ARE due (02:50 today has passed)
    case("yesterday 18Z/21Z ARE due",
         lambda b: _mk(b, autos=["%sT%02dZ" % (D1, h) for h in (0, 3)]),
         [("18Z cell", lambda S: S["smi"]["grid"][-2]["cells"][6], MISSING),
          ("day missing", lambda S: S["smi"]["grid"][-2]["missing_due"], 6)])

    # 5. invalid filename is reported, not silently ignored
    case("invalid filename detected",
         lambda b: _mk(b, autos=["NOT-A-DATE", "%sT00Z" % D1]),
         [("bad count", lambda S: len(S["smi"]["bad_names"]), 1),
          ("good count", lambda S: S["smi"]["files"], 1)])

    # 6. forecast must be tomorrow AND complete
    case("forecast complete for tomorrow",
         lambda b: _mk(b, fcs=["%sT%02dZ" % (D2, h) for h in SLOTS],
                       status="success"),
         [("slots", lambda S: S["forecast"]["slots"], 8),
          ("complete", lambda S: S["forecast"]["complete"], True),
          ("is tomorrow", lambda S: S["forecast"]["is_tomorrow"], True),
          ("status", lambda S: S["forecast_status"], "success")])

    # 7. incomplete forecast is caught
    case("incomplete forecast caught",
         lambda b: _mk(b, fcs=["%sT%02dZ" % (D2, h) for h in (0, 3, 6)]),
         [("slots", lambda S: S["forecast"]["slots"], 3),
          ("complete", lambda S: S["forecast"]["complete"], False)])

    # 8. AQ valid file parsed correctly
    case("AQ file parsed (1 city, 24h)",
         lambda b: _mk(b, aqs=[D0]),
         [("present", lambda S: [r for r in S["aq"]["rows"]
                                 if r["date"] == D0][0]["present"], True),
          ("cities", lambda S: [r for r in S["aq"]["rows"]
                                if r["date"] == D0][0]["cities"], 1),
          ("hours", lambda S: [r for r in S["aq"]["rows"]
                               if r["date"] == D0][0]["hours"], 24),
          ("note", lambda S: [r for r in S["aq"]["rows"]
                              if r["date"] == D0][0]["note"], "")])

    # 9. AQ missing day flagged
    case("AQ missing day flagged",
         lambda b: _mk(b),
         [("note", lambda S: [r for r in S["aq"]["rows"]
                              if r["date"] == D0][0]["note"], "absent")])

    # 10. corrupt AQ JSON does not crash and is flagged
    def _corrupt(b):
        _mk(b)
        open(os.path.join(b, "aq-forecast", D0 + ".json"), "w").write("{oops")
    case("corrupt AQ JSON handled",
         _corrupt,
         [("present", lambda S: [r for r in S["aq"]["rows"]
                                 if r["date"] == D0][0]["present"], True),
          ("flagged", lambda S: [r for r in S["aq"]["rows"]
                                 if r["date"] == D0][0]["note"] != "", True)])

    # ── added 05/09/26 after cross-audit: the four gaps the review named ──

    # 12. forecast spread over TWO days must NOT count as complete
    #     (this was a REAL bug: sorted hours summed to SLOTS across days)
    case("forecast split across 2 days is NOT complete",
         lambda b: _mk(b, fcs=["2026-09-06T%02dZ" % h for h in (0, 3, 6, 9)] +
                              ["2026-09-07T%02dZ" % h for h in (12, 15, 18, 21)]),
         [("slots", lambda S: S["forecast"]["slots"], 8),
          ("days spanned", lambda S: S["forecast"]["days_spanned"], 2),
          ("complete", lambda S: S["forecast"]["complete"], False),
          ("is tomorrow", lambda S: S["forecast"]["is_tomorrow"], False)])

    # 13. duplicate hours must NOT count as complete
    case("duplicate hours are NOT complete",
         lambda b: _mk(b, fcs=["%sT%02dZ" % (D2, h)
                               for h in (0, 3, 6, 9, 12, 15, 18)]),
         [("slots", lambda S: S["forecast"]["slots"], 7),
          ("complete", lambda S: S["forecast"]["complete"], False)])

    # 14. a 0-byte PNG is NOT a present slot
    def _zero(b):
        _mk(b, autos=["%sT00Z" % D1])
        open(os.path.join(b, "conditions/auto", "%sT03Z.png" % D1), "wb").write(b"")
    case("0-byte PNG counted as corrupt, not present",
         _zero,
         [("files", lambda S: S["smi"]["files"], 1),
          ("corrupt", lambda S: len(S["smi"]["corrupt"]), 1),
          ("03Z not present", lambda S: S["smi"]["grid"][-2]["cells"][1], MISSING)])

    # 15. a file that is not a PNG at all is corrupt, not present
    def _fake(b):
        _mk(b)
        open(os.path.join(b, "conditions/auto", "%sT00Z.png" % D1),
             "wb").write(b"this is not a png")
    case("non-PNG content counted as corrupt",
         _fake,
         [("files", lambda S: S["smi"]["files"], 0),
          ("corrupt", lambda S: len(S["smi"]["corrupt"]), 1)])

    # 16-19. slot_due BOUNDARIES: one minute before vs one minute after.
    #        These are the rules I invented from the cron schedule -- the
    #        part most likely to be wrong, so it is pinned explicitly.
    case("00Z: 1 min BEFORE due -> not due",
         lambda b: _mk(b),
         [("cell", lambda S: S["smi"]["grid"][-1]["cells"][0], NOTDUE)],
         when=dt.datetime(2026, 9, 5, 8, 49, tzinfo=dt.timezone.utc))
    case("00Z: 1 min AFTER due -> missing",
         lambda b: _mk(b),
         [("cell", lambda S: S["smi"]["grid"][-1]["cells"][0], MISSING)],
         when=dt.datetime(2026, 9, 5, 8, 51, tzinfo=dt.timezone.utc))
    case("18Z: 1 min BEFORE next-day cron -> not due",
         lambda b: _mk(b),
         [("cell", lambda S: S["smi"]["grid"][-2]["cells"][6], NOTDUE)],
         when=dt.datetime(2026, 9, 5, 2, 49, tzinfo=dt.timezone.utc))
    case("18Z: 1 min AFTER next-day cron -> missing",
         lambda b: _mk(b),
         [("cell", lambda S: S["smi"]["grid"][-2]["cells"][6], MISSING)],
         when=dt.datetime(2026, 9, 5, 2, 51, tzinfo=dt.timezone.utc))

    # ── added 05/09/26 [Finding 1]: AQ "not due yet" ──────────────────────
    # Before this, the row for TOMORROW was reported MISSING every day, so a
    # perfectly healthy repo showed AQI 80% / WARN. These pin the fix.
    def _row(S, d):
        return [r for r in S["aq"]["rows"] if r["date"] == d][0]

    # 20. UPDATED 05/09/26 [2nd cross-audit]: AQ_DUE is now the REAL cron time
    #     ("06:30" from aq.yml), not None. Consequence: the file for day X is
    #     owed from 06:40Z of X-1, so at 12:00Z on D0 TOMORROW's file is
    #     already owed -- a failure is caught the SAME day instead of ~17h
    #     later. These expectations were rewritten for that semantics; they
    #     previously encoded the AQ_DUE=None assumption.
    case("AQ tomorrow IS owed once today's cron has run",
         lambda b: _mk(b, aqs=[Dm1, D1, D0, D2]),
         [("due", lambda S: _row(S, D2)["due"], True),
          ("note", lambda S: _row(S, D2)["note"], ""),
          ("healthy repo reads 100%", lambda S: S["aq"]["coverage_pct"], 100.0)])

    # 21. a day that IS owed and absent must still go red
    case("AQ owed-and-absent IS still flagged",
         lambda b: _mk(b, aqs=[Dm1, D1, D2]),
         [("due", lambda S: _row(S, D0)["due"], True),
          ("note", lambda S: _row(S, D0)["note"], "absent"),
          ("coverage drops", lambda S: S["aq"]["coverage_pct"], 75.0)])

    # 22-23. BOUNDARY of aq_due() with the real cron: owed at 06:40Z of X-1.
    case("AQ 1 min BEFORE its cron -> not due",
         lambda b: _mk(b),
         [("due", lambda S: _row(S, D0)["due"], False),
          ("note", lambda S: _row(S, D0)["note"], "not_due")],
         when=dt.datetime(2026, 9, 4, 6, 39, tzinfo=dt.timezone.utc))
    case("AQ 1 min AFTER its cron -> due and absent",
         lambda b: _mk(b),
         [("due", lambda S: _row(S, D0)["due"], True),
          ("note", lambda S: _row(S, D0)["note"], "absent")],
         when=dt.datetime(2026, 9, 4, 6, 41, tzinfo=dt.timezone.utc))

    # 24. verdict() must stay silent about a file that was never owed.
    #     At 06:00Z on D0, tomorrow's cron (06:40Z) has NOT run yet.
    case("verdict stays silent about a not-yet-owed file",
         lambda b: _mk(b, aqs=[Dm1, D1, D0]),
         [("no aq_missing", lambda S: [m for m in _v(S)[2]
                                       if m["c"] == "aq_missing"], [])],
         when=dt.datetime(2026, 9, 5, 6, 0, tzinfo=dt.timezone.utc))

    # ── added 05/09/26 after 2nd cross-audit ─────────────────────────────

    # 25. junk in forecast/ must FAIL, even when 8 valid files also exist
    def _fcjunk(b):
        _mk(b, fcs=["%sT%02dZ" % (D2, h) for h in SLOTS])
        open(os.path.join(b, "conditions/forecast", "GARBAGE.png"),
             "wb").write(PNG8)
        open(os.path.join(b, "conditions/forecast", "%sT02Z.png" % D2),
             "wb").write(b"")
    case("junk in forecast/ is reported",
         _fcjunk,
         [("valid slots", lambda S: S["forecast"]["slots"], 8),
          ("complete", lambda S: S["forecast"]["complete"], True),
          ("bad names", lambda S: len(S["forecast"]["bad_names"]), 1),
          ("corrupt", lambda S: len(S["forecast"]["corrupt"]), 1),
          ("verdict fails", lambda S: sorted(
              x["c"] for x in _v(S)[1]
              if x["c"].startswith("forecast_")),
           ["forecast_bad_names", "forecast_corrupt"])])

    # 26. an AQ file that EXISTS but is corrupt must FAIL and name the day
    def _aqbad(b):
        _mk(b, aqs=[D1])
        open(os.path.join(b, "aq-forecast", D0 + ".json"), "w").write("{broken")
    def _aqbad_clean(b):
        # every OTHER owed AQ day present and valid, so the ONLY aq finding
        # can be the corrupt one -- otherwise the case proves less than it
        # claims (review point, 4th cross-audit).
        _mk(b, aqs=[Dm1, D1, D2])
        open(os.path.join(b, "aq-forecast", D0 + ".json"), "w").write("{broken")
    case("corrupt AQ file is a FAIL, not silence",
         _aqbad_clean,
         [("present", lambda S: _row(S, D0)["present"], True),
          ("noted", lambda S: _row(S, D0)["note"] != "", True),
          ("verdict names it", lambda S: any(
              x["c"] == "aq_invalid" and "05/09" in x["v"] for x in _v(S)[1]),
           True),
          ("no aq_missing noise", lambda S: [m for m in _v(S)[2]
                                             if m["c"] == "aq_missing"], []),
          ("aq_invalid is the ONLY aq fail", lambda S: [
              x["c"] for x in _v(S)[1] if x["c"].startswith("aq_")],
           ["aq_invalid"])])

    # 27. AQ_DUE is now the real cron time, so a file owed since 06:40Z
    #     on the previous day is caught the SAME day, not 17h later
    case("AQ owed from 06:40Z of the previous day",
         lambda b: _mk(b),
         [("due at 06:39", lambda S: _row(S, D0)["due"], False)],
         when=dt.datetime(2026, 9, 4, 6, 39, tzinfo=dt.timezone.utc))
    case("AQ owed once 06:40Z has passed",
         lambda b: _mk(b),
         [("due at 06:41", lambda S: _row(S, D0)["due"], True)],
         when=dt.datetime(2026, 9, 4, 6, 41, tzinfo=dt.timezone.utc))

    # 28. remaining slot_due boundaries (03Z/06Z/09Z/12Z/15Z/21Z) so the
    #     invented cron rules are pinned COMPLETELY, not just for 00Z/18Z
    for hh, (before, after, idx) in {
            3:  ((8, 49), (8, 51), 1),
            6:  ((14, 49), (14, 51), 2),
            9:  ((14, 49), (14, 51), 3),
            12: ((20, 49), (20, 51), 4),
            15: ((20, 49), (20, 51), 5)}.items():
        case("%02dZ: before cron -> not due" % hh, lambda b: _mk(b),
             [("cell", (lambda i: lambda S: S["smi"]["grid"][-1]["cells"][i])(idx),
               NOTDUE)],
             when=dt.datetime(2026, 9, 5, before[0], before[1],
                              tzinfo=dt.timezone.utc))
        case("%02dZ: after cron -> missing" % hh, lambda b: _mk(b),
             [("cell", (lambda i: lambda S: S["smi"]["grid"][-1]["cells"][i])(idx),
               MISSING)],
             when=dt.datetime(2026, 9, 5, after[0], after[1],
                              tzinfo=dt.timezone.utc))
    case("21Z: before next-day cron -> not due", lambda b: _mk(b),
         [("cell", lambda S: S["smi"]["grid"][-2]["cells"][7], NOTDUE)],
         when=dt.datetime(2026, 9, 5, 2, 49, tzinfo=dt.timezone.utc))
    case("21Z: after next-day cron -> missing", lambda b: _mk(b),
         [("cell", lambda S: S["smi"]["grid"][-2]["cells"][7], MISSING)],
         when=dt.datetime(2026, 9, 5, 2, 51, tzinfo=dt.timezone.utc))

    # 29. ESCALATED 05/09/26 [3rd cross-audit]: complete-but-wrong-day
    #     forecast must be a FAIL, same class as forecast_partial -- both
    #     mean "tomorrow's forecast pipeline did not do its job", even
    #     though the app itself only 404s rather than showing wrong data.
    case("complete forecast for WRONG day is a FAIL",
         lambda b: _mk(b, fcs=["%sT%02dZ" % (D0, h) for h in SLOTS]),
         [("complete", lambda S: S["forecast"]["complete"], True),
          ("is_tomorrow", lambda S: S["forecast"]["is_tomorrow"], False),
          ("verdict", lambda S: _v(S)[0], "fail"),
          ("code", lambda S: [x["c"] for x in _v(S)[1]
                              if x["c"] == "forecast_wrong_day"],
           ["forecast_wrong_day"])])

    print("=" * 70)
    print("SELF-TEST — synthetic folders with known correct answers")
    print("fixed clock: %s" % NOW.strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 70)

    npass = nfail = 0
    for name, build, checks, when in cases:
        tmp = tempfile.mkdtemp(prefix="smoggy_selftest_")
        try:
            build(tmp)
            S = scan_local(when, 3, base=tmp)
            for label, get, want in checks:
                try:
                    got = get(S)
                except Exception as ex:
                    got = "EXC:%s" % type(ex).__name__
                good = (got == want)
                npass += good
                nfail += (not good)
                results.append((name, label, want, got, good))
                print("  [%s] %-42s %-20s expected=%r got=%r"
                      % ("PASS" if good else "FAIL", name, label, want, got))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # classification unit checks (no folders needed)
    cls = [({"http": 200, "bytes": 9}, False, "hit"),
           ({"http": 200, "bytes": 0}, False, "absent"),
           ({"http": 404, "bytes": 0}, False, "absent"),
           ({"http": 403, "bytes": 0}, False, "blocked"),
           ({"http": 429, "bytes": 0}, False, "blocked"),
           ({"http": 503, "bytes": 0}, False, "upstream"),
           ({"http": 0, "bytes": 0}, False, "unreachable"),
           ({"http": 301, "bytes": 0}, True, "hit")]
    for r, head, want in cls:
        got = classify(r, head)
        good = (got == want)
        npass += good
        nfail += (not good)
        print("  [%s] %-38s %-14s expected=%r got=%r"
              % ("PASS" if good else "FAIL", "classify()",
                 "http %s" % r["http"], want, got))

    print("-" * 70)
    print("SELF-TEST: %d passed, %d failed" % (npass, nfail))
    print("=" * 70)
    if nfail:
        print("The scanner is NOT reporting reality. Do not trust the dashboard.")
        return 1
    print("The scanner reports exactly what is on disk. Verified.")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# HTML  (EN / EL / DE — rendered client side from embedded JSON)
# ══════════════════════════════════════════════════════════════════════════
I18N = {
    "en": {
        "title": "data health", "checked": "checked", "files": "SMI files",
        "ok": "ALL CLEAR", "warn": "ATTENTION", "fail": "PROBLEMS",
        "cov": "SMI coverage %dd", "aqcov": "AQI coverage",
        "newest": "newest SMI", "fctom": "forecast tomorrow",
        "smi_h": "SMI · conditions/auto",
        "smi_s": "8 fixed 3-hour slots per day, UTC",
        "lg_yes": "present", "lg_no": "MISSING", "lg_nd": "not due yet",
        "lay_h": "Layers · third parties",
        "lay_s": "we request exactly what the app requests — availability, not storage",
        "aq_h": "AQI · aq-forecast",
        "aq_s": "ONE file per day holding 24 hourly values — not 3-hour slots",
        "t_for": "valid for", "t_made": "made on", "t_cont": "contents",
        "aq_note": "The file dated X is made the <b>previous</b> day (target = tomorrow). A gap on X means the cron of X-1 failed.",
        "refers": "refers to", "asked": "requested", "answered": "answered",
        "took": "took", "pub": "published (server)", "nopub": "server does not say",
        "url": "URL", "http": "HTTP", "size": "size",
        "evidence": "evidence", "cities": "cities",
        "no_probe": "Probes did not run (--no-net or no network). No claim is made about third parties — better empty than false.",
        "pattern": "<b>Pattern:</b> slot <b>%s</b> is missing <b>%d times</b> in the last %d days — responsible cron <b>%s</b>. Check below whether ECMWF had published that run at the time.",
        "selftest_ok": "Self-test passed: the scanner was verified against synthetic data with known answers.",
        "foot": "read-only · writes only inside verification/",
        "now": "local time",
        "tz_note": "The clock and the check time above are shown in <b>your device's timezone</b>, wherever you are. Everything in the grids and tables stays in <b>UTC</b>, because the data slots themselves are defined in UTC \u2014 converting them would misname the slots.",
        "st": {"ok": "current", "stale": "lagging", "absent": "DOES NOT EXIST",
               "blocked": "could not check", "unreachable": "could not check",
               "upstream": "could not check", "error": "could not check",
               "unknown": "unknown"},
        "m": {"empty_days": "%(n)d completely empty days: %(v)s",
              "no_smi": "no SMI data at all",
              "bad_names": "%(n)d invalid filenames in auto/",
              "corrupt": "%(n)d empty or invalid-header PNG files in auto/",
              "stale_data": "SMI data is %(v)sh old",
              "aging_data": "SMI data is %(v)sh old",
              "no_forecast": "no forecast (Tomorrow empty)",
              "forecast_partial": "forecast incomplete (%(n)d/8)",
              "forecast_wrong_day": "forecast is for %(v)s, not tomorrow",
              "slot_pattern": "slot %(v)s missing %(n)d times (cron %(v2)s)",
              "aq_missing": "AQI missing: %(v)s",
              "forecast_bad_names": "%(n)d invalid filenames in forecast/",
              "forecast_corrupt": "%(n)d empty or invalid-header PNG files in forecast/",
              "aq_invalid": "AQI file present but unreadable: %(v)s",
              "forecast_status": "last forecast: %(v)s",
              "layer_absent": "%(v)s: checked and DOES NOT exist",
              "layer_uncheckable": "%(v)s: could not check (%(v2)s)",
              "layer_stale": "%(v)s: lagging %(n)d slots"}},
    "el": {
        "title": "υγεία δεδομένων", "checked": "έλεγχος", "files": "αρχεία SMI",
        "ok": "ΟΛΑ ΚΑΘΑΡΑ", "warn": "ΠΡΟΣΟΧΗ", "fail": "ΣΦΑΛΜΑΤΑ",
        "cov": "SMI κάλυψη %dd", "aqcov": "AQI κάλυψη",
        "newest": "νεότερο SMI", "fctom": "forecast αύριο",
        "smi_h": "SMI · conditions/auto",
        "smi_s": "8 σταθερά 3ωρα ανά μέρα, σε UTC",
        "lg_yes": "υπάρχει", "lg_no": "ΛΕΙΠΕΙ", "lg_nd": "δεν οφείλει ακόμα",
        "lay_h": "Layers · τρίτοι",
        "lay_s": "ζητάμε ακριβώς ό,τι ζητάει η εφαρμογή — διαθεσιμότητα, όχι αποθήκευση",
        "aq_h": "AQI · aq-forecast",
        "aq_s": "ΕΝΑ αρχείο ανά μέρα με 24 ωριαίες τιμές — όχι 3ωρα slots",
        "t_for": "ισχύει για", "t_made": "φτιάχτηκε", "t_cont": "περιεχόμενο",
        "aq_note": "Το αρχείο με ημερομηνία X φτιάχνεται την <b>προηγούμενη</b> μέρα (target = αύριο). Κενό στην X σημαίνει ότι απέτυχε το cron της X-1.",
        "refers": "αναφέρεται σε", "asked": "ζητήθηκε", "answered": "απάντησε",
        "took": "χρόνος", "pub": "δημοσίευση (server)",
        "nopub": "δεν το δίνει ο server", "url": "URL", "http": "HTTP",
        "size": "μέγεθος", "evidence": "στοιχεία", "cities": "πόλεις",
        "no_probe": "Τα probes δεν έτρεξαν (--no-net ή χωρίς δίκτυο). Κανένας ισχυρισμός για τους τρίτους — καλύτερα κενά παρά ψεύτικα.",
        "pattern": "<b>Μοτίβο:</b> το slot <b>%s</b> λείπει <b>%d φορές</b> στις τελευταίες %d μέρες — υπεύθυνο cron <b>%s</b>. Δες παρακάτω αν το ECMWF είχε δημοσιεύσει το run εκείνη την ώρα.",
        "selftest_ok": "Το self-test πέρασε: ο σαρωτής επαληθεύτηκε με συνθετικά δεδομένα γνωστής απάντησης.",
        "foot": "read-only · γράφει μόνο στο verification/",
        "now": "τοπική ώρα",
        "tz_note": "Το ρολόι και η ώρα ελέγχου παραπάνω είναι στη <b>ζώνη ώρας της συσκευής σου</b>, όπου κι αν βρίσκεσαι. Ό,τι είναι στα πλέγματα και στους πίνακες μένει σε <b>UTC</b>, γιατί τα ίδια τα data slots ορίζονται σε UTC \u2014 η μετατροπή τους θα άλλαζε το όνομα του slot.",
        "st": {"ok": "ενήμερο", "stale": "καθυστερεί", "absent": "ΔΕΝ ΥΠΑΡΧΕΙ",
               "blocked": "δεν ελέγχθηκε", "unreachable": "δεν ελέγχθηκε",
               "upstream": "δεν ελέγχθηκε", "error": "δεν ελέγχθηκε",
               "unknown": "άγνωστο"},
        "m": {"empty_days": "%(n)d μέρες τελείως κενές: %(v)s",
              "no_smi": "κανένα SMI δεδομένο",
              "bad_names": "%(n)d άκυρα filenames στο auto/",
              "corrupt": "%(n)d κενά ή με άκυρο header PNG στο auto/",
              "stale_data": "τα SMI δεδομένα είναι %(v)sh παλιά",
              "aging_data": "τα SMI δεδομένα είναι %(v)sh παλιά",
              "no_forecast": "κανένα forecast (Tomorrow κενό)",
              "forecast_partial": "forecast ατελές (%(n)d/8)",
              "forecast_wrong_day": "forecast για %(v)s, όχι για αύριο",
              "slot_pattern": "slot %(v)s λείπει %(n)d φορές (cron %(v2)s)",
              "aq_missing": "AQI λείπει: %(v)s",
              "forecast_bad_names": "%(n)d άκυρα filenames στο forecast/",
              "forecast_corrupt": "%(n)d κενά ή με άκυρο header PNG στο forecast/",
              "aq_invalid": "AQI αρχείο υπάρχει αλλά δεν διαβάζεται: %(v)s",
              "forecast_status": "τελευταίο forecast: %(v)s",
              "layer_absent": "%(v)s: ελέγχθηκε και ΔΕΝ υπάρχει",
              "layer_uncheckable": "%(v)s: δεν ελέγχθηκε (%(v2)s)",
              "layer_stale": "%(v)s: καθυστερεί %(n)d slots"}},
    "de": {
        "title": "Datenzustand", "checked": "geprüft", "files": "SMI-Dateien",
        "ok": "ALLES IN ORDNUNG", "warn": "ACHTUNG", "fail": "FEHLER",
        "cov": "SMI-Abdeckung %dd", "aqcov": "AQI-Abdeckung",
        "newest": "neueste SMI", "fctom": "Vorhersage morgen",
        "smi_h": "SMI · conditions/auto",
        "smi_s": "8 feste 3-Stunden-Slots pro Tag, UTC",
        "lg_yes": "vorhanden", "lg_no": "FEHLT", "lg_nd": "noch nicht fällig",
        "lay_h": "Layer · Dritte",
        "lay_s": "wir fragen genau das ab, was die App anfordert — Verfügbarkeit, keine Speicherung",
        "aq_h": "AQI · aq-forecast",
        "aq_s": "EINE Datei pro Tag mit 24 Stundenwerten — keine 3-Stunden-Slots",
        "t_for": "gültig für", "t_made": "erstellt am", "t_cont": "Inhalt",
        "aq_note": "Die Datei mit Datum X wird am <b>Vortag</b> erstellt (target = morgen). Eine Lücke bei X bedeutet, dass der Cron von X-1 fehlgeschlagen ist.",
        "refers": "gilt für", "asked": "angefragt", "answered": "geantwortet",
        "took": "Dauer", "pub": "veröffentlicht (Server)",
        "nopub": "Server gibt es nicht an", "url": "URL", "http": "HTTP",
        "size": "Größe", "evidence": "Belege", "cities": "Städte",
        "no_probe": "Probes liefen nicht (--no-net oder kein Netz). Keine Aussage über Dritte — lieber leer als falsch.",
        "pattern": "<b>Muster:</b> Slot <b>%s</b> fehlt <b>%d mal</b> in den letzten %d Tagen — zuständiger Cron <b>%s</b>. Siehe unten, ob ECMWF den Lauf zu dieser Zeit bereits veröffentlicht hatte.",
        "selftest_ok": "Selbsttest bestanden: der Scanner wurde gegen synthetische Daten mit bekannter Antwort verifiziert.",
        "foot": "read-only · schreibt nur in verification/",
        "now": "Ortszeit",
        "tz_note": "Die Uhr und die Prüfzeit oben stehen in der <b>Zeitzone deines Geräts</b>, wo immer du bist. Alles in den Rastern und Tabellen bleibt in <b>UTC</b>, da die Daten-Slots selbst in UTC definiert sind \u2014 eine Umrechnung würde die Slots falsch benennen.",
        "st": {"ok": "aktuell", "stale": "verzögert", "absent": "EXISTIERT NICHT",
               "blocked": "nicht prüfbar", "unreachable": "nicht prüfbar",
               "upstream": "nicht prüfbar", "error": "nicht prüfbar",
               "unknown": "unbekannt"},
        "m": {"empty_days": "%(n)d völlig leere Tage: %(v)s",
              "no_smi": "keine SMI-Daten",
              "bad_names": "%(n)d ungültige Dateinamen in auto/",
              "corrupt": "%(n)d leere PNG-Dateien oder ungültiger Header in auto/",
              "stale_data": "SMI-Daten sind %(v)sh alt",
              "aging_data": "SMI-Daten sind %(v)sh alt",
              "no_forecast": "keine Vorhersage (Tomorrow leer)",
              "forecast_partial": "Vorhersage unvollständig (%(n)d/8)",
              "forecast_wrong_day": "Vorhersage für %(v)s, nicht für morgen",
              "slot_pattern": "Slot %(v)s fehlt %(n)d mal (Cron %(v2)s)",
              "aq_missing": "AQI fehlt: %(v)s",
              "forecast_bad_names": "%(n)d ungültige Dateinamen in forecast/",
              "forecast_corrupt": "%(n)d leere PNG-Dateien oder ungültiger Header in forecast/",
              "aq_invalid": "AQI-Datei vorhanden, aber nicht lesbar: %(v)s",
              "forecast_status": "letzte Vorhersage: %(v)s",
              "layer_absent": "%(v)s: geprüft und existiert NICHT",
              "layer_uncheckable": "%(v)s: nicht prüfbar (%(v2)s)",
              "layer_stale": "%(v)s: %(n)d Slots verzögert"}},
}

LAYER_NAMES = {
    "clouds": ("Clouds", "Σύννεφα", "Wolken", "NASA GIBS · VIIRS · daily pass"),
    "aod": ("Aerosol", "Aerosol", "Aerosol", "Copernicus CAMS · composition_aod550"),
    "dust": ("Dust", "Σκόνη", "Staub", "Copernicus CAMS · composition_duaod550"),
    "ecmwf": ("ECMWF open-data", "ECMWF open-data", "ECMWF open-data",
              "source of SMI · 6-hourly run"),
}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>smoggy · data health</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#05070c;color:#eaf2ff;font-family:ui-monospace,'SF Mono',Menlo,monospace;
 padding:14px;font-size:13px;line-height:1.5;-webkit-text-size-adjust:100%}
.wrap{max-width:560px;margin:0 auto}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px}
h1{font-size:15px;letter-spacing:1px}
h1 .b1{color:#44aaff}h1 .bo{color:#ffd84a}
.sub{font-size:11px;color:#7f93aa;margin-top:2px}
.clk{font-size:12px;color:#44aaff;margin-top:4px;font-variant-numeric:tabular-nums}
.clk .tz{color:#6f88a5;font-size:10.5px}
.langs{display:flex;gap:4px;flex-shrink:0}
.lb{background:#0d1525;border:1px solid rgba(68,170,255,.25);color:#8fa3bd;
 border-radius:8px;padding:4px 8px;font-size:11px;cursor:pointer;font-family:inherit}
.lb.on{color:#44aaff;border-color:#44aaff;background:rgba(68,170,255,.12)}
.banner{border-radius:12px;padding:11px 13px;margin-bottom:14px;border:1px solid}
.banner.ok{background:rgba(43,255,85,.10);border-color:rgba(43,255,85,.45);color:#7dffa0}
.banner.warn{background:rgba(255,216,74,.10);border-color:rgba(255,216,74,.45);color:#ffd84a}
.banner.fail{background:rgba(255,80,80,.12);border-color:rgba(255,80,80,.5);color:#ff8a8a}
.banner .t{font-size:15px;font-weight:700;letter-spacing:.5px}
.banner ul{margin:7px 0 0 16px;font-size:11.5px;line-height:1.7}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:16px}
.kpi{background:#0d1525;border:1px solid rgba(68,170,255,.16);border-radius:11px;padding:10px 12px}
.kpi .k{font-size:10px;letter-spacing:1px;color:#7f93aa;text-transform:uppercase}
.kpi .v{font-size:21px;font-weight:800;margin-top:4px}
.g{color:#2BFF55}.y{color:#ffd84a}.r{color:#ff6b6b}
h2{font-size:12px;letter-spacing:1.4px;color:#44aaff;text-transform:uppercase;margin:20px 0 3px}
.h2s{font-size:11px;color:#7f93aa;margin-bottom:9px}
.hdr,.row{display:flex;gap:2px;align-items:center}
.hdr{margin-bottom:5px}
.lab{width:44px;flex:0 0 44px;font-size:10.5px;color:#8fa3bd}
.cell{flex:1 1 0;min-width:0;height:21px;border-radius:3px;background:#ff5252}
.cell.p{background:#2BFF55}.cell.n{background:#26344a}
.hc{flex:1 1 0;min-width:0;text-align:center;font-size:10px;color:#6f88a5}
.row{margin-bottom:2px}
.cnt{width:28px;flex:0 0 28px;text-align:right;font-size:10.5px;color:#8fa3bd}
.leg{display:flex;gap:13px;flex-wrap:wrap;margin-top:10px;font-size:10.5px;color:#8fa3bd}
.leg i{width:11px;height:11px;border-radius:2px;display:inline-block;vertical-align:-1px;margin-right:5px}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{text-align:left;font-size:10px;letter-spacing:1px;color:#44aaff;text-transform:uppercase;
 padding:0 0 6px;border-bottom:1px solid rgba(68,170,255,.16)}
th.r,td.r{text-align:right}
td{padding:7px 0;border-bottom:1px solid rgba(68,170,255,.07);color:#cfe0f5}
tr:last-child td{border-bottom:0}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.card{background:#0d1525;border:1px solid rgba(68,170,255,.16);border-radius:11px;
 padding:11px 12px;margin-bottom:8px}
.card .nm{font-size:13px;font-weight:700}
.card .src{font-size:10px;color:#6f88a5;margin:1px 0 7px 14px}
.tr2{display:flex;justify-content:space-between;gap:10px;padding:2px 0;font-size:11px}
.tr2 .k{color:#7f93aa;flex-shrink:0}.tr2 .v{text-align:right;word-break:break-all}
.mu{color:#5c7189}
details{margin-top:7px}
summary{font-size:10.5px;color:#44aaff;cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"+ "}
details[open] summary::before{content:"- "}
.ev{margin-top:6px;padding:7px 8px;background:#070c16;border-radius:8px;
 font-size:10px;color:#8fa3bd;line-height:1.6;word-break:break-all}
.note{background:#0d1525;border:1px solid rgba(68,170,255,.16);border-radius:11px;
 padding:10px 12px;margin-top:13px;font-size:11.5px;color:#a9c0d8;line-height:1.65}
.note.st{border-color:rgba(43,255,85,.3);color:#7dffa0}
.foot{margin-top:18px;font-size:10px;color:#5c7189;line-height:1.7;text-align:center}
</style></head><body><div class="wrap">
<div class="top">
 <div><h1><span class="b1">sm</span><span class="bo">o</span><span class="b1">ggy</span>
  &middot; <span id="ttl"></span></h1><div class="clk" id="clk"></div>
  <div class="sub" id="sub"></div></div>
 <div class="langs">
  <button class="lb" data-l="en">EN</button>
  <button class="lb" data-l="el">EL</button>
  <button class="lb" data-l="de">DE</button>
 </div>
</div>
<div id="app"></div>
<div class="foot">make_status.py &middot; <span id="ft"></span><br>
ECMWF / Copernicus CAMS &middot; NASA GIBS &middot; Open-Meteo (CC BY 4.0)</div>
</div>
<script id="DATA" type="application/json">__DATA__</script>
<script id="I18N" type="application/json">__I18N__</script>
<script id="LNAMES" type="application/json">__LNAMES__</script>
<script>
var S=JSON.parse(document.getElementById('DATA').textContent);
var T=JSON.parse(document.getElementById('I18N').textContent);
var LN=JSON.parse(document.getElementById('LNAMES').textContent);
var LANG='en';
try{var s=localStorage.getItem('smoggyLang');if(s&&T[s])LANG=s;}catch(e){}
var ST_COL={ok:'#2BFF55',stale:'#ffd84a',absent:'#ff5252',blocked:'#5c7189',
 unreachable:'#5c7189',upstream:'#5c7189',error:'#5c7189',unknown:'#5c7189'};
var LIDX={en:0,el:1,de:2};
function esc(x){var d=document.createElement('div');d.textContent=x==null?'':String(x);return d.innerHTML;}
/* LOCAL TIME. Deliberately done here and not in Python: make_status.py runs
   on a CI runner that is always UTC, so it cannot possibly know where the
   reader is. The browser can. This follows the device wherever it travels,
   with no configuration. The grids and tables stay in UTC because the data
   slots themselves are defined in UTC -- see L.tz_note. */
function LOC(){return LANG==='el'?'el-GR':(LANG==='de'?'de-DE':'en-GB');}
function TZ(){try{return Intl.DateTimeFormat().resolvedOptions().timeZone||'';}
  catch(e){return '';}}
function offs(d){var o=-d.getTimezoneOffset(),s=o<0?'-':'+';o=Math.abs(o);
  return 'UTC'+s+('0'+Math.floor(o/60)).slice(-2)+':'+('0'+(o%60)).slice(-2);}
function toLocal(iso){
  var d=new Date(iso);
  if(!iso||isNaN(d.getTime()))return esc(iso||'—');
  return esc(d.toLocaleString(LOC(),{day:'2-digit',month:'2-digit',
    hour:'2-digit',minute:'2-digit',hour12:false}));
}
function tick(){
  var el=document.getElementById('clk');if(!el)return;
  var n=new Date(),z=TZ();
  var t=n.toLocaleTimeString(LOC(),{hour:'2-digit',minute:'2-digit',
        second:'2-digit',hour12:false});
  var dd=n.toLocaleDateString(LOC(),{weekday:'short',day:'2-digit',month:'2-digit'});
  el.innerHTML=esc(T[LANG].now)+': <b>'+esc(dd+' '+t)+'</b>'+
    ' <span class="tz">'+esc((z?z+' · ':'')+offs(n))+'</span>';
}
setInterval(tick,1000);
function fmt(tpl,o){return tpl.replace(/%\\((\\w+)\\)[ds]/g,function(_,k){return o[k]==null?'':o[k];});}
function msg(m){var t=T[LANG].m[m.c];return t?fmt(t,m):m.c;}
function cc(v){return v>=95?'g':v>=80?'y':'r';}
function render(){
 var L=T[LANG],smi=S.smi,aq=S.aq,fc=S.forecast,P=S.probes||{};
 document.documentElement.lang=LANG;
 document.getElementById('ttl').textContent=L.title;
 document.getElementById('sub').innerHTML=esc(L.checked)+': '+toLocal(S.generated_at)+
   ' <span class="tz">('+esc(S.generated_at)+')</span> · '+esc(smi.files+' '+L.files);
 tick();
 document.getElementById('ft').textContent=L.foot;
 [].forEach.call(document.querySelectorAll('.lb'),function(b){
   b.className='lb'+(b.dataset.l===LANG?' on':'');});
 var h='';
 var li=S.fails.concat(S.warns).map(function(m){return '<li>'+esc(msg(m))+'</li>';}).join('');
 h+='<div class="banner '+S.state+'"><div class="t">'+L[S.state]+'</div>'+(li?'<ul>'+li+'</ul>':'')+'</div>';
 if(S.selftest_passed){h+='<div class="note st">'+L.selftest_ok+'</div>';}
 var a=smi.age_hours,ac=a==null?'r':(a<=12?'g':(a<=24?'y':'r'));
 h+='<div class="kpis">'+
  '<div class="kpi"><div class="k">'+L.cov.replace('%d',smi.days)+'</div><div class="v '+cc(smi.coverage_pct)+'">'+Math.round(smi.coverage_pct)+'%</div></div>'+
  '<div class="kpi"><div class="k">'+L.aqcov+'</div><div class="v '+cc(aq.coverage_pct)+'">'+Math.round(aq.coverage_pct)+'%</div></div>'+
  '<div class="kpi"><div class="k">'+L.newest+'</div><div class="v '+ac+'">'+(a==null?'—':a.toFixed(1)+'h')+'</div></div>'+
  '<div class="kpi"><div class="k">'+L.fctom+'</div><div class="v '+((fc.complete&&fc.is_tomorrow)?'g':'r')+'">'+(fc.slots?fc.slots+'/8':'—')+'</div></div>'+
  '</div>';
 h+='<h2>'+L.smi_h+'</h2><div class="h2s">'+L.smi_s+'</div>';
 h+='<div class="hdr"><span class="lab"></span>'+smi.slots.map(function(x){
   return '<span class="hc">'+('0'+x).slice(-2)+'</span>';}).join('')+'<span class="cnt"></span></div>';
 h+=smi.grid.map(function(r){
   return '<div class="row"><span class="lab">'+esc(r.label)+'</span>'+
    r.cells.map(function(c){return '<span class="cell '+(c===1?'p':(c===2?'n':''))+'"></span>';}).join('')+
    '<span class="cnt">'+r.present+'/8</span></div>';}).join('');
 h+='<div class="leg"><span><i style="background:#2BFF55"></i>'+L.lg_yes+'</span>'+
  '<span><i style="background:#ff5252"></i>'+L.lg_no+'</span>'+
  '<span><i style="background:#26344a"></i>'+L.lg_nd+'</span></div>';
 if(smi.worst_hour!=null){
   var wh=('0'+smi.worst_hour).slice(-2)+'Z';
   h+='<div class="note">'+L.pattern.replace('%s',wh)
      .replace('%d',smi.missing_by_hour[smi.worst_hour])
      .replace('%d',smi.days).replace('%s',smi.slot_cron[smi.worst_hour])+'</div>';
 }
 h+='<h2>'+L.lay_h+'</h2><div class="h2s">'+L.lay_s+'</div>';
 if(!P.enabled){h+='<div class="note">'+L.no_probe+'</div>';}
 else{
  ['clouds','aod','dust','ecmwf'].forEach(function(k){
   var v=P.layers[k]||{status:'unknown',tries:[]},nm=LN[k],st=v.status||'unknown';
   var tr=v.tries&&v.tries.length?v.tries:[{}],f=tr[0],la=tr[tr.length-1];
   var pub=la.last_modified||la.server_date;
   var col=ST_COL[st]||'#5c7189';
   h+='<div class="card"><div class="nm"><span class="dot" style="background:'+col+'"></span>'+
     esc(nm[LIDX[LANG]])+' <span style="color:'+col+';font-size:11px;font-weight:400">'+
     esc(L.st[st]||st)+'</span></div><div class="src">'+esc(nm[3])+'</div>'+
     row(L.refers,'<b>'+esc(v.newest||'—')+'</b>')+
     row(L.asked,esc(f.asked_at||'—'))+
     row(L.answered,esc(la.got_at||'—'))+
     row(L.took,la.ms==null?'—':la.ms+' ms')+
     row(L.pub,pub?esc(pub):'<span class="mu">'+L.nopub+'</span>')+
     '<details><summary>'+L.evidence+' ('+tr.length+')</summary>'+
      tr.map(function(t){return '<div class="ev">'+L.http+' '+(t.http||0)+
        ' · '+L.size+' '+(t.bytes||0)+'B · '+(t.ms||0)+'ms · '+esc(t.verdict||'')+
        '<br>'+esc(t.url||'')+(t.error?'<br>'+esc(t.error):'')+'</div>';}).join('')+
     '</details></div>';
  });
 }
 h+='<h2>'+L.aq_h+'</h2><div class="h2s">'+L.aq_s+'</div>';
 h+='<table><tr><th>'+L.t_for+'</th><th>'+L.t_made+'</th><th class="r">'+L.t_cont+'</th></tr>'+
  aq.rows.map(function(r){
   var d,t,c;
   if(r.present&&!r.note){d='#2BFF55';t=r.cities+' '+L.cities+' · 24h';c='#cfe0f5';}
   else if(r.present){d='#ffd84a';t=esc(r.note);c='#ffd84a';}
   else if(r.due===false){d='#26344a';t=L.lg_nd;c='#7f93aa';}
   else{d='#ff5252';t=L.lg_no;c='#ff8a8a';}
   return '<tr><td><span class="dot" style="background:'+d+'"></span>'+esc(r.label)+
    '</td><td class="mu">'+esc(r.made_on)+'</td><td class="r" style="color:'+c+'">'+t+'</td></tr>';
  }).join('')+'</table>';
 h+='<div class="note">'+L.aq_note+'</div>';
 h+='<div class="note">'+L.tz_note+'</div>';
 document.getElementById('app').innerHTML=h;
}
function row(k,v){return '<div class="tr2"><span class="k">'+k+'</span><span class="v">'+v+'</span></div>';}
[].forEach.call(document.querySelectorAll('.lb'),function(b){
  b.onclick=function(){LANG=b.dataset.l;try{localStorage.setItem('smoggyLang',LANG);}catch(e){}render();};});
render();
</script></body></html>"""


def build_html(S):
    lnames = {k: [v[0], v[1], v[2], v[3]] for k, v in LAYER_NAMES.items()}
    return (PAGE
            .replace("__DATA__", json.dumps(S, ensure_ascii=False))
            .replace("__I18N__", json.dumps(I18N, ensure_ascii=False))
            .replace("__LNAMES__", json.dumps(lnames, ensure_ascii=False)))


# ══════════════════════════════════════════════════════════════════════════
def main():
    if has_arg("--selftest"):
        return selftest()

    now = dt.datetime.now(dt.timezone.utc)
    ndays = arg_int("--days", 10, 3, 60)

    S = scan_local(now, ndays)
    S["probes"] = run_probes(now, not has_arg("--no-net"))
    S["generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    S["state"], S["fails"], S["warns"] = verdict(S)

    # run the self-test silently so the page can state whether the scanner
    # itself was verified on THIS run (not a claim from some earlier day)
    try:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            S["selftest_passed"] = (selftest() == 0)
    except Exception:
        S["selftest_passed"] = False

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, data in (("status.json", json.dumps(S, indent=1, ensure_ascii=False)),
                       ("index.html", build_html(S))):
        dst = os.path.join(OUT_DIR, name)
        tmp = dst + ".partial"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, dst)          # atomic — never a half-written file
        print("wrote:", os.path.relpath(dst, BASE_DIR),
              os.path.getsize(dst) // 1024, "KB")

    print("state:", S["state"].upper(),
          "| SMI %.1f%% | AQI %.1f%% | selftest %s"
          % (S["smi"]["coverage_pct"], S["aq"]["coverage_pct"],
             "PASS" if S["selftest_passed"] else "FAIL"))
    for x in S["fails"]:
        print("  FAIL:", x)
    for x in S["warns"]:
        print("  WARN:", x)
    return 0          # the gate is test_smoggy.py, not this


if __name__ == "__main__":
    sys.exit(main())
