"""BTC race bot v6: core + shallow (calm filter) + washout + scout.

Trades CBBTC against USDC, one-minute bars, paper capital $1,000.
Pure standard library, integer arithmetic only, fully deterministic:
the same bars always produce byte-identical replies. No clock, no
network, no files, no randomness. Speaks agent-fun/bars-replay/v1.

Structure: one tiny core position ($5) unlocks trading, then three
independent one-leg sleeves:
  shallow  - catches one small dip that recovers (dev-gate leg),
             only when the market is calm (60-min drop under 100 bps).
  washout  - waits for one big capitulation (60-min drop >= 250 bps),
             buys with all remaining cash, sells into the snap-back.
  scout    - NEW in v6: after a 200 bps 60-minute drop, buys a small
             30%-of-cash leg to catch the bounce (TP +100 bps).
"""

import json
import sys

SYMBOL = "CBBTC"
BPS = 10000

CORE_BPS = 50
CORE_SLIPPAGE_BPS = 300

SHALLOW_SIZE_BPS = 6000
SHALLOW_DIP_MIN_BPS = 50
SHALLOW_DIP_MAX_BPS = 200
SHALLOW_HIGH_BPS = 400
SHALLOW_CALM_BPS = 100
SHALLOW_TP_BPS = 150
SHALLOW_SL_BPS = 60
SHALLOW_HOLD_BARS = 90
SHALLOW_REST_BARS = 10

WASHOUT_SIZE_BPS = 10000
WASHOUT_DROP_BPS = 250
WASHOUT_TP_BPS = 350
WASHOUT_SL_BPS = 200
WASHOUT_HOLD_BARS = 120

SCOUT_SIZE_BPS = 3000
SCOUT_DROP_BPS = 200
# Scout take-profit +100 bps: bounces after a 200 bps drop peak at
# +119..+165 bps before fading, so +100 banks most of the move.
# (Tested +150 on dev-1444: net +$0.99 vs +$2.67 at +100, so +100 kept.)
SCOUT_TP_BPS = 100
SCOUT_SL_BPS = 60
SCOUT_HOLD_BARS = 90

CRASH_GUARD_BPS = 300
DD_BLOCK_BPS = 1800
DD_FLUSH_BPS = 2000
BUY_SLIPPAGE_BPS = 100
SELL_SLIPPAGE_BPS = 300


class Sleeve:
    """One independent leg: qty held, cost actually paid, bookkeeping."""

    __slots__ = ("qty", "cost", "buy_bar", "legs", "last_close_bar", "inflight")

    def __init__(self):
        self.qty = 0
        self.cost = 0
        self.buy_bar = 0
        self.legs = 0
        self.last_close_bar = None
        self.inflight = None


def main():
    price_scale = 0
    qty_scale = 0
    quote_scale = 0
    min_notional = 0
    denom = 1
    quote_mult = 1

    closes = []
    bar_idx = -1
    cash = 0
    equity = 0
    peak = 0
    core_filled = False
    core_qty = 0
    core_inflight = None
    sleeves = {"shallow": Sleeve(), "washout": Sleeve(), "scout": Sleeve()}
    pending = {}

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mtype = msg.get("type")
        if mtype == "hello":
            sym = msg["symbols"][0]
            price_scale = int(sym["price_scale"])
            qty_scale = int(sym["qty_scale"])
            quote_scale = int(msg["capital"]["quote_scale"])
            min_notional = int(msg["fees"]["min_notional_quote_units"])
            denom = 10 ** (qty_scale + price_scale)
            quote_mult = 10 ** quote_scale
            continue
        if mtype == "done":
            break
        if mtype != "batch":
            continue
        bar_idx += 1

        # 1. Settle fills reported in this batch.
        for fill in msg.get("fills") or []:
            cid = fill.get("client_id")
            info = pending.pop(cid, None)
            if info is None:
                continue
            name, kind = info
            if fill.get("status") != "filled":
                if name == "core":
                    core_inflight = None
                else:
                    sleeves[name].inflight = None
                continue
            if kind == "buy":
                if name == "core":
                    core_filled = True
                    core_qty = int(fill["qty"])
                    core_inflight = None
                else:
                    s = sleeves[name]
                    s.qty = int(fill["qty"])
                    s.cost = int(fill["notional_quote_units"])
                    s.legs += 1
                    s.inflight = None
            else:
                s = sleeves[name]
                s.qty = 0
                s.cost = 0
                s.last_close_bar = bar_idx
                s.inflight = None

        # 2. Cash and equity come from the account; track the peak.
        acct = msg.get("account") or {}
        cash = int(acct.get("cash_quote_units", cash))
        equity = int(acct.get("equity_quote_units", equity))
        if equity > peak:
            peak = equity

        # Find this bar's CBBTC close, if the batch carries one.
        close = None
        for bar in msg.get("bars") or []:
            if bar.get("symbol") == SYMBOL:
                close = int(bar["c"])
                break

        orders = []
        if close is not None:
            # 3. Record the close.
            closes.append(close)

            def drawdown_bps():
                if peak <= 0:
                    return 0
                return (peak - equity) * BPS // peak

            def sma(n):
                if len(closes) < n:
                    return None
                return sum(closes[-n:]) // n

            def drop60_bps():
                if len(closes) < 61:
                    return None
                old = closes[-61]
                return (old - closes[-1]) * BPS // old

            def crash_guard_ok():
                if len(closes) < 120:
                    return True
                avg = sum(closes[-120:]) // 120
                return (avg - closes[-1]) * BPS // avg <= CRASH_GUARD_BPS

            def send_buy(name, sleeve, size_bps, slippage):
                notional = cash * size_bps // BPS
                if notional < min_notional:
                    return False
                cid = "%s-buy-%d" % (name, bar_idx)
                orders.append({"side": "buy", "client_id": cid,
                               "symbol": SYMBOL,
                               "notional_quote_units": str(notional),
                               "max_slippage_bps": slippage})
                pending[cid] = (name, "buy")
                if sleeve is None:
                    return True
                sleeve.buy_bar = bar_idx
                sleeve.inflight = cid
                return True

            # 4. Core first: one $5 buy, retry until filled, nothing else.
            if not core_filled and core_inflight is None:
                if send_buy("core", None, CORE_BPS, CORE_SLIPPAGE_BPS):
                    core_inflight = "core-buy-%d" % bar_idx
            else:
                # 5. Exits first: every open leg that meets an exit sells.
                exit_specs = (
                    ("shallow", SHALLOW_TP_BPS, SHALLOW_SL_BPS,
                     SHALLOW_HOLD_BARS),
                    ("washout", WASHOUT_TP_BPS, WASHOUT_SL_BPS,
                     WASHOUT_HOLD_BARS),
                    ("scout", SCOUT_TP_BPS, SCOUT_SL_BPS, SCOUT_HOLD_BARS),
                )
                for name, tp, sl, hold in exit_specs:
                    s = sleeves[name]
                    if s.qty <= 0 or s.inflight is not None:
                        continue
                    value = s.qty * close * quote_mult // denom
                    if (value * BPS >= s.cost * (BPS + tp)
                            or value * BPS <= s.cost * (BPS - sl)
                            or bar_idx - s.buy_bar >= hold
                            or drawdown_bps() > DD_FLUSH_BPS):
                        cid = "%s-sell-%d" % (name, bar_idx)
                        orders.append({"side": "sell", "client_id": cid,
                                       "symbol": SYMBOL, "qty": str(s.qty),
                                       "max_slippage_bps": SELL_SLIPPAGE_BPS})
                        s.inflight = cid
                        pending[cid] = (name, "sell")

                # 6. Entries, at most one buy per bar, in priority order:
                # washout first, then scout, then shallow.
                if not orders and drawdown_bps() <= DD_BLOCK_BPS:
                    d60 = drop60_bps()
                    wash = sleeves["washout"]
                    scout = sleeves["scout"]
                    shal = sleeves["shallow"]
                    if (core_filled and wash.qty <= 0
                            and wash.inflight is None and wash.legs == 0
                            and d60 is not None and d60 >= WASHOUT_DROP_BPS
                            and crash_guard_ok()
                            and cash >= min_notional):
                        send_buy("washout", wash, WASHOUT_SIZE_BPS,
                                 BUY_SLIPPAGE_BPS)
                    elif (core_filled and scout.qty <= 0
                            and scout.inflight is None and scout.legs == 0
                            and d60 is not None and d60 >= SCOUT_DROP_BPS
                            and crash_guard_ok()
                            and cash >= min_notional):
                        send_buy("scout", scout, SCOUT_SIZE_BPS,
                                 BUY_SLIPPAGE_BPS)
                    elif core_filled and shal.qty <= 0 \
                            and shal.inflight is None and shal.legs == 0:
                        avg4 = sma(4)
                        dip = None
                        if avg4 is not None:
                            dip = (avg4 - close) * BPS // avg4
                        high = max(closes[-480:])
                        high_drop = (high - close) * BPS // high
                        rest_ok = (shal.last_close_bar is None
                                   or bar_idx - shal.last_close_bar
                                   >= SHALLOW_REST_BARS)
                        calm_ok = d60 is None or d60 < SHALLOW_CALM_BPS
                        if (dip is not None
                                and SHALLOW_DIP_MIN_BPS <= dip
                                <= SHALLOW_DIP_MAX_BPS
                                and high_drop >= SHALLOW_HIGH_BPS
                                and crash_guard_ok() and calm_ok and rest_ok
                                and cash >= min_notional):
                            send_buy("shallow", shal, SHALLOW_SIZE_BPS,
                                     BUY_SLIPPAGE_BPS)

        out.write(json.dumps({"type": "reply", "seq": msg["seq"],
                              "orders": orders}) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
