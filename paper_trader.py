"""TradePilot paper trader. Simulation only: no broker keys, no orders.
Opening-range breakout on NIFTY 50 / BANK NIFTY using free Yahoo Finance 1-minute data."""
import sys, json, time, subprocess as sp, datetime as dt
from zoneinfo import ZoneInfo
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
CFG = dict(
    symbols={"NIFTY 50": "^NSEI", "BANK NIFTY": "^NSEBANK"},
    lot={"NIFTY 50": 75, "BANK NIFTY": 30},   # VERIFY lot sizes on nseindia.com
    delta=0.5, charges_per_trade=60,
    or_end="09:45", entry_end="11:00", stop_all="11:15",
    confirm_closes=2, buffer_pct=0.03, rr=1.5, max_trades=2, max_consec_losses=2,
    poll_sec=30, max_lag_min=5)
F = "data/state.json"

def now(): return dt.datetime.now(IST)
def hm(s): h, m = map(int, s.split(":")); return dt.time(h, m)

def bars(sym):
    for _ in range(3):
        try:
            df = yf.Ticker(sym).history(period="1d", interval="1m")
            if df is not None and not df.empty:
                df.index = df.index.tz_convert(IST)
                return df
        except Exception:
            pass
        time.sleep(3)
    return None

def load():
    try: return json.load(open(F))
    except Exception: return {"config": CFG, "sessions": []}

def save(st):
    st["config"] = CFG; st["updated"] = now().isoformat()
    json.dump(st, open(F, "w"), indent=1, default=str)

def put(s):
    st = load()
    st["sessions"] = [x for x in st["sessions"] if x["date"] != s["date"]] + [s]
    save(st)

def publish():
    for c in (["git", "config", "user.name", "tradepilot-bot"],
              ["git", "config", "user.email", "bot@users.noreply.github.com"],
              ["git", "add", "data"], ["git", "commit", "-m", "progress", "-q"],
              ["git", "pull", "--rebase", "--autostash", "-q"], ["git", "push", "-q"]):
        sp.run(c, check=False)

def check():
    out = {}
    for n, s in CFG["symbols"].items():
        d = bars(s)
        if d is None: out[n] = "no data"; continue
        last = d.index[-1]
        out[n] = {"last_bar": last.isoformat(), "close": round(float(d.Close.iloc[-1]), 2),
                  "lag_minutes": round((now() - last).total_seconds() / 60, 1)}
    st = load(); st["last_check"] = {"at": now().isoformat(), "result": out}; save(st); print(out)

def finish(pos, px, reason):
    pts = (px - pos["entry"]) * pos["dir"]
    est = pts * CFG["delta"] * CFG["lot"][pos["inst"]] - CFG["charges_per_trade"]
    pos.update(exit=round(px, 2), exit_time=now().strftime("%H:%M:%S"), reason=reason,
               points=round(pts, 2), est_rupees_one_lot=round(est, 0))
    return pos

def session():
    st = load(); today = now().date().isoformat()
    if any(x["date"] == today and x["status"] == "completed" for x in st["sessions"]):
        print("already completed today"); return
    if now().time() >= hm(CFG["stop_all"]):
        print("too late for today"); return
    s = {"date": today, "status": "running", "trades": [], "max_lag_min": 0, "notes": []}
    while now().time() < hm("09:15"): time.sleep(20)
    pos, consec, closed, ranges, last_px, last_pub = None, 0, 0, {}, {}, time.time()
    try:
        while now().time() < hm(CFG["stop_all"]):
            try:
                data = {n: bars(sy) for n, sy in CFG["symbols"].items()}
                live = {n: d for n, d in data.items()
                        if d is not None and d.index[-1].date() == now().date()}
                if not live:
                    s["empty_polls"] = s.get("empty_polls", 0) + 1
                    if s["empty_polls"] >= 10 and not last_px:
                        s["status"] = "market closed or no data today"; break
                else:
                    for n, d in live.items():
                        last_px[n] = float(d.Close.iloc[-1])
                        lag = (now() - d.index[-1]).total_seconds() / 60
                        s["max_lag_min"] = round(max(s["max_lag_min"], lag), 1)
                    t = now().time()
                    if t >= hm(CFG["or_end"]):
                        for n, d in live.items():
                            if n in ranges: continue
                            o = d[d.index.time < hm(CFG["or_end"])]
                            if len(o): ranges[n] = (float(o.High.max()), float(o.Low.min()))
                    if pos:
                        if pos["inst"] in live:
                            px = last_px[pos["inst"]]; sgn = pos["dir"]
                            hit = ("stop" if (px - pos["stop"]) * sgn <= 0 else
                                   "target" if (px - pos["target"]) * sgn >= 0 else None)
                            if hit:
                                tr = finish(pos, px, hit); s["trades"].append(tr); closed += 1
                                consec = consec + 1 if tr["est_rupees_one_lot"] < 0 else 0
                                pos = None; put(s)
                    elif (closed < CFG["max_trades"] and consec < CFG["max_consec_losses"]
                          and hm(CFG["or_end"]) <= t < hm(CFG["entry_end"])):
                        for n, (hi, lo) in ranges.items():
                            if n not in live: continue
                            c = live[n].Close.tail(CFG["confirm_closes"]).astype(float).tolist()
                            if len(c) < CFG["confirm_closes"]: continue
                            buf = hi * CFG["buffer_pct"] / 100
                            dr = 1 if all(x > hi + buf for x in c) else -1 if all(x < lo - buf for x in c) else 0
                            if dr:
                                e = c[-1]; stop = (hi + lo) / 2
                                pos = dict(inst=n, dir=dr, side="CE" if dr == 1 else "PE",
                                           entry=round(e, 2), stop=round(stop, 2),
                                           target=round(e + CFG["rr"] * (e - stop), 2),
                                           entry_time=now().strftime("%H:%M:%S"),
                                           or_high=round(hi, 2), or_low=round(lo, 2))
                                break
            except Exception as e:
                s["notes"] = (s["notes"] + ["error: " + repr(e)[:150]])[-8:]
            if time.time() - last_pub > 600:
                put(s); publish(); last_pub = time.time()
            time.sleep(CFG["poll_sec"])
        if s["status"] == "running": s["status"] = "completed"
    finally:
        if s["status"] == "running": s["status"] = "interrupted"
        if pos:
            px = last_px.get(pos["inst"], pos["entry"])
            s["trades"].append(finish(pos, px, "session end"))
        if s["max_lag_min"] > CFG["max_lag_min"]:
            s["notes"].append("Data was delayed: treat results as invalid.")
        s["net_est_rupees_one_lot"] = sum(x["est_rupees_one_lot"] for x in s["trades"])
        s["net_points"] = round(sum(x["points"] for x in s["trades"]), 2)
        put(s)

if __name__ == "__main__":
    check() if (len(sys.argv) > 1 and sys.argv[1] == "check") else session()
