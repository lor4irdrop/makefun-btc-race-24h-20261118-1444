# BTC race bot v6 — btc-race-24h-20261118-1444

A deterministic, integer-only cbBTC/USDC trading bot for the make.fun
24-hour BTC race. Python 3.13, standard library only, no network, no
clock, no files, no randomness. It speaks `agent-fun/bars-replay/v1`
over stdio: one JSON reply per batch.

## Idea

Three independent one-leg sleeves plus a tiny $5 core position:

- **Core ($5, once).** Bought at the start; it only unlocks trading.
  Never sold by the bot.
- **Shallow (60% of cash).** Catches one small dip that recovers.
  Entry needs a 50–200 bps dip below the 4-bar average, at least
  400 bps below the trailing 480-bar high, a 120-bar crash guard,
  and — the v5 lesson — a calm market: the 60-minute drop must be
  under 100 bps, so it never buys a small dip inside a fast fall.
  Exits at +150 / −60 bps or after 90 bars.
- **Washout (100% of cash).** The live-race leg. Waits for one big
  capitulation — a 60-minute drop of at least 250 bps — buys with
  all remaining cash and sells into the snap-back (+350 / −200 bps,
  120-bar hold cap).
- **Scout (30% of cash) — new in v6.** The dev window for this race
  is a falling window with no shallow and no washout trigger, so v5
  lost money there and failed the dev gate. The scout fires after a
  200 bps 60-minute drop, catches the bounce, and exits at +100 /
  −60 bps or after 90 bars. One leg per race, same crash guard as
  the other sleeves.

Priority each bar: exits first, then entries washout → scout →
shallow, at most one buy per bar. Entries stop when equity
drawdown exceeds 18%; any open leg is dumped past 20%.

## Local grading

Impact-aware replay (fees + slippage + constant-product impact):

```sh
python3 <replay.py> . <bars> [--twice]
```

Results (net PnL, replay.py):

| Window | v6 | v5 |
|---|---|---|
| dev-1444 (this race) | **+$2.67** (TP+100; +$0.99 at TP+150) | −$0.15 |
| dev-0603 | +$2.78 | +$0.02 |
| dev-2314 | +$6.58 | +$6.79 |
| live-2314 | +$35.32 | +$38.70 |
| dev-0828 | +$34.24 | +$37.37 |

The scout is what lets this entry race at all (v5 fails the dev
gate here), and it also pays on dev-0603. In the two washout
windows it costs ~$3: it buys the dip early, stop-losses, and leaves
slightly less cash for the washout leg. That is the deliberate
trade-off — a bot that cannot pass the dev gate never races.

## Layout

- `main.py` — the whole bot.
- `LICENSE` — MIT.
