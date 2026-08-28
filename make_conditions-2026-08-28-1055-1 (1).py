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


class ForecastFailure(Exception):
    """Forecast-only failure. Den prepei POTE na blokarei to AUTO commit."""
    pass


def download(step, date=None, time=None, target=None):
    # date/time/target: EAN einai None (default), symperifora AKRIVWS idia
    # me prin -- to AUTO callsite (process_one) den ta perna kathologou.
    # Xrisimopoiountai MONO apo to forecast path gia na "kleidosoun" ena
    # sygkekrimeno ECMWF run anti gia to "latest".
    from ecmwf.opendata import Client
    client = Client(source="ecmwf")
    if target is None:
        target = GRIB % step
    kwargs = dict(type="fc", step=step, param=["t", "r"],
                  levelist=LEVELS_HPA, target=target)
    if date is not None:
        kwargs["date"] = date
    if time is not None:
        kwargs["time"] = time
    result = client.retrieve(**kwargs)
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
            "AUTO run mismatch: expected %s, got %s (to ECMWF den exei akoma "
            "dimosiepsei to anamenomeno cycle)" % (expected_run, run_datetime))
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


# Alex 28/08/26: ta IDIA cron slots me to conditions.yml ("40 2,8,14,20 * * *").
# Xeirokinita edo giati to script den mporei na diavasei to .yml. AN allaxei
# pote to conditions.yml, prepei na allaxei kai edo (γι' αυτό το σχόλιο).
CRON_SLOTS_UTC = [(2, 40, 0), (8, 40, 6), (14, 40, 12), (20, 40, 18)]
# (ora cron, lepto cron, anamenomeno ECMWF cycle)


def expected_cycle_utc():
    # Alex 28/08/26: v3 -- PROTIMATAI to EXPECTED_CYCLE_HOUR pou perna idio
    # to conditions.yml (map apo to github.event.schedule sto SOSTO cron
    # slot, se separate step, PRIN trexei auto to script). Auto ine PLIROS
    # deterministic -- den exartatai KATHOLOU apo poso kathisterise o
    # runner. An leipei (px manual workflow_dispatch, opou den yparxei
    # github.event.schedule), peftoume ston PALIO (v2) ypologismo apo to
    # roloi -- idio kwdika me prin, kamia allagi ekei, mono os fallback gia
    # mi-scheduled triggers.
    env_h = os.environ.get("EXPECTED_CYCLE_HOUR", "").strip()
    now = dt.datetime.now(dt.timezone.utc)
    if env_h in ("0", "6", "12", "18"):
        cyc_hour = int(env_h)
        # Alex 28/08/26: to cyc_hour MONO DEN arkei gia ti sosti imerominia --
        # an o runner perasei ta UTC mesanyxta, to now.date() tha itan LATHOS
        # (mia mera meta). To trigger_hour (i ora tou idiou cron, oxi tou
        # cycle) ine PANTA cyc_hour+2 me lepto :40 (2,8,14,20 <-> 0,6,12,18 --
        # idio mapping me to conditions.yml). An i simerini stigmi tou
        # trigger EINAI STO MELLON se sxesi me to 'now', simainei oti
        # perasame mesanyxta apo tote pou skedul-aristike -- i SOSTI
        # imerominia ine XTHES, oxi simera.
        trigger_hour = cyc_hour + 2
        candidate_today = dt.datetime(now.year, now.month, now.day,
                                       trigger_hour, 40,
                                       tzinfo=dt.timezone.utc)
        expected_date = (now.date() if candidate_today <= now
                          else now.date() - dt.timedelta(days=1))
        return dt.datetime(expected_date.year, expected_date.month,
                            expected_date.day, cyc_hour, 0,
                            tzinfo=dt.timezone.utc)
    if env_h:
        print("::warning:: EXPECTED_CYCLE_HOUR=%r den ine egkyri timi "
              "(perimenoume 0/6/12/18) -- ptosi ston wall-clock fallback" %
              env_h)

    # ---- fallback (v2, AMETABLITO) -- MONO gia mi-scheduled triggers ----
    today = now.date()
    yst = today - dt.timedelta(days=1)
    candidates = [
        (dt.datetime(today.year, today.month, today.day, h, m,
                      tzinfo=dt.timezone.utc), today, cyc)
        for h, m, cyc in CRON_SLOTS_UTC
    ]
    # + to teleftaio slot tis PROIGOUMENIS meras, gia tin periptosi pou to
    # 'now' ine PRIN to proto simerino slot (02:40) -- px execution 01:00Z.
    h, m, cyc = CRON_SLOTS_UTC[-1]
    candidates.append((dt.datetime(yst.year, yst.month, yst.day, h, m,
                                    tzinfo=dt.timezone.utc), yst, cyc))
    past = [c for c in candidates if c[0] <= now]
    _slot_dt, cyc_date, cyc_hour = max(past, key=lambda c: c[0])
    return dt.datetime(cyc_date.year, cyc_date.month, cyc_date.day, cyc_hour,
                        0, tzinfo=dt.timezone.utc)


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
    locked_run = None
    expected_run = expected_cycle_utc()   # Alex 28/08/26: pin, oxi 'latest'

    # ---- AUTO: KAMIA try/except -- failure edo = kanoniko workflow
    #      failure, akrivos opos simera. ----
    for i, step in enumerate(STEPS):
        run_dt = process_one(step, expected_run=expected_run)
        if i == 0:
            locked_run = run_dt   # MONO to PROTO step kleidonei to run

    # ---- FORECAST: xoristo, DEN epireazei to AUTO commit ----
    write_status("failure")   # fail-safe default PRIN xekinisei to forecast
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


if __name__ == "__main__":
    main()