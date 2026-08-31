#!/usr/bin/env python3
"""
make_conditions.py — VIMA 2 tou #053.  (sti RIZA tou repo smoggy-data)

Katevazei ENA subset apo to ECMWF open data (t + r sta 250 hPa) GIA KATHE
step [0,3] tou idiou run, ypologizei tin katastasi contrail me tin IDIA
fysiki me tin app, kai grafei ENA PNG ANA step se provoli Web Mercator —
2 PNG/run, 8 PNG/mera synolika.

Alex 28/08/26 (polling redesign): DEN manteuoume pia POTE PIA "expected
cycle" apo to roloi/cron. To .last_auto_run (root-level, commit-aretai)
kratai to teleftaio epityximeno ECMWF cycle -- to EPOMENO ine APLA auto
+6h, deterministic, MIDEN manteuma. To cron treksei SYXNA (kathe 30 lepta,
antikeimeniko onoma "polling" oxi "single pull") kai ROTAEI an ine etoimo
to epomeno -- an oxi (404), KATHARO skip, OXI failure, ksanadokimazei sto
epomeno tick. Diakrisi: 404=sigouro "oxi akoma" (siopilo), 500/503/timeout
=aoristo diktyako (warning, oxi failure), opoiodipote allo=PRAGMATIKO bug
(hard failure, kokkino X).

ECMWF open data: CC BY 4.0 — dorean, kai gia emporiki xrisi, ME anafora.
POTE olokliro arxeio: mia mera = ~726 GiB. Mono subset.
"""
import os
import sys
import shutil
import traceback
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

# ── FORECAST (Tomorrow) — xoristo path, DEN piratzei to STEPS/OUTDIR panw ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FORECAST_OUTDIR = os.path.join(BASE_DIR, "conditions", "forecast")
STATUS_FILE = os.path.join(BASE_DIR, ".forecast_status")  # root-level, EKTOS conditions/
GRIB_FC = "/tmp/smoggy_ecmwf_forecast_step%d.grib2"       # xoristo apo to GRIB tou auto
TARGETS_UTC_HH = [0, 3, 6, 9, 12, 15, 18, 21]              # ta 8 valid times tou "Tomorrow"
MAX_STEP = 144                                             # orio ECMWF opendata tier

# ── AUTO polling state — root-level, EKTOS conditions/, commit-aretai ──
LAST_AUTO_FILE = os.path.join(BASE_DIR, ".last_auto_run")


class ForecastFailure(Exception):
    """Forecast-only failure. Den prepei POTE na blokarei to AUTO commit."""
    pass


class NotReadyYet(Exception):
    """ECMWF: to zitoumeno cycle den exei dimosiefsei akoma (HTTP 404,
    SIGOURO). KANONIKO gegonos gia to polling, OXI failure -- siopilo skip,
    ksanadokimazei sto epomeno tick."""
    pass


class UncertainAvailability(Exception):
    """Diktyako/server provlima (500/503/timeout/connection) KATA to
    request -- DEN xeroume an to cycle yparxei i oxi. DEN to xeirizomaste
    san 'den yparxei' (auto tha itan to idio lathos me to palio -3h
    fallback tou client). Warning, oxi hard failure gia MIA fora — an
    epimenei se polla tick sti seira, tha fainetai sta logs."""
    pass


def download(step, date=None, time=None, target=None):
    # date/time/target: EAN einai None (default), symperifora AKRIVWS idia
    # me prin -- kanena callsite den ta afhnei kena pia (kai to AUTO kai to
    # forecast tora perbnoun panta sygkekrimeno date/time, MIDEN 'latest').
    from ecmwf.opendata import Client
    import requests
    client = Client(source="ecmwf")
    if target is None:
        target = GRIB % step
    kwargs = dict(type="fc", step=step, param=["t", "r"],
                  levelist=LEVELS_HPA, target=target)
    if date is not None:
        kwargs["date"] = date
    if time is not None:
        kwargs["time"] = time
    try:
        result = client.retrieve(**kwargs)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            raise NotReadyYet(
                "step %d date=%s time=%s -> 404 (den exei dimosiefsei akoma)"
                % (step, date, time)) from e
        raise UncertainAvailability(
            "step %d date=%s time=%s -> HTTP %s" %
            (step, date, time, status)) from e
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        raise UncertainAvailability(
            "step %d date=%s time=%s -> %s: %s" %
            (step, date, time, type(e).__name__, e)) from e
    print("step", step, "run  :", result.datetime, "UTC")
    print("step", step, "bytes:", os.path.getsize(target))
    return target, result.datetime, result.datetime + dt.timedelta(hours=step)


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


def process_one(step, expected_run=None):
    # Alex 28/08/26: expected_run (proaiiretiko) -> pin sto ANAMENOMENO ECMWF
    # cycle anti na zitame 'latest' xoris elegxo. Idio pattern me to
    # run_forecast() (date=/time=). Xoris arg (None) -> AKRIVWS i palia
    # symperifora, kamia allagi.
    target, run_datetime, valid = download(
        step,
        date=(expected_run.date() if expected_run is not None else None),
        time=(expected_run.hour if expected_run is not None else None))
    if expected_run is not None and ensure_utc(run_datetime) != expected_run:
        raise RuntimeError(
            "AUTO run mismatch: expected %s, got %s (apanithike KATI, alla "
            "oxi to cycle pou zitisame)" % (expected_run, run_datetime))
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
    return run_datetime


def ensure_utc(d):
    # naive -> assume UTC (tag, xoris shift). aware (allis tz) -> PRAGMATIKI
    # metatropi se UTC (astimezone, oxi aplo tag). Efarmozetai PANTA prin
    # apo opoiadipote subtraction/comparison metaxy datetimes.
    if d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def read_last_auto_run():
    """Diavazei to teleftaio epityximeno AUTO cycle apo to committed arxeio.
    None an den yparxei akoma (PROTI fora pote, i to arxeio xathike)."""
    try:
        with open(LAST_AUTO_FILE, "r") as f:
            s = f.read().strip()
        return ensure_utc(dt.datetime.fromisoformat(s))
    except Exception:
        return None


def write_last_auto_run(run_dt):
    # fail-safe / best-effort, idio pnevma me to write_status() — an spasei
    # to grapsimo, den prepei na rixnei olokliro to script.
    try:
        with open(LAST_AUTO_FILE, "w") as f:
            f.write(ensure_utc(run_dt).isoformat())
    except Exception as e:
        print("::warning:: could not write last-auto-run file:",
              type(e).__name__, e)


def _bootstrap_guess():
    """Otan leipei to state, synexizei meta apo to teleftaio AUTO PNG."""
    valid_times = []
    for filename in os.listdir(OUTDIR):
        try:
            valid = dt.datetime.strptime(
                filename, "%Y-%m-%dT%HZ.png"
            ).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        valid_times.append(valid)

    if not valid_times:
        raise RuntimeError(
            "AUTO bootstrap failed: no valid PNG found in %s" % OUTDIR)

    latest_valid = max(valid_times)
    latest_run = latest_valid.replace(
        hour=(latest_valid.hour // 6) * 6)
    return latest_run + dt.timedelta(hours=6)


def next_expected_run(last):
    if last is None:
        return _bootstrap_guess()
    return last + dt.timedelta(hours=6)


def write_status(value):
    # fail-safe / best-effort: POTE den kanei raise, POTE den epireazei
    # to exit code tou script -- status-file failure != forecast failure
    # kai kanena apo ta dyo den prepei na blokarei to AUTO commit.
    try:
        with open(STATUS_FILE, "w") as f:
            f.write(value)
    except Exception as e:
        print("::warning:: could not write forecast status file:",
              type(e).__name__, e)


def run_forecast(locked_run_raw):
    """Tomorrow forecast (8 valid times), OLA apo ENA kleidomeno ECMWF run.
    ALL-OR-NOTHING: to conditions/forecast/ allazei MONO an kai ta 8 PNG
    petyxoun apo to IDIO run. Kathe apotyxia (opoudipote) = to yparxon
    conditions/forecast/ menei akrivos opws itan."""
    locked_run = ensure_utc(locked_run_raw)
    # "Tomorrow" = current UTC calendar date +1 (i mera pou zitaei o xristis),
    # OXI i imerominia tou locked ECMWF run -- an to run einai palio (π.χ.
    # execution 25/08 02:40Z me latest run 24/08 18Z), to "avrio" tou run
    # tha itan LATHOS mera. To locked_run xrisimopoieitai APOKLEISTIKA gia
    # ton ypologismo step = target_valid - locked_run parakato.
    tomorrow = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1))

    uid = "%d_%d" % (os.getpid(),
                      int(dt.datetime.now(dt.timezone.utc).timestamp()))
    # root-level (BASE_DIR = riza tou repo), EKTOS conditions/ -- POTE den
    # piananontai apo "git add conditions/..."
    stage = os.path.join(BASE_DIR, ".forecast_stage_%s" % uid)
    backup = os.path.join(BASE_DIR, ".forecast_backup_%s" % uid)
    written = []
    swap_failed_and_unrecovered = False

    try:
        os.makedirs(stage, exist_ok=True)

        for hh in TARGETS_UTC_HH:
            target_valid = ensure_utc(
                dt.datetime(tomorrow.year, tomorrow.month, tomorrow.day, hh, 0))
            delta_seconds = (target_valid - locked_run).total_seconds()

            if delta_seconds % 3600 != 0:
                raise ForecastFailure(
                    "non-integer hour delta for %s" % target_valid)
            step = int(delta_seconds // 3600)
            if step < 0 or step % 3 != 0 or step > MAX_STEP:
                raise ForecastFailure(
                    "invalid step %d for %s" % (step, target_valid))

            grib_target = GRIB_FC % step
            _tgt, run_dt, valid = download(
                step, date=locked_run.date(), time=locked_run.hour,
                target=grib_target)

            actual_run = ensure_utc(run_dt)
            actual_valid = ensure_utc(valid)
            if actual_run != locked_run or actual_valid != target_valid:
                raise ForecastFailure(
                    "run/step mismatch for %s (got run=%s valid=%s)" %
                    (target_valid, actual_run, actual_valid))

            # IDIO pipeline me process_one -- read_grib / ct_state_multilevel /
            # resample_to_mercator / state_to_rgba -- KAMIA allagi se fysiki.
            t_stack, rh_stack, lats, lons = read_grib(grib_target, LEVELS_HPA)
            state = sg.ct_state_multilevel(list(t_stack), list(rh_stack), LEVELS_HPA)
            merc = sg.resample_to_mercator(state, lats, lons, WIDTH, HEIGHT)
            img = Image.fromarray(sg.state_to_rgba(merc), "RGBA")

            stamp = target_valid.strftime("%Y-%m-%dT%HZ")
            img.save(os.path.join(stage, stamp + ".png"), optimize=True)
            print("forecast grafike:", stamp, "step", step)
            written.append(target_valid)

        if len(written) != 8:
            raise ForecastFailure(
                "only %d/8 valid times succeeded" % len(written))

        # ---- Alex 27/08/26: RETENTION ton SIMERINON forecast PNG ----
        # Ta 8 PNG tis SIMERINIS imeras katevikan XTHES os "Tomorrow".
        # To atomic rename parakato antikathista OLOKLIRO ton fakelo, ara
        # xoris auto to block tha xanontan akrivos ti stigmi pou ta
        # xreiazomaste (Today slots pou den exoun akoma AUTO tile).
        # KANONAS: to forecast/ kratai MONO Today + Tomorrow.
        #   - simerina stamps -> antigrafontai apo to palio set sto stage
        #   - xthesina kai palaiotera -> DEN antigrafontai (pefton me to rename)
        #   - an to trexon run xanaeftiakse to idio stamp -> NIKAEI to neo
        # MIDENIKO neo download. MIDEMIA epidrasi sto Tomorrow.
        today_prefix = dt.datetime.now(dt.timezone.utc).date().isoformat() + "T"
        kept = 0
        if os.path.isdir(FORECAST_OUTDIR):
            for fn in sorted(os.listdir(FORECAST_OUTDIR)):
                if not fn.startswith(today_prefix) or not fn.endswith(".png"):
                    continue
                dst = os.path.join(stage, fn)
                if os.path.exists(dst):
                    continue
                shutil.copy2(os.path.join(FORECAST_OUTDIR, fn), dst)
                kept += 1
        print("forecast retention: kratithikan", kept, "simerina PNG")

        # ---- ATOMIC SWAP (root-level paths, idio filesystem -> os.rename
        #      einai atomic) ----
        # os.rename apaitei na yparxei o parent fakelos tou proorismou.
        # Simera "tha" yparxei idi giati to AUTO loop (process_one) ton
        # dimiourgei san side-effect tou dikou tou os.makedirs(OUTDIR) --
        # ALLA auto einai implicit/eythrausto (an allaksei i seira klisis,
        # i an kaneis kalesei pote to run_forecast() xechorista, spaei).
        # EXPLICIT eggyisi edo, anexartita apo to AUTO:
        os.makedirs(os.path.dirname(FORECAST_OUTDIR), exist_ok=True)

        had_old = os.path.isdir(FORECAST_OUTDIR)
        if had_old:
            os.rename(FORECAST_OUTDIR, backup)

        try:
            os.rename(stage, FORECAST_OUTDIR)
        except Exception as swap_err:
            if had_old:
                try:
                    os.rename(backup, FORECAST_OUTDIR)
                except Exception as rollback_err:
                    swap_failed_and_unrecovered = True
                    print("::error:: FORECAST PUBLISH+ROLLBACK BOTH FAILED. "
                          "Old forecast set preserved at: %s -- manual "
                          "recovery needed (rename to conditions/forecast/). "
                          "swap_err=%r rollback_err=%r" %
                          (backup, swap_err, rollback_err))
                    raise ForecastFailure(
                        "publish and rollback both failed") from rollback_err
                else:
                    raise ForecastFailure(
                        "publish failed, rolled back to previous set") from swap_err
            else:
                raise ForecastFailure(
                    "publish failed, no previous set existed") from swap_err

    finally:
        # staging: PANTA cleanup
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
        # backup: cleanup MONO an DEN eimaste sto unrecovered case
        # (successful publish -> backup = palio, perito.
        #  successful rollback -> backup path adeiase, asfales.
        #  unrecovered -> MENEI epitides, idi typothike to path pio panw.)
        if not swap_failed_and_unrecovered and os.path.isdir(backup):
            shutil.rmtree(backup, ignore_errors=True)


def main():
    last_auto = read_last_auto_run()
    next_run = next_expected_run(last_auto)
    print("AUTO: epomeno anamenomeno cycle ->", next_run.isoformat(),
          "(proigoumeno:", (last_auto.isoformat() if last_auto else "KANENA -- bootstrap"), ")")

    locked_run = None
    new_data = False
    try:
        for i, step in enumerate(STEPS):
            run_dt = process_one(step, expected_run=next_run)
            if i == 0:
                locked_run = run_dt   # MONO to PROTO step kleidonei to run
        new_data = True
    except NotReadyYet as e:
        print("::notice:: AUTO: to epomeno cycle den exei dimosiefsei akoma "
              "-- kanoniko, ksanadokimazei sto epomeno tick. %s" % e)
    except UncertainAvailability as e:
        print("::warning:: AUTO: aoristo diktyako/server provlima (OXI "
              "sigouro 'den yparxei') -- ksanadokimazei sto epomeno tick. %s" % e)
    except Exception as e:
        # PRAGMATIKO, mi anamenomeno sfalma (bug, kateklismeno GRIB, klp) --
        # AYTO PREPEI na fainetai kokkino, oxi na katapinetai san "not ready".
        print("::error:: AUTO FAILED (mi anamenomeno):", type(e).__name__, e)
        traceback.print_exc()
        sys.exit(1)

    if new_data:
        write_last_auto_run(locked_run)
        print("last_auto_run enimerothike ->", ensure_utc(locked_run).isoformat())

    # ---- FORECAST: MONO otan yparxei FRESKO AUTO se AYTO to tick --
    #      (den exei noima na xanaktrexei to forecast se kathe "not ready"
    #      tick pou den evgale tipota neo). ----
    write_status("failure")   # fail-safe default PRIN xekinisei to forecast
    if new_data:
        try:
            run_forecast(locked_run)
            write_status("success")
        except Exception as e:
            # OXI mono ForecastFailure -- piastikan KAI network/ECMWF/IO errors
            # pou tha mporousan na xefygoun apo to run_forecast().
            print("::warning:: FORECAST FAILED:", type(e).__name__, e)
            traceback.print_exc()
            # DEN ginetai re-raise -> exit 0 -> to commit tou idi-etoimou AUTO
            # trexei kanonika
    else:
        print("::notice:: FORECAST paraleiftike -- kanena neo AUTO cycle se auto to tick")


if __name__ == "__main__":
    main()
