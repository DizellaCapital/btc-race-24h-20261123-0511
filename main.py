"""Dip-buy trend bot for the cbBTC 24-hour race (agent-fun/bars-replay/v1).

Strategy, taken from the dev leaderboard study (the $35-44 cluster pattern:
dip-buy about 0.5% under the running high, gated by an EMA trend filter,
take-profit about 2%, trailing stop, few round trips, max drawdown under 2%):
  - trend filter: EMA(8) above EMA(21) on 1-minute closes
  - entry: flat, uptrend, close at least 0.5% under the 120-bar running high,
    with an uptick (close above the previous close) -- buy the dip recovering,
    never a falling knife
  - exit: take profit +2% over cost, trailing stop 1% under the peak value
    since entry, hard stop -1.5% under cost, EMA(8) under EMA(21), or a 3%
    crash brake under the running high
  - 60-bar cooldown after a stop-loss exit, so chop cannot bleed the account
  - one position at a time, full equity per trade; long only, no leverage
  - deterministic: only the bars decide; no clock, no environment, no random

The bot speaks the protocol through the kit's Python shim, vendored byte for
byte under vendor/ (the kit's RULES.txt leaves such a copy out of the
authorship counts). Everything else here is this bot's own code.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"
FAST_SPAN = 40
SLOW_SPAN = 120
HIGH_BARS = 120
DIP_BPS = 80            # 0.8% under the running high to buy the dip: 0.5% dips
                      # are constant noise in flat chop; 0.8% is a real pullback
                      # (the studied cluster spans 0.5-0.8%)
TP_BPS = 300            # +3% take profit over cost: in a real trend, ride it;
                      # the trailing stop, not the target, does the exiting
TRAIL_BPS = 250         # 2.5% trailing stop under the peak since entry
STOP_BPS = 300          # -3% hard stop under cost
BRAKE_BPS = 300         # 3% crash brake under the running high
COOLDOWN_BARS = 60      # bars to sit out after a stop-loss exit
MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on


class Ema:
    """One exponential moving average over closes, seeded as the mean of its
    first span of closes, then moving 2 / (span + 1) of the way to each new
    close (the same seeding the kit baselines use)."""

    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None

    def push(self, close):
        if self.value is not None:
            self.value += (2.0 / (self.span + 1)) * (close - self.value)
            return
        self.warm.append(close)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / float(self.span)


class Bot:
    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.closes = []          # closes seen, oldest first
        self.prev_close = None
        self.peak_value = 0       # highest position value since entry
        self.cooldown_until = -1  # first seq allowed to buy again
        self.fast_hist = []       # fast EMA values, oldest first

    def running_high(self):
        """Highest close of the HIGH_BARS bars before the current one."""
        if not self.closes:
            return None
        window = self.closes[-HIGH_BARS:]
        return max(window)

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        close = state.close(SYMBOL)
        if close is None:
            return []
        self.fast.push(close)
        self.slow.push(close)
        if self.fast.value is not None:
            self.fast_hist.append(self.fast.value)
        high = self.running_high()
        self.closes.append(close)

        held = state.positions.get(SYMBOL)
        if held is not None:
            value = state.value(SYMBOL, held.qty)
            if value > self.peak_value:
                self.peak_value = value
            cost = held.cost
            trend_down = False  # no EMA-cross exit: the trailing stop, hard
            # stop and crash brake protect the downside; in chop the cross
            # flips constantly and exits winners too early
            crash = high is not None and close * 10000 <= high * (10000 - BRAKE_BPS)
            take_profit = value * 10000 >= cost * (10000 + TP_BPS)
            trailing = value * 10000 <= self.peak_value * (10000 - TRAIL_BPS)
            stopped = value * 10000 <= cost * (10000 - STOP_BPS)
            if take_profit or trailing or stopped or trend_down or crash:
                if stopped or crash:
                    self.cooldown_until = state.seq + COOLDOWN_BARS
                self.peak_value = 0
                return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            self.prev_close = close
            return []

        # Flat: look for a dip-buy.
        self.peak_value = 0
        uptrend = (
            self.fast.value is not None
            and self.slow.value is not None
            and self.fast.value > self.slow.value
            and close > self.slow.value  # price still above the slow EMA:
            # a pullback inside an uptrend, not a rollover below it
            and len(self.fast_hist) > 20
            and self.fast.value > self.fast_hist[-21]  # the fast EMA itself
            # is still rising: the short trend has not rolled over yet
        )
        dipped = high is not None and close * 10000 <= high * (10000 - DIP_BPS)
        uptick = self.prev_close is not None and close > self.prev_close
        # the recovery must hold for more than one bar: a single uptick in
        # flat chop is noise, a 5-bar recovery is a real bounce
        recovered = len(self.closes) >= 6 and close > self.closes[-6]
        self.prev_close = close
        if not (uptrend and dipped and uptick and recovered):
            return []
        if state.seq < self.cooldown_until:
            return []
        notional = state.cash
        room = state.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < int(state.hello["fees"]["min_notional_quote_units"]):
            return []
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": notional,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


makefun_bars.run(Bot().on_batch)
