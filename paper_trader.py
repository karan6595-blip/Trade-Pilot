"""TradePilot paper trader v3: real lots, Black-Scholes option prices, fixed daily capital."""
import sys, json, time, datetime as dt
from math import log, sqrt, exp, erf
from zoneinfo import ZoneInfo
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
CFG = dict(
    symbols={"NIFTY 50": "^NSEI", "BANK NIFTY": "^NSEBANK"},
    lot={"NIFTY 50": 65, "BANK NIFTY": 30},      # NSE lots from Jan 2026; VERIFY
    step={"NIFTY 50": 50, "BANK NIFTY": 100},
    iv={"NIFTY 50": 0.13, "BANK NIFTY": 0.15},   # ASSUMED implied volatility
    rate=0.065,
    daily_capital=30000,     # same amount for every trade; profit/loss kept aside
    stop_pct=25, target_pct=37, slip_pct=1,
    brokerage=40, var_charge_pct=0.2,            # ASSUMED; VERIFY on Groww
    or_end="09:45", entry_end="11:00", stop_all="11:15",
    confirm_closes=2, buffer_pct=0.03, max_trades=2, max_consec_losses=2)
F = "data/state.json"

def now(): return dt.datetime.now(IST)
def hm(s): h, m = map(int, s.split(":")); return dt.time(h, m)
def N(x): return 0.5 * (1 + erf(x / sqrt(2)))

def bs(S, K, T, sig, call, r):
    T = max(T, 1e-6); d1 = (log(S / K) + (r + sig * sig / 2) * T) / (sig * sqrt(T)); d2 = d1 - sig * sqrt(T)
    return S * N(d1) - K * exp(-r * T) * N(d2) if call else K * exp(-r * T) * N(-d2) - S * N(-d1)

def expiry(n, day, t):
    def end(d): return dt.datetime.combine(d, dt.time(15, 30), tzinfo=IST)
    if n == "NIFTY 50":                       # weekly, Tuesday
        d = day + dt.timedelta(days=(1 - day.weekday()) % 7)
        return end(d + dt.timedelta(days=7)) if end(d) <= t else end(d)
    def last_tue(y, m):                       # BANK NIFTY: monthly, last Tuesday
        d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        while d.weekday() != 1: d -= dt.timedelta(days=1)
        return d
    d = last_tue(day.year, day.month)
    return end(last_tue(day.year + (day.month == 12), day.month % 12 + 1)) if end(d) <= t else end(d)

def fetch(sym):
    for _ in range(3):
        try:
            df = yf.Ticker(sym).history(period="7d", interval="1m")
            if df is not None and not df.empty:
                df.index = df.index.tz_convert(IST); return df
        except Exception:
            pass
        time.sleep(3)
    return None

def load():
    try: return json.load(open(F))
    except Exception: return {"sessions": []}

def save(st):
    st["config"] = CFG; st["updated"] = now().isoformat()
    json.dump(st, open(F, "w"), indent=1, default=str)

def replay(day, frames):
    C = CFG; slip = C["slip_pct"] / 100
    s = dict(date=str(day), method="replay-v3", trades=[], notes=[])
    rng = {}
    for n, d in frames.items():
        o = d[d.index.time < hm(C["or_end"])]
        if len(o) >= 10: rng[n] = (float(o.High.max()), float(o.Low.min()))

    def value(p, S, t):
        T = (p["expiry"] - t).total_seconds() / (365 * 86400)
        return bs(S, p["strike"], T, C["iv"][p["inst"]], p["dir"] == 1, C["rate"])

    def close(p, prem, why, S, t1):
        q = p["lots"] * C["lot"][p["inst"]]
        net = q * (prem - p["entry_prem"]) - C["brokerage"] - q * (prem + p["entry_prem"]) * C["var_charge_pct"] / 100
        p.update(exit_prem=round(prem, 2), exit_time=t1.strftime("%H:%M"), reason=why,
                 points=round((S - p["spot_in"]) * p["dir"], 2), est_rupees=round(net, 2))
        return p

    ts = sorted(set().union(*[set(d.index) for d in frames.values()]))
    pos, consec, last = None, 0, {}
    for t in ts:
        tt, t1 = t.time(), t + dt.timedelta(minutes=1)
        for n, d in frames.items():
            if t in d.index: last[n] = float(d.Close[t])
        if tt < hm(C["or_end"]) or tt >= hm(C["stop_all"]): continue
        if pos:
            d = frames[pos["inst"]]
            if t not in d.index: continue
            b, sg = d.loc[t], pos["dir"]
            Sbad = float(b.Low if sg == 1 else b.High); Sgood = float(b.High if sg == 1 else b.Low)
            bad = value(pos, Sbad, t1) * (1 - slip); good = value(pos, Sgood, t1) * (1 - slip)
            if bad <= pos["stop_prem"]: tr = close(pos, pos["stop_prem"], "stop", Sbad, t1)
            elif good >= pos["target_prem"]: tr = close(pos, pos["target_prem"], "target", Sgood, t1)
            elif tt >= hm("11:14"):
                cl = float(b.Close); tr = close(pos, value(pos, cl, t1) * (1 - slip), "time", cl, t1)
            else: continue
            s["trades"].append(tr); consec = consec + 1 if tr["est_rupees"] < 0 else 0; pos = None
        elif (len(s["trades"]) < C["max_trades"] and consec < C["max_consec_losses"]
              and tt < hm(C["entry_end"])):
            for n, (hi, lo) in rng.items():
                d = frames[n]
                if t not in d.index: continue
                c = d.Close[d.index <= t].tail(C["confirm_closes"]).astype(float).tolist()
                if len(c) < C["confirm_closes"]: continue
                buf = hi * C["buffer_pct"] / 100
                dr = 1 if all(x > hi + buf for x in c) else -1 if all(x < lo - buf for x in c) else 0
                if not dr: continue
                S = c[-1]; K = round(S / C["step"][n]) * C["step"][n]
                p = dict(inst=n, dir=dr, side="CE" if dr == 1 else "PE", strike=K, spot_in=round(S, 2),
                         expiry=expiry(n, t.date(), t1))
                prem = round(value(p, S, t1) * (1 + slip), 2)
                lots = max(1, int(C["daily_capital"] // (prem * C["lot"][n])))
                if lots < 1:
                    if not any(x.startswith(n + " signal skipped") for x in s["notes"]):
                        s["notes"].append(f"{n} signal skipped: 1 lot costs about ₹{prem * C['lot'][n]:,.0f}")
                    continue
                p.update(lots=lots, entry_prem=prem, entry_time=t1.strftime("%H:%M"),
                         stop_prem=prem * (1 - C["stop_pct"] / 100), target_prem=prem * (1 + C["target_pct"] / 100))
                pos = p; break
    if pos:
        cl = last.get(pos["inst"], pos["spot_in"])
        s["trades"].append(close(pos, value(pos, cl, ts[-1]) * (1 - slip), "data end", cl, ts[-1]))
        s["notes"].append("data ended early")
    s["net_rupees"] = round(sum(x["est_rupees"] for x in s["trades"]), 2)
    s["net_points"] = round(sum(x["points"] for x in s["trades"]), 2)
    return s

def run(mode):
    got = {}
    for n, sy in CFG["symbols"].items():
        df = fetch(sy)
        if df is not None: got[n] = df
    if not got: print("no data"); return
    st = load(); today = now().date()
    for day in sorted({x.date() for df in got.values() for x in df.index}):
        if mode != "backfill" and day != today: continue
        fr = {n: df[df.index.date == day] for n, df in got.items()}
        fr = {n: f for n, f in fr.items() if len(f) and f.index[-1].time() >= hm("11:14")
              and f.index[0].time() <= hm("09:20")}
        if not fr: continue
        s = replay(day, fr)
        st["sessions"] = [x for x in st["sessions"] if x["date"] != s["date"]] + [s]
        print(s["date"], s["net_rupees"], len(s["trades"]))
    st["sessions"].sort(key=lambda x: x["date"]); save(st)

def check():
    out = {}
    for n, sy in CFG["symbols"].items():
        d = fetch(sy)
        if d is None: out[n] = "no data"; continue
        out[n] = {"last_bar": d.index[-1].isoformat(), "lag_minutes": round((now() - d.index[-1]).total_seconds() / 60, 1)}
    st = load(); st["last_check"] = {"at": now().isoformat(), "result": out}; save(st); print(out)

if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "session"
    check() if m == "check" else run(m)
