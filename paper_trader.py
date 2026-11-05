"""TradePilot paper trader. Simulation only: holds no broker keys, places no orders.
Opening-range breakout on NIFTY 50 / BANK NIFTY using free Yahoo Finance 1-minute data."""
import sys, json, time, datetime as dt
from zoneinfo import ZoneInfo
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
CFG = dict(
    symbols={"NIFTY 50": "^NSEI", "BANK NIFTY": "^NSEBANK"},
    lot={"NIFTY 50": 75, "BANK NIFTY": 30},   # VERIFY current lot sizes on nseindia.com
    delta=0.5,              # assumed option delta for ATM option (estimate only)
    charges_per_trade=60,   # rough round-trip brokerage+taxes in rupees (estimate)
    or_end="09:45", entry_end="11:00", stop_all="11:15",
    confirm_closes=2, buffer_pct=0.03, rr=1.5, max_trades=2, max_consec_losses=2,
    poll_sec=30, max_lag_min=5)
F = "data/state.json"

def now(): return dt.datetime.now(IST)
def hm(s): h, m = map(int, s.split(":")); return dt.time(h, m)

def bars(sym):
    df = yf.Ticker(sym).history(period="1d", interval="1m")
    if df.empty: return None
    df.index = df.index.tz_convert(IST)
    return df

def load():
    try: return json.load(open(F))
    except Exception: return {"config": CFG, "sessions": []}

def save(st):
    st["config"] = CFG; st["updated"] = now().isoformat()
    json.dump(st, open(F, "w"), indent=1, default=str)

def check():
    out = {}
    for n, s in CFG["symbols"].items():
        d = bars(s)
        if d is None: out[n] = "no data"; continue
        last = d.index[-1]
        out[n] = {"last_bar": last.isoformat(), "close": round(float(d.Close.iloc[-1]), 2),
                  "lag_minutes": round((now() - last).total_seconds() / 60, 1)}
    st = load(); st["last_check"] = {"at": now().isoformat(), "result": out}; save(st); print(out)

def session():
    st = load(); today = now().date().isoformat()
    s = {"date": today, "status": "running", "trades": [], "max_lag_min": 0, "notes": []}
    while now().time() < hm("09:15"): time.sleep(20)
    pos, consec, closed = None, 0, 0
    ranges = {}
    try:
        while now().time() < hm(CFG["stop_all"]):
            data = {n: bars(sy) for n, sy in CFG["symbols"].items()}
            if all(d is None or d.index[-1].date() != now().date() for d in data.values()):
                s["status"] = "market closed or no data today"; break
            for n, d in data.items():
                if d is None: continue
                lag = (now() - d.index[-1]).total_seconds() / 60
                s["max_lag_min"] = round(max(s["max_lag_min"], lag), 1)
            t = now().time()
            if t >= hm(CFG["or_end"]):
                for n, d in data.items():
                    if n in ranges or d is None: continue
                    o = d[d.index.time < hm(CFG["or_end"])]
                    if len(o): ranges[n] = (float(o.High.max()), float(o.Low.min()))
            if pos:
                d = data[pos["inst"]]; px = float(d.Close.iloc[-1]); sgn = pos["dir"]
                hit = "stop" if (px - pos["stop"]) * sgn <= 0 else "target" if (px - pos["target"]) * sgn >= 0 else None
                if hit or t >= hm(CFG["stop_all"]):
                    pts = (px - pos["entry"]) * sgn
                    est = pts * CFG["delta"] * CFG["lot"][pos["inst"]] - CFG["charges_per_trade"]
                    pos.update(exit=round(px, 2), exit_time=now().strftime("%H:%M:%S"), reason=hit or "time",
                               points=round(pts, 2), est_rupees_one_lot=round(est, 0))
                    s["trades"].append(pos); closed += 1; consec = consec + 1 if est < 0 else 0; pos = None
            elif closed < CFG["max_trades"] and consec < CFG["max_consec_losses"] and hm(CFG["or_end"]) <= t < hm(CFG["entry_end"]):
                for n, (hi, lo) in ranges.items():
                    d = data[n]
                    if d is None: continue
                    c = d.Close.tail(CFG["confirm_closes"]).astype(float).tolist()
                    if len(c) < CFG["confirm_closes"]: continue
                    buf = hi * CFG["buffer_pct"] / 100
                    dr = 1 if all(x > hi + buf for x in c) else -1 if all(x < lo - buf for x in c) else 0
                    if dr:
                        e = c[-1]; stop = (hi + lo) / 2
                        pos = dict(inst=n, dir=dr, side="CE" if dr == 1 else "PE", entry=round(e, 2),
                                   stop=round(stop, 2), target=round(e + CFG["rr"] * (e - stop), 2),
                                   entry_time=now().strftime("%H:%M:%S"), or_high=hi, or_low=lo)
                        break
            time.sleep(CFG["poll_sec"])
        if s["status"] == "running": s["status"] = "completed"
    finally:
        if pos: s["notes"].append("open position at end was not closed: " + json.dumps(pos))
        if s["max_lag_min"] > CFG["max_lag_min"]: s["notes"].append("Data was delayed: treat results as invalid.")
        s["net_est_rupees_one_lot"] = sum(x["est_rupees_one_lot"] for x in s["trades"])
        s["net_points"] = round(sum(x["points"] for x in s["trades"]), 2)
        st = load(); st["sessions"].append(s); save(st)

if __name__ == "__main__":
    check() if (len(sys.argv) > 1 and sys.argv[1] == "check") else session()
