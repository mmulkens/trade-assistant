# ---------------------------------------------------------------------------
# runner.py — WalkForwardRunner: the walk-forward simulation orchestrator
#
# Responsibilities:
#   - Enforce state isolation (wf_sim.db ≠ risk.db at startup)
#   - Build and configure all pipeline components for the simulation
#   - Drive the day loop: advance walker → scan → exit → enter → equity record
#   - Route P&L from exits back into portfolio_value for the next day's sizing
#   - Mark open positions as sim_end at the end of the run
#   - Persist the full run record to wf_sim.db for summary analysis
#
# Components used:
#   - DataFrameWalker  — time-bounded cache replacing data_fetcher.cache
#   - SignalEngine     — injected with walker (SE-25) for lookahead-safe scans
#   - RiskLayer        — reused unchanged; pointed at wf_sim.db for isolation
#   - SimPositionManager — gap-aware exits, ATR trail, time-exit
#   - risk_layer.state / position_manager.state — direct DB helpers for
#     stop/trail/peak updates (mirrors what the live Position Manager does)
#
# Key design decisions:
#   - Signals are scanned BEFORE exits so the ranked list is available as
#     signal_queue for the time-exit gate on the same bar.
#   - portfolio_value_stub is kept current by mutating the wf_config dict and
#     the RiskLayer instance (_portfolio_value_stub) after every exit.
#   - RL state tables are cleared at run start so each simulation starts clean.
# ---------------------------------------------------------------------------

from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path

import pandas as pd

from risk_layer.layer import RiskLayer
from risk_layer import state as rl_state
from position_manager import state as pm_state
from signal_engine.engine import SignalEngine
from signal_engine import indicators as ind

from . import storage
from .walker import DataFrameWalker
from .sim_pm import SimPositionManager


class WalkForwardRunner:
    """Drives the walk-forward simulation end-to-end.

    Usage:
        runner = WalkForwardRunner(config, logger)
        run_id = runner.run(tickers)         # blocks until complete
        # inspect wf_sim.db for results
    """

    def __init__(self, config: dict, logger: Logger) -> None:
        self._config = config
        self._logger = logger

        wf = config["walk_forward"]
        self._wf_db_path: str = wf["wf_db_path"]
        self._min_warmup_bars: int = int(wf.get("min_warmup_bars", 200))
        self._min_ticker_days: int = int(wf.get("min_ticker_days", 700))
        self._benchmark: str = config["signal_engine"]["benchmark"]
        self._cache_dir: str = config["data_fetcher"]["cache_dir"]
        self._atr_period: int = int(config["position_manager"]["atr_period"])

        # Fail loudly if someone accidentally points wf_db at the live risk DB
        original_risk_db = Path(config["risk"]["db_path"]).resolve()
        wf_db = Path(self._wf_db_path).resolve()
        assert original_risk_db != wf_db, (
            f"walk_forward.wf_db_path must not equal risk.db_path — got {wf_db}"
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self, tickers: list[str]) -> str:
        """Execute the full simulation. Returns the run_id string."""
        logger = self._logger

        # Build an isolated config where risk.db_path → wf_sim.db.
        # Keeps all RL writes out of the live risk.db.
        wf_config = copy.deepcopy(self._config)
        wf_config["risk"]["db_path"] = self._wf_db_path

        portfolio_value: float = float(wf_config["risk"]["portfolio_value_stub"])

        # --- Init databases ---
        Path(self._wf_db_path).parent.mkdir(parents=True, exist_ok=True)
        rl_state.init_db(self._wf_db_path)    # risk_positions, system_state
        storage.init_db(self._wf_db_path)      # wf_runs, wf_positions, wf_signals, wf_equity_curve

        # Clear RL tables so each run starts from a clean slate
        with sqlite3.connect(self._wf_db_path) as conn:
            conn.execute("DELETE FROM risk_positions")
            conn.execute("DELETE FROM system_state")
            conn.commit()

        # --- Build Walker ---
        all_tickers = list(set(tickers + [self._benchmark]))
        walker = DataFrameWalker(all_tickers, self._cache_dir)

        # --- Filter to eligible universe ---
        eligible = self._eligible_tickers(walker, tickers)
        logger.info({
            "event": "wf_universe",
            "total_tickers": len(tickers),
            "eligible_tickers": len(eligible),
        })

        # --- Determine simulation dates (benchmark-driven) ---
        bench_full = walker._store.get(self._benchmark)
        if bench_full is None:
            raise RuntimeError(f"Benchmark {self._benchmark!r} not in walker cache")

        bench_idx = bench_full.index
        if bench_idx.tz is not None:
            bench_idx = bench_idx.tz_localize(None)

        if len(bench_idx) < self._min_warmup_bars:
            raise RuntimeError(
                f"Benchmark has {len(bench_idx)} bars; need {self._min_warmup_bars} for warmup"
            )

        # Simulation starts after the warmup window
        sim_dates: list[pd.Timestamp] = list(bench_idx[self._min_warmup_bars:])
        if not sim_dates:
            raise RuntimeError("No simulation dates remain after the warmup period")

        sim_start = sim_dates[0].strftime("%Y-%m-%d")
        sim_end   = sim_dates[-1].strftime("%Y-%m-%d")

        # --- Create run record ---
        run_id = storage.create_run(
            db_path=self._wf_db_path,
            sim_start=sim_start,
            sim_end=sim_end,
            portfolio_start=portfolio_value,
        )

        # --- Instantiate pipeline components ---
        signal_engine = SignalEngine(wf_config, logger, cache=walker)
        risk_layer    = RiskLayer(wf_config, logger)
        sim_pm        = SimPositionManager(wf_config)

        # In-memory map: ticker → wf_positions row id for currently open positions.
        # Required because storage.record_position_close / update_position_stop
        # need the wf_positions id, which is not stored in risk_positions.
        ticker_to_wf_id: dict[str, int] = {}
        total_trades = 0

        logger.info({
            "event": "wf_run_started",
            "run_id": run_id,
            "sim_start": sim_start,
            "sim_end": sim_end,
            "sim_days": len(sim_dates),
            "universe_size": len(eligible),
            "portfolio_start": portfolio_value,
        })

        # =======================================================================
        # Day loop
        # =======================================================================
        tob_pct: float = float(wf_config["costs"]["tob_pct"])

        for date in sim_dates:
            date_str = date.strftime("%Y-%m-%d")
            walker.advance(date)

            # ---------------------------------------------------------------
            # Step 1 — Scan for signals (pure computation; no DB writes)
            # Signals are scanned before exits so the ranked list is available
            # as signal_queue for the time-exit gate in step 2.
            # scan() returns signals already ranked (signal_rank=1 is best).
            # ---------------------------------------------------------------
            ranked = signal_engine.scan(eligible)

            # ---------------------------------------------------------------
            # Step 2 — Process exits for all positions open at start of day
            # ---------------------------------------------------------------
            open_positions = rl_state.get_open_positions(self._wf_db_path)

            for pos in open_positions:
                ticker = pos["ticker"]
                df = walker.load(ticker, self._cache_dir)
                if df is None or len(df) < self._atr_period + 1:
                    continue  # no data for today; hold, re-evaluate tomorrow

                today_bar    = df.iloc[-1]
                current_price = float(today_bar["close"])

                # Update peak_price in DB if today set a new high
                current_peak = float(pos.get("peak_price") or pos.get("fill_price") or pos["entry_price"])
                if current_price > current_peak:
                    pm_state.update_peak_price(ticker, current_price, self._wf_db_path)
                    pos["peak_price"] = current_price

                atr_series   = ind.atr(df, self._atr_period)
                atr_value    = float(atr_series.iloc[-1])
                trading_days = self._count_trading_days(pos, date, sim_dates)

                result = sim_pm.evaluate(
                    position=pos,
                    bar=today_bar,
                    signal_queue=ranked,
                    all_open_positions=open_positions,
                    config=wf_config,
                    atr_value=atr_value,
                    trading_days=trading_days,
                )

                if result.closed:
                    risk_layer.close_position(ticker, result.exit_price, result.exit_reason)

                    wf_pos_id = ticker_to_wf_id.get(ticker)
                    if wf_pos_id is not None:
                        storage.record_position_close(
                            db_path=self._wf_db_path,
                            position_db_id=wf_pos_id,
                            exit_date=date_str,
                            exit_price=result.exit_price,
                            exit_commission=result.exit_commission,
                            gross_pnl=result.gross_pnl,
                            net_pnl=result.net_pnl,
                            exit_reason=result.exit_reason,
                        )
                        del ticker_to_wf_id[ticker]

                    portfolio_value += result.net_pnl
                    wf_config["risk"]["portfolio_value_stub"] = portfolio_value
                    risk_layer._portfolio_value_stub = portfolio_value

                    logger.info({
                        "event": "wf_exit",
                        "run_id": run_id,
                        "date": date_str,
                        "ticker": ticker,
                        "reason": result.exit_reason,
                        "exit_price": result.exit_price,
                        "net_pnl": result.net_pnl,
                        "portfolio_value": round(portfolio_value, 2),
                    })

                else:
                    # Persist stop advance or trail activation if state changed
                    new_stop = result.new_stop
                    if new_stop > float(pos["stop_price"]):
                        new_rps      = float(pos["entry_price"]) - new_stop
                        new_risk_amt = max(0.0, int(pos["shares"]) * new_rps)
                        pm_state.update_position_stop(
                            ticker, new_stop, new_rps, new_risk_amt, self._wf_db_path
                        )
                        wf_pos_id = ticker_to_wf_id.get(ticker)
                        if wf_pos_id is not None:
                            storage.update_position_stop(self._wf_db_path, wf_pos_id, new_stop)

                    if result.trail_activated:
                        pm_state.activate_trail(ticker, current_price, self._wf_db_path)

            # ---------------------------------------------------------------
            # Step 3 — Try to enter approved signals
            # ---------------------------------------------------------------
            open_tickers = {p["ticker"] for p in rl_state.get_open_positions(self._wf_db_path)}

            for signal in ranked:
                if signal.ticker in open_tickers:
                    continue  # already open from a prior day

                decision = risk_layer.evaluate(signal)
                action   = "entered" if decision.approved else f"skipped:{decision.reject_reason}"

                storage.record_signal(
                    db_path=self._wf_db_path,
                    run_id=run_id,
                    signal_date=date_str,
                    ticker=signal.ticker,
                    signal_type=signal.signal_type,
                    conviction=signal.conviction,
                    entry_price=signal.entry_price,
                    stop_price=signal.stop_price,
                    signal_rank=signal.signal_rank,
                    action=action,
                )

                if not decision.approved:
                    continue

                fill_price       = signal.entry_price
                entry_commission = round(fill_price * decision.shares * tob_pct / 100, 4)

                risk_layer.open_position(
                    decision=decision,
                    fill_price=fill_price,
                    fill_timestamp=datetime.now(timezone.utc).isoformat(),
                    entry_commission=entry_commission,
                    bot_initiated=True,
                    peak_price=fill_price,
                )

                wf_pos_id = storage.record_position_open(
                    db_path=self._wf_db_path,
                    run_id=run_id,
                    ticker=signal.ticker,
                    entry_date=date_str,
                    entry_price=fill_price,
                    stop_price=signal.stop_price,
                    shares=decision.shares,
                    risk_amount=decision.risk_amount,
                    entry_commission=entry_commission,
                    signal_type=signal.signal_type,
                    conviction=signal.conviction,
                )
                ticker_to_wf_id[signal.ticker] = wf_pos_id
                open_tickers.add(signal.ticker)
                total_trades += 1

                logger.info({
                    "event": "wf_entry",
                    "run_id": run_id,
                    "date": date_str,
                    "ticker": signal.ticker,
                    "fill_price": fill_price,
                    "shares": decision.shares,
                    "risk_amount": decision.risk_amount,
                    "conviction": signal.conviction,
                })

            # ---------------------------------------------------------------
            # Step 4 — Daily equity snapshot
            # ---------------------------------------------------------------
            n_open = len(rl_state.get_open_positions(self._wf_db_path))
            storage.record_equity(self._wf_db_path, run_id, date_str, portfolio_value, n_open)

        # =======================================================================
        # Simulation complete — mark remaining open positions as sim_end
        # =======================================================================
        for pos in rl_state.get_open_positions(self._wf_db_path):
            ticker = pos["ticker"]
            df     = walker.load(ticker, self._cache_dir)
            close_price = float(df.iloc[-1]["close"]) if (df is not None and len(df) > 0) \
                          else float(pos["entry_price"])

            shares      = int(pos["shares"])
            entry_price = float(pos["entry_price"])
            gross_pnl        = round((close_price - entry_price) * shares, 4)
            entry_commission = round(entry_price * shares * tob_pct / 100, 4)
            exit_commission  = round(close_price  * shares * tob_pct / 100, 4)
            net_pnl          = round(gross_pnl - entry_commission - exit_commission, 4)

            rl_state.close_position(ticker, close_price, "sim_end", self._wf_db_path)

            wf_pos_id = ticker_to_wf_id.get(ticker)
            if wf_pos_id is not None:
                storage.record_position_close(
                    db_path=self._wf_db_path,
                    position_db_id=wf_pos_id,
                    exit_date=sim_end,
                    exit_price=close_price,
                    exit_commission=exit_commission,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    exit_reason="sim_end",
                )

            portfolio_value += net_pnl
            total_trades    += 1

        # Final run record update
        storage.close_run(self._wf_db_path, run_id, portfolio_value, total_trades)

        logger.info({
            "event": "wf_run_complete",
            "run_id": run_id,
            "portfolio_start": float(self._config["risk"]["portfolio_value_stub"]),
            "portfolio_end": round(portfolio_value, 2),
            "total_trades": total_trades,
        })

        return run_id

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _eligible_tickers(self, walker: DataFrameWalker, tickers: list[str]) -> list[str]:
        """Return tickers with at least min_ticker_days of calendar-day history."""
        eligible: list[str] = []
        for ticker in tickers:
            df = walker._store.get(ticker)
            if df is None:
                continue
            idx = df.index
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            if len(idx) == 0:
                continue
            calendar_days = (idx[-1] - idx[0]).days
            if calendar_days >= self._min_ticker_days:
                eligible.append(ticker)
            else:
                self._logger.debug({
                    "event": "wf_ticker_skipped",
                    "ticker": ticker,
                    "calendar_days": calendar_days,
                    "required": self._min_ticker_days,
                })
        return eligible

    @staticmethod
    def _count_trading_days(
        position: dict,
        current_date: pd.Timestamp,
        sim_dates: list[pd.Timestamp],
    ) -> int:
        """Count trading sessions between the position's entry date and current_date."""
        entry_ts = pd.Timestamp(position["opened_at"][:10])
        return sum(1 for d in sim_dates if entry_ts <= d <= current_date)
