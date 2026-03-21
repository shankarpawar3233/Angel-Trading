import argparse
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import pandas as pd
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: pandas. Install it with `pip install pandas` "
        "and re-run this script."
    ) from exc


LOGGER = logging.getLogger("nifty_signal_analysis")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def load_jsonl_safely(file_path: Path) -> pd.DataFrame:
    """Load JSONL into DataFrame while skipping malformed lines."""
    records: List[Dict] = []
    malformed = 0

    with file_path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
                else:
                    malformed += 1
            except json.JSONDecodeError:
                malformed += 1
                continue

    LOGGER.info("Loaded %s valid JSON rows; ignored %s malformed rows", len(records), malformed)
    return pd.DataFrame(records)


def clean_and_filter_signals(df: pd.DataFrame, symbol: str = "NIFTY") -> pd.DataFrame:
    """Filter relevant signals and coerce required columns to numeric."""
    required_cols = ["ts", "symbol", "signal", "price", "entry", "target", "stoploss", "confidence", "strike"]
    for col in required_cols:
        if col not in df.columns:
            df[col] = pd.NA

    work = df.copy()
    work = work[(work["symbol"] == symbol) & (work["signal"].isin(["BUY_CE", "BUY_PE"]))]

    work["ts"] = pd.to_datetime(work["ts"], errors="coerce", utc=True)
    for col in ["price", "entry", "target", "stoploss", "confidence", "strike"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=["ts", "entry", "target", "stoploss", "price"])
    work = work.sort_values("ts").reset_index(drop=True)

    LOGGER.info("Filtered to %s valid %s signal rows", len(work), symbol)
    return work


def classify_trade_outcome(
    future_prices: pd.Series, target: float, stoploss: float
) -> Tuple[str, int | None, float | None]:
    """
    Determine outcome from future prices.
    Returns: (outcome, relative_hit_index, hit_price)
    """
    if future_prices.empty:
        return "NO_RESULT", None, None

    for idx, px in enumerate(future_prices.tolist()):
        if px >= target:
            return "TARGET_HIT", idx, float(px)
        if px <= stoploss:
            return "STOPLOSS_HIT", idx, float(px)
    return "NO_RESULT", None, None


def simulate_trade_outcomes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Simulate each signal against future rows using the `price` column.
    """
    if df.empty:
        out = df.copy()
        out["outcome"] = pd.Series(dtype="object")
        out["hit_ts"] = pd.Series(dtype="datetime64[ns, UTC]")
        out["hit_price"] = pd.Series(dtype="float64")
        out["pnl_points"] = pd.Series(dtype="float64")
        return out

    work = df.copy()
    outcomes: List[str] = []
    hit_ts_list: List[pd.Timestamp | pd.NaT] = []
    hit_price_list: List[float | None] = []
    pnl_points_list: List[float] = []

    prices = work["price"]
    timestamps = work["ts"]

    for i in range(len(work)):
        row = work.iloc[i]
        future_prices = prices.iloc[i + 1 :]
        future_ts = timestamps.iloc[i + 1 :]

        outcome, rel_hit_idx, hit_price = classify_trade_outcome(
            future_prices=future_prices,
            target=float(row["target"]),
            stoploss=float(row["stoploss"]),
        )
        outcomes.append(outcome)

        if rel_hit_idx is not None:
            hit_ts = future_ts.iloc[rel_hit_idx]
            hit_ts_list.append(hit_ts)
            hit_price_list.append(hit_price)
        else:
            hit_ts_list.append(pd.NaT)
            hit_price_list.append(None)

        entry = float(row["entry"])
        target = float(row["target"])
        stoploss = float(row["stoploss"])
        if outcome == "TARGET_HIT":
            pnl = target - entry
        elif outcome == "STOPLOSS_HIT":
            pnl = stoploss - entry
        else:
            pnl = 0.0
        pnl_points_list.append(float(pnl))

    work["outcome"] = outcomes
    work["hit_ts"] = hit_ts_list
    work["hit_price"] = hit_price_list
    work["pnl_points"] = pnl_points_list
    return work


def _compute_streaks(results_df: pd.DataFrame) -> Dict[str, int]:
    max_win_streak = 0
    max_loss_streak = 0
    current_win = 0
    current_loss = 0

    for is_win in (results_df["net_pnl_points"] > 0).tolist():
        if is_win:
            current_win += 1
            current_loss = 0
        else:
            current_loss += 1
            current_win = 0
        max_win_streak = max(max_win_streak, current_win)
        max_loss_streak = max(max_loss_streak, current_loss)

    return {"max_win_streak": int(max_win_streak), "max_loss_streak": int(max_loss_streak)}


def simulate_realistic_trade_outcomes(
    df: pd.DataFrame,
    slippage_pct: float,
    brokerage_per_order: float,
    close_open_positions_at_end: bool = True,
) -> pd.DataFrame:
    """
    Simulate option premium execution with:
    - slippage on entry/exit prices
    - brokerage on both sides
    - optional forced close for NO_RESULT at last observed future price
    """
    if df.empty:
        out = df.copy()
        out["outcome"] = pd.Series(dtype="object")
        out["hit_ts"] = pd.Series(dtype="datetime64[ns, UTC]")
        out["hit_price"] = pd.Series(dtype="float64")
        out["entry_exec"] = pd.Series(dtype="float64")
        out["exit_exec"] = pd.Series(dtype="float64")
        out["gross_pnl_points"] = pd.Series(dtype="float64")
        out["cost_points"] = pd.Series(dtype="float64")
        out["net_pnl_points"] = pd.Series(dtype="float64")
        out["rr_ratio"] = pd.Series(dtype="float64")
        out["trade_date"] = pd.Series(dtype="object")
        return out

    work = df.copy()
    prices = work["price"]
    timestamps = work["ts"]

    outcomes: List[str] = []
    hit_ts_list: List[pd.Timestamp | pd.NaT] = []
    hit_price_list: List[float | None] = []
    entry_exec_list: List[float] = []
    exit_exec_list: List[float] = []
    gross_pnl_points_list: List[float] = []
    cost_points_list: List[float] = []
    net_pnl_points_list: List[float] = []
    rr_ratio_list: List[float | None] = []

    for i in range(len(work)):
        row = work.iloc[i]
        signal_ts = row["ts"]
        future_prices = prices.iloc[i + 1 :]
        future_ts = timestamps.iloc[i + 1 :]

        outcome, rel_hit_idx, hit_price = classify_trade_outcome(
            future_prices=future_prices,
            target=float(row["target"]),
            stoploss=float(row["stoploss"]),
        )

        theoretical_entry = float(row["entry"])
        entry_exec = theoretical_entry * (1.0 + slippage_pct / 100.0)

        if rel_hit_idx is not None:
            hit_ts = future_ts.iloc[rel_hit_idx]
            raw_exit = float(hit_price) if hit_price is not None else float(future_prices.iloc[rel_hit_idx])
        else:
            hit_ts = pd.NaT
            if close_open_positions_at_end and not future_prices.empty:
                raw_exit = float(future_prices.iloc[-1])
            else:
                raw_exit = theoretical_entry

        if outcome == "TARGET_HIT":
            exit_exec = raw_exit * (1.0 - slippage_pct / 100.0)
        elif outcome == "STOPLOSS_HIT":
            exit_exec = raw_exit * (1.0 - slippage_pct / 100.0)
        else:
            exit_exec = raw_exit * (1.0 - slippage_pct / 100.0) if close_open_positions_at_end else entry_exec

        gross_pnl = exit_exec - entry_exec
        total_cost = 2.0 * brokerage_per_order
        net_pnl = gross_pnl - total_cost

        reward = float(row["target"]) - theoretical_entry
        risk = theoretical_entry - float(row["stoploss"])
        rr_ratio = (reward / risk) if risk > 0 else None

        outcomes.append(outcome)
        hit_ts_list.append(hit_ts)
        hit_price_list.append(raw_exit)
        entry_exec_list.append(float(entry_exec))
        exit_exec_list.append(float(exit_exec))
        gross_pnl_points_list.append(float(gross_pnl))
        cost_points_list.append(float(total_cost))
        net_pnl_points_list.append(float(net_pnl))
        rr_ratio_list.append(float(rr_ratio) if rr_ratio is not None else None)

    work["outcome"] = outcomes
    work["hit_ts"] = hit_ts_list
    work["hit_price"] = hit_price_list
    work["entry_exec"] = entry_exec_list
    work["exit_exec"] = exit_exec_list
    work["gross_pnl_points"] = gross_pnl_points_list
    work["cost_points"] = cost_points_list
    work["net_pnl_points"] = net_pnl_points_list
    work["pnl_points"] = work["net_pnl_points"]
    work["rr_ratio"] = rr_ratio_list
    work["trade_date"] = work["ts"].dt.date
    work["holding_minutes"] = (
        (work["hit_ts"].fillna(work["ts"]) - work["ts"]).dt.total_seconds().div(60.0)
    )
    work.loc[work["holding_minutes"] < 0, "holding_minutes"] = 0.0
    LOGGER.info(
        "Realistic simulation complete: slippage=%.4f%% brokerage/order=%.2f",
        slippage_pct,
        brokerage_per_order,
    )
    return work


def compute_metrics(results_df: pd.DataFrame) -> Dict:
    total_signals = int(len(results_df))
    valid_trades = total_signals
    target_hits = int((results_df["outcome"] == "TARGET_HIT").sum()) if total_signals else 0
    stoploss_hits = int((results_df["outcome"] == "STOPLOSS_HIT").sum()) if total_signals else 0
    no_result = int((results_df["outcome"] == "NO_RESULT").sum()) if total_signals else 0
    accuracy = (target_hits / valid_trades * 100.0) if valid_trades else 0.0

    buy_ce_count = int((results_df["signal"] == "BUY_CE").sum()) if total_signals else 0
    buy_pe_count = int((results_df["signal"] == "BUY_PE").sum()) if total_signals else 0
    avg_confidence = float(results_df["confidence"].mean()) if total_signals else 0.0

    high_conf_df = results_df[results_df["confidence"] > 70]
    low_conf_df = results_df[results_df["confidence"] < 50]
    high_conf_acc = (
        (high_conf_df["outcome"] == "TARGET_HIT").sum() / len(high_conf_df) * 100.0
        if len(high_conf_df)
        else 0.0
    )
    low_conf_acc = (
        (low_conf_df["outcome"] == "TARGET_HIT").sum() / len(low_conf_df) * 100.0
        if len(low_conf_df)
        else 0.0
    )

    strike_stats = (
        results_df.groupby("strike", dropna=True)
        .agg(
            trades=("outcome", "size"),
            target_hits=("outcome", lambda s: (s == "TARGET_HIT").sum()),
        )
        .reset_index()
    )
    if not strike_stats.empty:
        strike_stats["accuracy"] = (strike_stats["target_hits"] / strike_stats["trades"]) * 100.0
        best_strike_row = strike_stats.sort_values(["accuracy", "trades"], ascending=[False, False]).iloc[0]
        best_strike = best_strike_row["strike"]
    else:
        strike_stats["accuracy"] = pd.Series(dtype="float64")
        best_strike = None

    hourly_stats = (
        results_df.assign(hour=results_df["ts"].dt.hour)
        .groupby("hour")
        .agg(
            trades=("outcome", "size"),
            target_hits=("outcome", lambda s: (s == "TARGET_HIT").sum()),
        )
        .reset_index()
    )
    if not hourly_stats.empty:
        hourly_stats["accuracy"] = (hourly_stats["target_hits"] / hourly_stats["trades"]) * 100.0
        best_hour_row = hourly_stats.sort_values(["accuracy", "trades"], ascending=[False, False]).iloc[0]
        best_hour = int(best_hour_row["hour"])
    else:
        hourly_stats["accuracy"] = pd.Series(dtype="float64")
        best_hour = None

    avg_rr_ratio = float(results_df["rr_ratio"].dropna().mean()) if total_signals else 0.0
    total_gross_pnl = float(results_df["gross_pnl_points"].sum()) if total_signals else 0.0
    total_cost_points = float(results_df["cost_points"].sum()) if total_signals else 0.0
    total_net_pnl = float(results_df["net_pnl_points"].sum()) if total_signals else 0.0
    avg_net_pnl = float(results_df["net_pnl_points"].mean()) if total_signals else 0.0
    streak_stats = _compute_streaks(results_df.sort_values("ts")) if total_signals else {"max_win_streak": 0, "max_loss_streak": 0}

    return {
        "total_signals": total_signals,
        "valid_trades": valid_trades,
        "target_hits": target_hits,
        "stoploss_hits": stoploss_hits,
        "no_result": no_result,
        "accuracy": accuracy,
        "buy_ce_count": buy_ce_count,
        "buy_pe_count": buy_pe_count,
        "avg_confidence": avg_confidence,
        "high_conf_acc": high_conf_acc,
        "low_conf_acc": low_conf_acc,
        "strike_stats": strike_stats,
        "hourly_stats": hourly_stats,
        "best_strike": best_strike,
        "best_hour": best_hour,
        "avg_rr_ratio": avg_rr_ratio,
        "total_gross_pnl": total_gross_pnl,
        "total_cost_points": total_cost_points,
        "total_net_pnl": total_net_pnl,
        "avg_net_pnl": avg_net_pnl,
        "max_win_streak": streak_stats["max_win_streak"],
        "max_loss_streak": streak_stats["max_loss_streak"],
    }


def print_report(metrics: Dict) -> None:
    print("==== NIFTY PERFORMANCE REPORT ====")
    print()
    print(f"Total Signals: {metrics['total_signals']}")
    print(f"Valid Trades: {metrics['valid_trades']}")
    print(f"Target Hits: {metrics['target_hits']}")
    print(f"Stoploss Hits: {metrics['stoploss_hits']}")
    print(f"No Result: {metrics['no_result']}")
    print(f"Accuracy: {metrics['accuracy']:.2f}%")
    print(f"Total Net PnL: {metrics['total_net_pnl']:.2f}")
    print(f"Average Net PnL/Trade: {metrics['avg_net_pnl']:.2f}")
    print()
    print("--- Signal Split ---")
    print(f"BUY_CE: {metrics['buy_ce_count']}")
    print(f"BUY_PE: {metrics['buy_pe_count']}")
    print()
    print("--- Confidence ---")
    print(f"Avg Confidence: {metrics['avg_confidence']:.2f}")
    print(f"High Confidence Accuracy: {metrics['high_conf_acc']:.2f}%")
    print(f"Low Confidence Accuracy: {metrics['low_conf_acc']:.2f}%")
    print(f"Avg R:R Ratio: {metrics['avg_rr_ratio']:.2f}")
    print()
    print("--- Streaks ---")
    print(f"Max Win Streak: {metrics['max_win_streak']}")
    print(f"Max Loss Streak: {metrics['max_loss_streak']}")
    print()
    print("--- Best Strike ---")
    print(metrics["best_strike"] if metrics["best_strike"] is not None else "N/A")
    print()
    print("--- Best Hour ---")
    print(metrics["best_hour"] if metrics["best_hour"] is not None else "N/A")


def save_results_csv(df: pd.DataFrame, output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)
    LOGGER.info("Saved analysis CSV: %s", output_csv)


def save_daily_pnl_summary(results_df: pd.DataFrame, output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    if results_df.empty:
        pd.DataFrame(
            columns=["trade_date", "trades", "target_hits", "stoploss_hits", "no_result", "gross_pnl_points", "cost_points", "net_pnl_points", "win_rate"]
        ).to_csv(output_csv, index=False)
        LOGGER.info("Saved empty daily PnL summary: %s", output_csv)
        return

    daily = (
        results_df.groupby("trade_date")
        .agg(
            trades=("outcome", "size"),
            target_hits=("outcome", lambda s: (s == "TARGET_HIT").sum()),
            stoploss_hits=("outcome", lambda s: (s == "STOPLOSS_HIT").sum()),
            no_result=("outcome", lambda s: (s == "NO_RESULT").sum()),
            gross_pnl_points=("gross_pnl_points", "sum"),
            cost_points=("cost_points", "sum"),
            net_pnl_points=("net_pnl_points", "sum"),
        )
        .reset_index()
    )
    daily["win_rate"] = daily.apply(
        lambda r: (r["target_hits"] / r["trades"] * 100.0) if r["trades"] else 0.0, axis=1
    )
    daily.to_csv(output_csv, index=False)
    LOGGER.info("Saved daily PnL summary CSV: %s", output_csv)


def plot_equity_curve(results_df: pd.DataFrame, output_png: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        LOGGER.warning("matplotlib is not installed; skipping equity curve plot.")
        return

    output_png.parent.mkdir(parents=True, exist_ok=True)
    if results_df.empty:
        LOGGER.warning("No results to plot for equity curve.")
        return

    plot_df = results_df.sort_values("ts").copy()
    plot_df["equity_curve"] = plot_df["pnl_points"].cumsum()

    plt.figure(figsize=(11, 5))
    plt.plot(plot_df["ts"], plot_df["equity_curve"], color="tab:blue", linewidth=2)
    plt.title("NIFTY Signal Equity Curve")
    plt.xlabel("Time")
    plt.ylabel("Cumulative PnL (points)")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_png, dpi=150)
    plt.close()
    LOGGER.info("Saved equity curve plot: %s", output_png)


def plot_accuracy_by_confidence(results_df: pd.DataFrame, output_png: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        LOGGER.warning("matplotlib is not installed; skipping confidence bucket plot.")
        return

    output_png.parent.mkdir(parents=True, exist_ok=True)
    if results_df.empty:
        LOGGER.warning("No results to plot for confidence bucket accuracy.")
        return

    bins = [0, 40, 50, 60, 70, 80, 90, 100]
    labels = ["0-40", "40-50", "50-60", "60-70", "70-80", "80-90", "90-100"]

    conf_df = results_df.dropna(subset=["confidence"]).copy()
    if conf_df.empty:
        LOGGER.warning("No confidence values available for bucket plot.")
        return

    conf_df["confidence_bucket"] = pd.cut(
        conf_df["confidence"], bins=bins, labels=labels, include_lowest=True, right=True
    )
    bucket_stats = (
        conf_df.groupby("confidence_bucket", observed=False)
        .agg(trades=("outcome", "size"), target_hits=("outcome", lambda s: (s == "TARGET_HIT").sum()))
        .reset_index()
    )
    bucket_stats["accuracy"] = bucket_stats.apply(
        lambda r: (r["target_hits"] / r["trades"] * 100.0) if r["trades"] else 0.0, axis=1
    )

    plt.figure(figsize=(10, 5))
    plt.bar(bucket_stats["confidence_bucket"].astype(str), bucket_stats["accuracy"], color="tab:green")
    plt.title("Accuracy by Confidence Bucket")
    plt.xlabel("Confidence Bucket")
    plt.ylabel("Accuracy (%)")
    plt.ylim(0, 100)
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_png, dpi=150)
    plt.close()
    LOGGER.info("Saved confidence bucket accuracy plot: %s", output_png)


def run_analysis(
    input_jsonl: Path,
    output_csv: Path,
    output_dir: Path,
    slippage_pct: float,
    brokerage_per_order: float,
    close_open_positions_at_end: bool,
) -> None:
    raw_df = load_jsonl_safely(input_jsonl)
    filtered_df = clean_and_filter_signals(raw_df, symbol="NIFTY")
    results_df = simulate_realistic_trade_outcomes(
        filtered_df,
        slippage_pct=slippage_pct,
        brokerage_per_order=brokerage_per_order,
        close_open_positions_at_end=close_open_positions_at_end,
    )
    metrics = compute_metrics(results_df)
    print_report(metrics)

    save_results_csv(results_df, output_csv)
    save_daily_pnl_summary(results_df, output_dir / "nifty_daily_pnl_summary.csv")
    plot_equity_curve(results_df, output_dir / "nifty_equity_curve.png")
    plot_accuracy_by_confidence(results_df, output_dir / "nifty_accuracy_by_confidence_bucket.png")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NIFTY signal backtest analysis from JSONL")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("logs") / "signal_events.jsonl",
        help="Path to signal_events.jsonl",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("analysis") / "nifty_trade_analysis.csv",
        help="Path to output CSV",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("analysis"),
        help="Directory where plots will be saved",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--slippage-pct",
        type=float,
        default=0.25,
        help="Slippage percentage applied on entry and exit execution prices.",
    )
    parser.add_argument(
        "--brokerage-per-order",
        type=float,
        default=0.5,
        help="Brokerage cost in premium points charged per order side.",
    )
    parser.add_argument(
        "--close-open-positions-at-end",
        action="store_true",
        help="Force close NO_RESULT trades at the last available future price.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)

    if not args.input.exists():
        raise SystemExit(f"Input JSONL file not found: {args.input}")

    run_analysis(
        input_jsonl=args.input,
        output_csv=args.output_csv,
        output_dir=args.output_dir,
        slippage_pct=args.slippage_pct,
        brokerage_per_order=args.brokerage_per_order,
        close_open_positions_at_end=args.close_open_positions_at_end,
    )


if __name__ == "__main__":
    main()
