from __future__ import annotations

import argparse
import os
import sys
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import threading
import traceback
import json
import html

import pandas as pd
import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from data_sources.price_data import (
    create_universe_template,
    download_universe_pykrx,
    download_prices,
    load_universe,
    read_prices,
)
from data_sources.quantking_sqlite_importer import (
    convert_quantking_sqlite_to_csv,
    write_table_inventory,
)
from data_sources.supply_data import create_supply_template, read_supply, download_supply_pykrx
from data_sources.financial_data import create_financial_template, read_financials, download_financials_pykrx
from data_sources.volume_data import ensure_volume_columns

from filters.liquidity_filter import latest_liquidity_flags
from filters.sudden_spike_filter import latest_spike_flags
from filters.financial_risk_filter import financial_risk_flags
from filters.overheat_filter import latest_overheat_flags

from features.return_distribution_features import compute_return_distribution_features
from features.sideways_features import compute_sideways_features
from features.support_resistance_features import compute_support_resistance_features
from features.volatility_contraction_features import compute_volatility_contraction_features
from features.supply_features import compute_supply_features
from features.accumulation_features import compute_accumulation_features
from features.valuation_features import compute_valuation_features
from features.fibonacci_angle_features import compute_fibonacci_angle_features

from scoring.sideways_score import score_sideways
from scoring.support_resistance_score import score_support_resistance
from scoring.supply_score import score_supply
from scoring.accumulation_score import score_accumulation
from scoring.valuation_score import score_valuation
from scoring.fibonacci_angle_score import score_fibonacci_angle
from scoring.risk_score import score_risk
from scoring.final_ranker import build_final_ranking

from reports.daily_signal_report import save_reports, _mini_chart_svg
from reports.ai_summary import build_offline_summary
from strategies.multi_strategy_portfolio import build_multi_strategy_portfolio, save_multi_strategy_reports
from strategies.fast_current_portfolio import build_fast_current_portfolio
from data_sources.weekly_features import build_weekly_prices
from data_sources.sector_strength import read_sector_map, build_sector_strength
from data_sources.dart_financials import download_dart_financials
from backtest.multi_strategy_backtester import run_multi_strategy_backtest

DATA_DIR = Path(os.getenv("DATA_DIR", "data/csv_import"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "reports/output"))


def paths(data_dir: Path = DATA_DIR) -> dict[str, Path]:
    return {
        "universe": data_dir / "universe.csv",
        "prices": data_dir / "prices_daily.csv",
        "supply": data_dir / "supply_daily.csv",
        "financials": data_dir / "financials.csv",
        "financial_quarterly": data_dir / "financial_quarterly.csv",
        "weekly": data_dir / "prices_weekly.csv",
        "sector_map": data_dir / "sector_map.csv",
        "sector_strength": data_dir / "sector_strength.csv",
    }


JOB_STATE = {"running": False, "name": "", "status": "대기", "error": "", "traceback": ""}


STRATEGY_IDS = ["vcp", "canslim", "stage2", "darvas"]
STRATEGY_LABELS = {
    "vcp": "1. Minervini VCP",
    "canslim": "2. CANSLIM",
    "stage2": "3. Weinstein Stage2",
    "darvas": "4. Darvas Box",
}


def _selected_strategies_from_form(form: dict[str, list[str]] | None) -> list[str]:
    if not form:
        return STRATEGY_IDS.copy()
    vals = form.get("strategies") or form.get("strategy") or []
    selected = [str(v).strip().lower() for v in vals if str(v).strip().lower() in STRATEGY_IDS]
    return selected or STRATEGY_IDS.copy()


def _runtime_config(base_config: str | Path, enabled_strategies: list[str] | None, output_name: str) -> Path:
    base = Path(base_config)
    cfg = {}
    if base.exists():
        cfg = json.loads(base.read_text(encoding="utf-8"))
    if enabled_strategies:
        cfg["enabled_strategies"] = enabled_strategies
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / output_name
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _strategy_checkbox_html() -> str:
    return "".join(
        f"<label class='check'><input type='checkbox' name='strategies' value='{sid}' checked> {label}</label>"
        for sid, label in STRATEGY_LABELS.items()
    )

def _run_job(name: str, func, *args, **kwargs) -> None:
    def worker():
        JOB_STATE.update({"running": True, "name": name, "status": "진행 중", "error": "", "traceback": ""})
        try:
            func(*args, **kwargs)
            JOB_STATE.update({"running": False, "status": "완료"})
        except Exception as e:
            JOB_STATE.update({"running": False, "status": "오류", "error": str(e), "traceback": traceback.format_exc()})
    if JOB_STATE.get("running"):
        raise RuntimeError(f"이미 작업이 실행 중입니다: {JOB_STATE.get('name')}")
    threading.Thread(target=worker, daemon=True).start()

def _partition_universe_file(source_csv: Path, part: int, parts: int, output_csv: Path, limit: int | None = None) -> Path:
    df = pd.read_csv(source_csv, dtype={"stock_code": str, "종목코드": str}).rename(columns={"종목코드": "stock_code"})
    df["stock_code"] = df["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    df = df.drop_duplicates("stock_code").sort_values("stock_code").reset_index(drop=True)
    if limit:
        df = df.head(limit)
    if parts > 1:
        df = df[(pd.Series(range(len(df))) % parts) == (part - 1)].copy()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    return output_csv

def build_derived_data(data_dir: Path = DATA_DIR) -> None:
    p = paths(data_dir)
    prices = read_prices(p["prices"])
    if prices.empty:
        raise RuntimeError("prices_daily.csv가 비어 있어 주봉/섹터 데이터를 만들 수 없습니다.")
    prices = ensure_volume_columns(prices)
    weekly = build_weekly_prices(prices, p["weekly"])
    sector_map = read_sector_map(p["sector_map"])
    sector_strength = build_sector_strength(prices, sector_map, p["sector_strength"])
    print(f"주봉 데이터 저장: {p['weekly']} ({len(weekly):,} rows)")
    print(f"섹터 주도주 데이터 저장: {p['sector_strength']} ({len(sector_strength):,} rows)")

def download_dart_data(part: int = 1, parts: int = 3, years: int = 5, limit: int | None = None, sleep_sec: float = 0.15) -> None:
    try:
        from dotenv import load_dotenv; load_dotenv(override=True)
    except Exception:
        pass
    api_key = os.getenv("DART_API_KEY", "").strip()
    p = paths()
    result = download_dart_financials(api_key=api_key, universe_csv=p["universe"], output_csv=p["financial_quarterly"], years=years, part=part, parts=parts, limit=limit, sleep_sec=sleep_sec)
    print(f"DART part 저장: {result.part_file} ({len(result.quarterly):,} rows)")
    print(f"DART 병합 저장: {result.merged_file}")
    print(f"최신 재무 요약 저장: {p['financials']}")

def download_supply_partition(days: int = 1250, part: int = 1, parts: int = 3, limit: int | None = None) -> None:
    p = paths()
    tmp = DATA_DIR / "tmp" / f"universe_part{part}_of_{parts}.csv"
    _partition_universe_file(p["universe"], part, parts, tmp, limit=limit)
    part_out = DATA_DIR / "supply_parts" / f"supply_daily_part{part}_of_{parts}.csv"
    part_supply = download_supply_pykrx(tmp, part_out, days=days)
    frames = []
    for f in sorted((DATA_DIR / "supply_parts").glob("supply_daily_part*.csv")):
        try:
            frames.append(pd.read_csv(f, dtype={"stock_code": str}))
        except Exception:
            pass
    if p["supply"].exists():
        try:
            frames.append(pd.read_csv(p["supply"], dtype={"stock_code": str}))
        except Exception:
            pass
    if frames:
        merged = pd.concat(frames, ignore_index=True)
        merged["stock_code"] = merged["stock_code"].astype(str).str.zfill(6)
        merged = merged.drop_duplicates(["stock_code", "date"], keep="last").sort_values(["stock_code", "date"])
        merged.to_csv(p["supply"], index=False, encoding="utf-8-sig")
        print(f"수급 병합 저장: {p['supply']} ({len(merged):,} rows)")
    print(f"수급 part 저장: {part_out} ({len(part_supply):,} rows)")

def run_backtest(data_dir: Path = DATA_DIR, output_dir: Path = OUTPUT_DIR, config_path: str | Path | None = None, enabled_strategies: list[str] | None = None) -> dict[str, pd.DataFrame]:
    p = paths(data_dir)
    prices = read_prices(p["prices"])
    if prices.empty:
        raise RuntimeError("prices_daily.csv가 비어 있습니다. 먼저 가격 CSV를 받아야 합니다.")
    prices = ensure_volume_columns(prices)
    if enabled_strategies:
        config_path = _runtime_config(config_path or "configs/backtest_config.json", enabled_strategies, "runtime_backtest_config.json")
    result = run_multi_strategy_backtest(prices, config_path=config_path, output_dir=output_dir)
    print("백테스트 완료")
    print(output_dir / "backtest_report.html")
    if not result["summary"].empty:
        print(result["summary"].to_string(index=False))
    return result



def _prefilter_prices_for_analysis(prices: pd.DataFrame, max_symbols: int | None = None, lookback_rows: int = 80) -> tuple[pd.DataFrame, dict[str, int]]:
    """대형 2700종목 CSV에서 분석 시간을 줄이고 실거래성을 높이기 위한 최근 거래대금 상위 필터."""
    if prices is None or prices.empty:
        return prices, {"symbols_before": 0, "symbols_after": 0}
    if max_symbols is None:
        max_symbols = int(os.getenv("ANALYZE_MAX_SYMBOLS", "800"))
    if max_symbols <= 0:
        before = prices["stock_code"].astype(str).nunique() if "stock_code" in prices else 0
        return prices, {"symbols_before": before, "symbols_after": before}
    df = prices.copy()
    df["stock_code"] = df["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    before = int(df["stock_code"].nunique())
    if before <= max_symbols:
        return df, {"symbols_before": before, "symbols_after": before}
    if "trading_value" not in df:
        df["trading_value"] = pd.to_numeric(df.get("close", 0), errors="coerce") * pd.to_numeric(df.get("volume", 0), errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df["trading_value"] = pd.to_numeric(df["trading_value"], errors="coerce").fillna(0)
    liq = (
        df.sort_values(["stock_code", "date"])
        .groupby("stock_code", sort=False)
        .tail(lookback_rows)
        .groupby("stock_code")["trading_value"]
        .median()
        .sort_values(ascending=False)
    )
    keep = set(liq.head(max_symbols).index.astype(str))
    out = df[df["stock_code"].isin(keep)].copy()
    return out, {"symbols_before": before, "symbols_after": int(out["stock_code"].nunique())}


def init_templates() -> None:
    p = paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not p["universe"].exists():
        create_universe_template(p["universe"])
    if not p["prices"].exists():
        pd.DataFrame(columns=["stock_code", "stock_name", "date", "open", "high", "low", "close", "volume", "trading_value"]).to_csv(p["prices"], index=False, encoding="utf-8-sig")
    if not p["supply"].exists():
        create_supply_template(p["supply"])
    if not p["financials"].exists():
        create_financial_template(p["financials"])
    print(f"템플릿 생성 완료: {DATA_DIR}")


def download_data(source: str = "naver", days: int = 250, limit: int | None = None, skip_supply: bool = False, skip_financials: bool = False) -> None:
    p = paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not p["universe"].exists() or pd.read_csv(p["universe"], dtype=str).empty:
        try:
            print("universe.csv 생성 중(pykrx)...")
            download_universe_pykrx(p["universe"])
        except Exception as e:
            print(f"pykrx 유니버스 생성 실패: {e}")
            print("샘플 universe.csv를 생성합니다. 실제 분석 전 종목 목록을 채우세요.")
            create_universe_template(p["universe"])
    print(f"가격 데이터 다운로드 시작: source={source}, days={days}")
    if source.lower() == "kiwoom":
        from data_sources.kiwoom_openapi import download_prices_kiwoom
        prices = download_prices_kiwoom(p["universe"], p["prices"], days=days, limit=limit)
    elif source.lower() == "yfinance":
        from data_sources.yfinance_kr import build_krx_universe, download_prices_yfinance
        print("국내 전체 유니버스 생성 중(pykrx, KOSPI/KOSDAQ)...")
        build_krx_universe(p["universe"], markets=("KOSPI", "KOSDAQ"))
        result = download_prices_yfinance(
            universe_csv=p["universe"],
            output_csv=p["prices"],
            period="1y" if days >= 240 else f"{days}d",
            batch_size=int(os.getenv("YFINANCE_BATCH_SIZE", "50")),
            sleep_sec=float(os.getenv("YFINANCE_SLEEP_SEC", "1.0")),
            limit=limit,
        )
        prices = result.prices
        if not result.failures.empty:
            print(f"yfinance 누락/실패: {len(result.failures):,}개 → {p['prices'].parent / 'yfinance_download_failures.csv'}")
    else:
        prices = download_prices(p["universe"], p["prices"], source=source, days=days, limit=limit)
    print(f"가격 데이터 저장: {p['prices']} ({len(prices):,} rows)")
    if not skip_supply:
        try:
            if source.lower() == "kiwoom":
                print("수급 데이터 다운로드 시작(Kiwoom opt10059)...")
                from data_sources.kiwoom_openapi import download_supply_kiwoom
                supply = download_supply_kiwoom(p["universe"], p["supply"], days=days, limit=limit)
            else:
                print("수급 데이터 다운로드 시작(pykrx)...")
                supply = download_supply_pykrx(p["universe"], p["supply"], days=days, limit=limit)
            print(f"수급 데이터 저장: {p['supply']} ({len(supply):,} rows)")
        except Exception as e:
            print(f"수급 데이터 자동 다운로드 실패: {e}")
            print("supply_daily.csv 템플릿만 유지합니다. 나중에 CSV를 넣으면 수급 점수가 활성화됩니다.")
            if not p["supply"].exists(): create_supply_template(p["supply"])
    if not skip_financials:
        try:
            print("재무 데이터 다운로드 시작(pykrx)...")
            fin = download_financials_pykrx(p["universe"], p["financials"])
            print(f"재무 데이터 저장: {p['financials']} ({len(fin):,} rows)")
        except Exception as e:
            print(f"재무 데이터 자동 다운로드 실패: {e}")
            if not p["financials"].exists(): create_financial_template(p["financials"])


def analyze(data_dir: Path = DATA_DIR, output_dir: Path = OUTPUT_DIR, enabled_strategies: list[str] | None = None) -> pd.DataFrame:
    p = paths(data_dir)
    universe = load_universe(p["universe"])
    prices = read_prices(p["prices"])
    if prices.empty:
        raise RuntimeError("prices_daily.csv가 비어 있습니다. 먼저 python main.py download --source naver --days 250 실행 또는 CSV를 채우세요.")
    prices = ensure_volume_columns(prices)
    prices, analysis_filter_info = _prefilter_prices_for_analysis(prices)
    if analysis_filter_info.get("symbols_before") != analysis_filter_info.get("symbols_after"):
        print(f"분석 대상 필터: 최근 거래대금 상위 {analysis_filter_info.get('symbols_after')}개 / 전체 {analysis_filter_info.get('symbols_before')}개")
        universe = universe[universe["stock_code"].astype(str).str.zfill(6).isin(set(prices["stock_code"].astype(str).str.zfill(6)))] if not universe.empty else universe
    # 체크박스 기반 4전략 분석은 기존 레거시 점수 계산을 건너뛰고 빠르게 실행한다.
    if enabled_strategies is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        financials = read_financials(p["financials"])
        try:
            build_weekly_prices(prices, p["weekly"])
            build_sector_strength(prices, read_sector_map(p["sector_map"]), p["sector_strength"])
        except Exception as e:
            print(f"파생 데이터 생성 경고: {e}")
        config_path = _runtime_config("configs/multi_strategy_config.json", enabled_strategies, "runtime_multi_strategy_config.json")
        all_strategy_signals, multi_portfolio = build_fast_current_portfolio(
            prices=prices,
            universe=universe,
            financials=financials,
            config_path=config_path,
            enabled_strategies=enabled_strategies,
        )
        strategy_paths = save_multi_strategy_reports(all_strategy_signals, multi_portfolio, output_dir)
        # 기본 리포트 링크가 비어 보이지 않도록 간단한 안내 HTML도 생성한다.
        (output_dir / "daily_signal_report.html").write_text(
            "<html><meta charset='utf-8'><body><h1>선택 전략 분석 완료</h1><p><a href='/terminal'>전략 터미널</a>에서 종목별 상세 화면을 확인하세요.</p><p><a href='/multi-strategy'>4전략 포트폴리오</a></p></body></html>",
            encoding="utf-8",
        )
        print("선택 전략 분석 완료")
        print(strategy_paths)
        return multi_portfolio
    # 주봉 30주선/섹터 강도는 현재 prices_daily.csv에서 파생 생성한다.
    try:
        build_weekly_prices(prices, p["weekly"])
        build_sector_strength(prices, read_sector_map(p["sector_map"]), p["sector_strength"])
    except Exception as e:
        print(f"파생 데이터 생성 경고: {e}")
    supply = read_supply(p["supply"])
    financials = read_financials(p["financials"])

    # 1) 필터/feature 계산
    ret_f = compute_return_distribution_features(prices)
    side_f = compute_sideways_features(prices)
    sr_f = compute_support_resistance_features(prices)
    vc_f = compute_volatility_contraction_features(prices)
    supply_f = compute_supply_features(supply, prices=prices)
    acc_f = compute_accumulation_features(prices, supply_features=supply_f)
    val_f = compute_valuation_features(financials)
    fib_angle_f = compute_fibonacci_angle_features(prices)

    liquidity = latest_liquidity_flags(prices)
    spike = latest_spike_flags(prices)
    overheat = latest_overheat_flags(prices)
    finrisk = financial_risk_flags(financials)

    # 2) 점수화
    side_s = score_sideways(side_f)
    sr_s = score_support_resistance(sr_f)
    supply_s = score_supply(supply_f)
    acc_s = score_accumulation(acc_f)
    val_s = score_valuation(val_f)
    fib_angle_s = score_fibonacci_angle(fib_angle_f)
    risk_s = score_risk(ret_f, liquidity, spike, overheat, finrisk)

    ranking = build_final_ranking(
        universe=universe,
        latest_price=prices,
        sideways_score=side_s,
        support_score=sr_s,
        supply_score=supply_s,
        accumulation_score=acc_s,
        valuation_score=val_s,
        risk_score=risk_s,
        fibonacci_angle_score=fib_angle_s,
        extra_features={"volatility_contraction": vc_f, "fibonacci_angle": fib_angle_f},
    )

    # feature 상세 저장
    output_dir.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(output_dir / "ranking_full.csv", index=False, encoding="utf-8-sig")
    save_paths = save_reports(ranking, output_dir, prices=prices)

    # 3) 4개 유명 트레이더식 독립 알고리즘 포트폴리오 생성
    try:
        config_path = _runtime_config("configs/multi_strategy_config.json", enabled_strategies, "runtime_multi_strategy_config.json") if enabled_strategies else None
        all_strategy_signals, multi_portfolio = build_fast_current_portfolio(
            prices=prices,
            universe=universe,
            financials=financials,
            config_path=config_path,
            enabled_strategies=enabled_strategies,
        )
        strategy_paths = save_multi_strategy_reports(all_strategy_signals, multi_portfolio, output_dir)
        print("멀티 전략 포트폴리오 생성 완료")
        print(strategy_paths)
    except Exception as e:
        print(f"멀티 전략 포트폴리오 생성 실패: {e}")

    summary = build_offline_summary(ranking)
    (output_dir / "offline_summary.txt").write_text(summary, encoding="utf-8")
    print("분석 완료")
    print(save_paths)
    print(summary)
    return ranking


def _save_env_from_form(form: dict[str, list[str]]) -> str:
    env_path = Path(".env")
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); existing[k] = v
    allowed = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CODE", "KIS_BASE_URL", "KIS_RATE_LIMIT_PER_SEC", "DART_API_KEY"]
    for k in allowed:
        if k in form:
            existing[k] = form[k][0].strip()
    if "KIS_BASE_URL" not in existing: existing["KIS_BASE_URL"] = "https://openapi.koreainvestment.com:9443"
    if "KIS_RATE_LIMIT_PER_SEC" not in existing: existing["KIS_RATE_LIMIT_PER_SEC"] = "2"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")
    return ".env 저장 완료"





def _fmt_money(v) -> str:
    try:
        x = float(v)
        if not np.isfinite(x):
            return "-"
        return f"{x:,.0f}"
    except Exception:
        return "-"


def _fmt_pct_num(v) -> str:
    try:
        x = float(v)
        if not np.isfinite(x):
            return "-"
        return f"{x:.2f}%"
    except Exception:
        return "-"


def _load_csv_safe(path: Path, **kwargs) -> pd.DataFrame:
    try:
        if path.exists() and path.stat().st_size > 0:
            return pd.read_csv(path, **kwargs)
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def _stock_chart_svg(pr: pd.DataFrame, levels: dict[str, float] | None = None, width: int = 1040, height: int = 520) -> str:
    """캔들형 차트 SVG. 진입/손절/목표/피벗 라인을 함께 표시한다."""
    if pr is None or pr.empty:
        return "<div class='empty'>차트 데이터 없음</div>"
    df = pr.copy().sort_values("date").tail(180)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) < 2:
        return "<div class='empty'>차트 데이터 부족</div>"
    df["ma20"] = df["close"].rolling(20, min_periods=5).mean()
    df["ma60"] = df["close"].rolling(60, min_periods=15).mean()
    df["ma200"] = df["close"].rolling(200, min_periods=60).mean()
    left, right, top, bottom = 76, 28, 34, 120
    cw, ch = width - left - right, height - top - bottom
    level_vals = []
    for v in (levels or {}).values():
        try:
            vv = float(v)
            if np.isfinite(vv):
                level_vals.append(vv)
        except Exception:
            pass
    ymin = float(np.nanmin([df["low"].min(), *(level_vals or [df["low"].min()])]))
    ymax = float(np.nanmax([df["high"].max(), *(level_vals or [df["high"].max()])]))
    pad = (ymax - ymin) * 0.06 if ymax > ymin else 1
    ymin -= pad; ymax += pad
    if ymax <= ymin:
        ymax = ymin + 1
    xs = np.linspace(left, left + cw, len(df))
    candle_w = max(2.2, min(10.0, cw / len(df) * 0.62))
    def y(v):
        try:
            vv = float(v)
            return top + (ymax - vv) / (ymax - ymin) * ch
        except Exception:
            return top + ch
    def ymap(vals):
        arr = pd.to_numeric(vals, errors="coerce").to_numpy(float)
        return top + (ymax - arr) / (ymax - ymin) * ch
    def line_for(col, color, width_line=1.55):
        vals = df[col]
        pts = []
        ys = ymap(vals)
        for x, yy, v in zip(xs, ys, vals):
            if pd.notna(v) and np.isfinite(yy):
                pts.append(f"{x:.1f},{yy:.1f}")
        return f"<polyline points='{' '.join(pts)}' fill='none' stroke='{color}' stroke-width='{width_line}' opacity='.95'/>" if len(pts) > 1 else ""
    candles = []
    for x, (_, r) in zip(xs, df.iterrows()):
        o, h, l, c = float(r['open']), float(r['high']), float(r['low']), float(r['close'])
        color = '#ef4444' if c >= o else '#3b82f6'
        yo, yc, yh, yl = y(o), y(c), y(h), y(l)
        body_y = min(yo, yc)
        body_h = max(1.4, abs(yo-yc))
        candles.append(f"<line x1='{x:.1f}' y1='{yh:.1f}' x2='{x:.1f}' y2='{yl:.1f}' stroke='{color}' stroke-width='1.2'/>")
        candles.append(f"<rect x='{x-candle_w/2:.1f}' y='{body_y:.1f}' width='{candle_w:.1f}' height='{body_h:.1f}' fill='{color}' opacity='.86' rx='1.4'/>")
    maxv = float(df["volume"].max()) if df["volume"].max() and np.isfinite(df["volume"].max()) else 1.0
    vol_top = top + ch + 26
    vol_h = 58
    volbars = []
    for x, v, o, c in zip(xs, df["volume"], df["open"], df["close"]):
        vh = 0 if not np.isfinite(v) else float(v) / maxv * vol_h
        color = '#ef4444' if c >= o else '#3b82f6'
        volbars.append(f"<rect x='{x-candle_w/2:.1f}' y='{vol_top+vol_h-vh:.1f}' width='{candle_w:.1f}' height='{vh:.1f}' fill='{color}' opacity='.35'/>")
    level_svg = []
    level_colors = {"entry": "#22c55e", "stop": "#ef4444", "target": "#a855f7", "pivot": "#f59e0b"}
    labels = {"entry": "진입가", "stop": "손절가", "target": "목표가", "pivot": "피벗"}
    for k, v in (levels or {}).items():
        try:
            vv = float(v)
            if not np.isfinite(vv):
                continue
            yy = y(vv)
            c = level_colors.get(k, "#64748b")
            level_svg.append(f"<line x1='{left}' y1='{yy:.1f}' x2='{left+cw}' y2='{yy:.1f}' stroke='{c}' stroke-width='2' stroke-dasharray='8 5'/><rect x='{left+cw-126}' y='{yy-18:.1f}' width='122' height='22' rx='6' fill='#0b1220' stroke='{c}'/><text x='{left+cw-118}' y='{yy-3:.1f}' fill='{c}' font-size='12' font-weight='800'>{labels.get(k,k)} {_fmt_money(vv)}</text>")
        except Exception:
            pass
    last = df.iloc[-1]
    return f"""
<svg viewBox='0 0 {width} {height}' class='terminal-chart'>
<defs><linearGradient id='bg' x1='0' x2='1'><stop offset='0' stop-color='#08111f'/><stop offset='1' stop-color='#0f172a'/></linearGradient></defs>
<rect x='0' y='0' width='{width}' height='{height}' rx='18' fill='url(#bg)'/>
<text x='{left}' y='22' fill='#e2e8f0' font-size='13' font-weight='800'>Candlestick · red=상승 / blue=하락</text>
<text x='{left+260}' y='22' fill='#facc15' font-size='12'>MA20</text><text x='{left+312}' y='22' fill='#fb923c' font-size='12'>MA60</text><text x='{left+365}' y='22' fill='#a78bfa' font-size='12'>MA200</text>
<line x1='{left}' y1='{top+ch}' x2='{left+cw}' y2='{top+ch}' stroke='#334155'/>
<line x1='{left}' y1='{top}' x2='{left}' y2='{top+ch}' stroke='#334155'/>
<line x1='{left}' y1='{top+ch*.25}' x2='{left+cw}' y2='{top+ch*.25}' stroke='#1e293b' stroke-dasharray='4 5'/>
<line x1='{left}' y1='{top+ch*.50}' x2='{left+cw}' y2='{top+ch*.50}' stroke='#1e293b' stroke-dasharray='4 5'/>
<line x1='{left}' y1='{top+ch*.75}' x2='{left+cw}' y2='{top+ch*.75}' stroke='#1e293b' stroke-dasharray='4 5'/>
{''.join(candles)}
{line_for('ma20','#facc15',1.6)}{line_for('ma60','#fb923c',1.6)}{line_for('ma200','#a78bfa',1.6)}
{''.join(level_svg)}
<text x='12' y='{top+10}' fill='#94a3b8' font-size='12'>{_fmt_money(ymax)}</text>
<text x='12' y='{top+ch}' fill='#94a3b8' font-size='12'>{_fmt_money(ymin)}</text>
{''.join(volbars)}
<line x1='{left}' y1='{vol_top+vol_h}' x2='{left+cw}' y2='{vol_top+vol_h}' stroke='#334155'/>
<text x='{left}' y='{height-18}' fill='#94a3b8' font-size='12'>{df['date'].iloc[0].strftime('%Y-%m-%d')}</text>
<text x='{left+cw-96}' y='{height-18}' fill='#94a3b8' font-size='12'>{df['date'].iloc[-1].strftime('%Y-%m-%d')}</text>
<text x='{left+cw-260}' y='22' fill='#e2e8f0' font-size='12'>Latest close {_fmt_money(last['close'])}</text>
</svg>"""


def _terminal_page() -> str:
    pf = _load_csv_safe(OUTPUT_DIR / "multi_strategy_portfolio.csv", dtype={"stock_code": str})
    sig = _load_csv_safe(OUTPUT_DIR / "multi_strategy_signals_all.csv", dtype={"stock_code": str})
    rows = []
    data = pf if not pf.empty else sig.head(80)
    for i, r in data.head(100).iterrows():
        code = str(r.get("stock_code", "")).zfill(6)
        rows.append(f"""
<tr>
<td>{i+1}</td><td><a href='/stock?code={code}'><b>{html.escape(str(r.get('stock_name', r.get('name',''))))}</b></a><br><span>{code}</span></td>
<td>{html.escape(str(r.get('strategies', r.get('strategy_id',''))))}</td>
<td>{html.escape(str(r.get('signals', r.get('signal',''))))}</td>
<td>{html.escape(str(r.get('best_strategy_score', r.get('strategy_score',''))))}</td>
<td>{_fmt_money(r.get('close', ''))}</td>
<td>{_fmt_money(r.get('entry_price', ''))}</td>
<td>{_fmt_money(r.get('stop_loss', ''))}</td>
<td>{_fmt_money(r.get('target_price', ''))}</td>
<td>{html.escape(str(r.get('portfolio_weight_pct','')))}</td>
</tr>""")
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Bloomberg식 전략 터미널</title>
<style>body{{margin:0;background:#07111f;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}.wrap{{max-width:1320px;margin:0 auto;padding:26px 18px 60px}}.hero{{background:linear-gradient(135deg,#020617,#1d4ed8);border:1px solid #1e293b;border-radius:24px;padding:24px}}a{{color:#38bdf8;text-decoration:none;font-weight:800}}.panel{{background:#0b1220;border:1px solid #1e293b;border-radius:18px;padding:16px;margin-top:16px}}table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #1e293b;padding:10px;text-align:left;font-size:13px}}th{{color:#93c5fd}}td span{{color:#94a3b8}}</style></head><body><main class='wrap'><section class='hero'><h1>전략 터미널</h1><p>4개 알고리즘 후보를 종목별로 비교하고, 종목명을 누르면 진입가·손절가·목표가·차트 상세 화면으로 이동합니다.</p><p><a href='/'>홈</a> · <a href='/charts'>차트</a> · <a href='/backtest-report'>백테스트</a></p></section><section class='panel'><table><thead><tr><th>#</th><th>종목</th><th>전략</th><th>신호</th><th>점수</th><th>현재가</th><th>진입가</th><th>손절가</th><th>목표가</th><th>비중%</th></tr></thead><tbody>{''.join(rows) if rows else '<tr><td colspan="10">분석 결과가 없습니다. 먼저 선택 전략 분석을 실행하세요.</td></tr>'}</tbody></table></section></main></body></html>"""


def _stock_detail_page(code: str) -> str:
    code = str(code).strip().replace(".0", "").zfill(6)
    prices = read_prices(DATA_DIR / "prices_daily.csv")
    prices["stock_code"] = prices["stock_code"].astype(str).str.zfill(6)
    pr = prices[prices["stock_code"] == code].copy()
    pf = _load_csv_safe(OUTPUT_DIR / "multi_strategy_portfolio.csv", dtype={"stock_code": str})
    sig = _load_csv_safe(OUTPUT_DIR / "multi_strategy_signals_all.csv", dtype={"stock_code": str})
    fin = _load_csv_safe(DATA_DIR / "financials.csv", dtype={"stock_code": str})
    sup = _load_csv_safe(DATA_DIR / "supply_daily.csv", dtype={"stock_code": str})
    if pr.empty:
        return f"<html><meta charset='utf-8'><body><h1>{code} 가격 데이터 없음</h1><p><a href='/terminal'>돌아가기</a></p></body></html>"
    pr["date"] = pd.to_datetime(pr["date"])
    for c in ["open", "high", "low", "close", "volume", "trading_value"]:
        pr[c] = pd.to_numeric(pr[c], errors="coerce")
    latest = pr.sort_values("date").iloc[-1]
    name = str(latest.get("stock_name", latest.get("name", code)))
    def ret(days):
        if len(pr) <= days:
            return np.nan
        return (latest["close"] / pr.sort_values("date").iloc[-days-1]["close"] - 1) * 100
    high52 = pr.tail(252)["high"].max(); low52 = pr.tail(252)["low"].min()
    pf_code = pf[pf["stock_code"].astype(str).str.zfill(6) == code] if not pf.empty and "stock_code" in pf else pd.DataFrame()
    sig_code = sig[sig["stock_code"].astype(str).str.zfill(6) == code] if not sig.empty and "stock_code" in sig else pd.DataFrame()
    best = pf_code.iloc[0].to_dict() if not pf_code.empty else (sig_code.sort_values('strategy_score', ascending=False).iloc[0].to_dict() if not sig_code.empty and 'strategy_score' in sig_code else {})
    levels = {
        "entry": best.get("entry_price", np.nan),
        "stop": best.get("stop_loss", np.nan),
        "target": best.get("target_price", np.nan),
        "pivot": best.get("pivot_price", best.get("entry_price", np.nan)),
    }
    chart = _stock_chart_svg(pr, levels=levels)
    strat_rows = []
    for _, r in sig_code.sort_values("strategy_score", ascending=False).head(12).iterrows() if not sig_code.empty else []:
        strat_rows.append(f"<tr><td>{html.escape(str(r.get('strategy_name','')))}</td><td>{html.escape(str(r.get('signal','')))}</td><td>{html.escape(str(r.get('strategy_score','')))}</td><td>{_fmt_money(r.get('entry_price'))}</td><td>{_fmt_money(r.get('stop_loss'))}</td><td>{_fmt_money(r.get('target_price'))}</td><td>{html.escape(str(r.get('reason','')))[:320]}</td></tr>")
    fin_html = "재무 CSV 데이터 없음"
    if not fin.empty and "stock_code" in fin:
        frow = fin[fin["stock_code"].astype(str).str.zfill(6) == code]
        if not frow.empty:
            fr = frow.iloc[-1]
            fin_html = " · ".join(f"{html.escape(str(k))}: {html.escape(str(fr.get(k,'')))}" for k in fin.columns if k != "stock_code")
    sup_html = "수급 CSV 데이터 없음"
    if not sup.empty and "stock_code" in sup:
        sr = sup[sup["stock_code"].astype(str).str.zfill(6) == code].tail(20)
        if not sr.empty:
            for col in ["institution_net_buy_value", "foreign_net_buy_value"]:
                if col in sr:
                    sr[col] = pd.to_numeric(sr[col], errors="coerce")
            inst = sr.get("institution_net_buy_value", pd.Series(dtype=float)).sum() if "institution_net_buy_value" in sr else np.nan
            foreign = sr.get("foreign_net_buy_value", pd.Series(dtype=float)).sum() if "foreign_net_buy_value" in sr else np.nan
            sup_html = f"최근 20개 데이터 합산 · 기관 {_fmt_money(inst)} / 외국인 {_fmt_money(foreign)}"
    stop = levels.get("stop", np.nan); entry = levels.get("entry", np.nan); target = levels.get("target", np.nan)
    risk = (entry / stop - 1) * 100 if pd.notna(stop) and stop and pd.notna(entry) else np.nan
    reward = (target / entry - 1) * 100 if pd.notna(target) and pd.notna(entry) and entry else np.nan
    rr = reward / risk if pd.notna(reward) and pd.notna(risk) and risk != 0 else np.nan
    cards = [
        ("현재가", _fmt_money(latest["close"])), ("거래대금", _fmt_money(latest.get("trading_value", np.nan))),
        ("20일 수익률", _fmt_pct_num(ret(20))), ("60일 수익률", _fmt_pct_num(ret(60))),
        ("52주 고점 대비", _fmt_pct_num((latest['close']/high52-1)*100 if high52 else np.nan)), ("손익비", f"{rr:.2f}" if pd.notna(rr) else "-"),
    ]
    card_html = "".join(f"<div class='card'><b>{k}</b><span>{v}</span></div>" for k, v in cards)
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(name)} 상세</title>
<style>body{{margin:0;background:#07111f;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}.wrap{{max-width:1240px;margin:0 auto;padding:26px 18px 70px}}.hero{{background:linear-gradient(135deg,#020617,#1d4ed8);border:1px solid #1e293b;border-radius:24px;padding:24px}}.grid{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:16px 0}}.card{{background:#0b1220;border:1px solid #1e293b;border-radius:16px;padding:14px}}.card b{{display:block;color:#93c5fd;font-size:12px}}.card span{{font-size:18px;font-weight:800}}.panel{{background:#0b1220;border:1px solid #1e293b;border-radius:18px;padding:16px;margin-top:16px}}a{{color:#38bdf8;text-decoration:none;font-weight:800}}table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #1e293b;padding:9px;text-align:left;font-size:13px}}th{{color:#93c5fd}}.terminal-chart{{width:100%;height:auto}}@media(max-width:900px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}</style></head><body><main class='wrap'><section class='hero'><h1>{html.escape(name)} <span style='color:#93c5fd'>{code}</span></h1><p>Bloomberg식 종목 상세: 캔들 차트에 진입가(초록), 손절가(빨강), 목표가(보라), 피벗(주황)을 표시합니다. 표의 근거는 어떤 알고리즘이 왜 후보로 선택했는지 보여줍니다.</p><p><a href='/terminal'>전략 터미널</a> · <a href='/multi-strategy'>포트폴리오</a> · <a href='/'>홈</a></p></section><div class='grid'>{card_html}</div><section class='panel'>{chart}</section><section class='panel'><h2>전략 신호 / 매매 기준</h2><table><thead><tr><th>전략</th><th>신호</th><th>점수</th><th>진입가</th><th>손절가</th><th>목표가</th><th>근거</th></tr></thead><tbody>{''.join(strat_rows) if strat_rows else '<tr><td colspan="7">현재 선택 전략 후보에 포함되지 않았습니다.</td></tr>'}</tbody></table></section><section class='panel'><h2>재무 요약</h2><p>{fin_html}</p></section><section class='panel'><h2>수급 요약</h2><p>{sup_html}</p></section></main></body></html>"""


def _charts_page() -> str:
    portfolio_path = OUTPUT_DIR / "multi_strategy_portfolio.csv"
    prices_path = DATA_DIR / "prices_daily.csv"
    cards = []
    if portfolio_path.exists() and prices_path.exists():
        pf = pd.read_csv(portfolio_path, dtype={"stock_code": str}).head(20)
        pr = read_prices(prices_path)
        for _, r in pf.iterrows():
            code = str(r.get("stock_code", "")).zfill(6)
            chart = _mini_chart_svg(pr[pr["stock_code"].astype(str).str.zfill(6) == code])
            cards.append(f"<section class='panel'><h2>{r.get('stock_name','')} <span>{code}</span></h2><p>{r.get('strategy_names','')} · 비중 {r.get('portfolio_weight_pct','')}%</p>{chart}</section>")
    else:
        cards.append("<section class='panel'><h2>차트 데이터 없음</h2><p>먼저 분석 실행을 누르세요.</p></section>")
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>차트 대시보드</title>
<style>body{{margin:0;background:#f6f7fb;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}.wrap{{max-width:1100px;margin:0 auto;padding:28px 18px 60px}}.hero{{background:linear-gradient(135deg,#111827,#2563eb);color:white;border-radius:24px;padding:28px}}.panel{{background:white;border:1px solid #e5e7eb;border-radius:18px;padding:18px;margin:16px 0;box-shadow:0 8px 24px rgba(15,23,42,.06)}}h2 span{{color:#64748b;font-size:14px}}a{{color:#2563eb;font-weight:800;text-decoration:none}}.spark-chart{{width:100%;max-width:760px}}.chart-bg{{fill:#f8fafc;stroke:#e5e7eb}}.grid-axis{{stroke:#cbd5e1}}.grid-line{{stroke:#e5e7eb;stroke-dasharray:4 4}}.price-line{{stroke:#2563eb;stroke-width:2.6}}.last-dot{{fill:#2563eb}}.volbar{{fill:#cbd5e1}}.axis-label,.chart-label{{fill:#64748b;font-size:11px}}.chart-value{{fill:#0f172a;font-size:13px;font-weight:800}}</style></head><body><main class='wrap'><section class='hero'><h1>전략 후보 차트 대시보드</h1><p>4전략 포트폴리오 상위 후보의 최근 가격/거래량 그래프입니다.</p><p><a href='/'>홈</a> · <a href='/backtest-report'>백테스트 리포트</a></p></section>{''.join(cards)}</main></body></html>"""


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _num_input(name: str, label: str, value: str, help_text: str, step: str = "any") -> str:
    return f"""<label class='field'><span>{html.escape(label)}</span><input name='{name}' value='{html.escape(str(value))}' step='{step}'><em>{html.escape(help_text)}</em></label>"""


def _data_status_html() -> str:
    items = []
    for label, path in [
        ("가격 일봉", DATA_DIR / "prices_daily.csv"),
        ("종목 목록", DATA_DIR / "universe.csv"),
        ("재무", DATA_DIR / "financials.csv"),
        ("수급", DATA_DIR / "supply_daily.csv"),
        ("주봉 30주선", DATA_DIR / "prices_weekly.csv"),
        ("전략 후보", OUTPUT_DIR / "multi_strategy_portfolio.csv"),
        ("백테스트", OUTPUT_DIR / "backtest_summary.csv"),
    ]:
        if path.exists():
            try:
                size = path.stat().st_size / 1024 / 1024
                mod = pd.Timestamp(path.stat().st_mtime, unit='s').strftime('%m-%d %H:%M')
                status = f"OK · {size:.1f}MB · {mod}"
                cls = "ok"
            except Exception:
                status = "OK"; cls = "ok"
        else:
            status = "없음"; cls = "bad"
        items.append(f"<div class='status {cls}'><b>{label}</b><span>{status}</span></div>")
    return "".join(items)


def _backtest_summary_html() -> str:
    sm = _load_csv_safe(OUTPUT_DIR / "backtest_summary.csv")
    if sm.empty:
        return "<p class='muted'>아직 백테스트 결과가 없습니다. 전략 체크 후 백테스트를 실행하세요.</p>"
    row = sm.iloc[0]
    keys = [("총수익률", "total_return_pct"), ("CAGR", "cagr_pct"), ("MDD", "max_drawdown_pct"), ("Sharpe", "sharpe"), ("승률", "win_rate_pct"), ("PF", "profit_factor"), ("거래수", "num_trades")]
    cards = []
    for label, k in keys:
        v = row.get(k, "-")
        if "pct" in k or k.endswith("rate_pct"):
            val = _fmt_pct_num(v)
        else:
            try:
                val = f"{float(v):.2f}" if k != "num_trades" else f"{int(float(v)):,}"
            except Exception:
                val = html.escape(str(v))
        cards.append(f"<div class='mini'><b>{label}</b><span>{val}</span></div>")
    return "".join(cards) + "<p class='muted'>MDD는 고점 대비 최대 낙폭, PF는 총이익/총손실입니다. 실전형은 수익률뿐 아니라 MDD·승률·PF를 같이 봐야 합니다.</p>"


def _save_backtest_config_from_form(form: dict[str, list[str]], selected: list[str]) -> Path:
    base = load_json(Path("configs/backtest_config.json"))
    if not base:
        from backtest.multi_strategy_backtester import DEFAULT_CONFIG
        base = json.loads(json.dumps(DEFAULT_CONFIG))
    def fval(name, default, scale=1.0):
        try:
            raw = str(form.get(name, [default])[0]).strip()
            return float(raw if raw != "" else default) * scale
        except Exception:
            return float(default) * scale
    def ival(name, default):
        try:
            raw = str(form.get(name, [default])[0]).strip()
            return int(float(raw if raw != "" else default))
        except Exception:
            return int(default)
    base["enabled_strategies"] = selected
    base["initial_cash"] = ival("initial_cash", base.get("initial_cash", 10000000))
    base["test_years"] = ival("test_years", base.get("test_years", 3))
    base["max_backtest_symbols"] = ival("max_backtest_symbols", base.get("max_backtest_symbols", 500))
    base["min_trading_value"] = ival("min_trading_value", base.get("min_trading_value", 300000000))
    base["buy_commission_rate"] = fval("buy_commission_pct", 0.015, 0.01)
    base["sell_commission_rate"] = fval("sell_commission_pct", 0.015, 0.01)
    base["sell_tax_rate"] = fval("sell_tax_pct", 0.20, 0.01)
    base["slippage_rate"] = fval("slippage_pct", 0.10, 0.01)
    ro = base.get("risk_overlay", {}) or {}
    ro["enabled"] = bool(form.get("risk_overlay_enabled"))
    ro["block_new_entries_in_risk_off"] = bool(form.get("block_risk_off"))
    ro["max_total_open_positions"] = ival("max_total_open_positions", ro.get("max_total_open_positions", 20))
    ro["cooldown_after_stop_days"] = ival("cooldown_after_stop_days", ro.get("cooldown_after_stop_days", 10))
    base["risk_overlay"] = ro
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "web_backtest_config.json"
    out.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def download_yfinance_web(period: str = "5y", batch_size: int = 50, sleep_sec: float = 0.7, limit: int | None = None, rebuild_universe: bool = False) -> None:
    from data_sources.yfinance_kr import build_krx_universe, download_prices_yfinance
    p = paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if rebuild_universe or not p["universe"].exists():
        build_krx_universe(p["universe"], markets=("KOSPI", "KOSDAQ"))
    result = download_prices_yfinance(
        universe_csv=p["universe"],
        output_csv=p["prices"],
        period=period,
        interval="1d",
        batch_size=batch_size,
        sleep_sec=sleep_sec,
        limit=limit,
        progress=True,
    )
    print(f"yfinance 저장 완료: {p['prices']} prices={len(result.prices):,} failures={len(result.failures):,}")


def _strategy_control_html(action: str, button: str, include_backtest_inputs: bool = False) -> str:
    extra = ""
    if include_backtest_inputs:
        extra = f"""
        <div class='subgrid'>
        {_num_input('initial_cash','초기자금','10000000','백테스트 시작 계좌 금액. 예: 10000000 = 천만원')}
        {_num_input('test_years','평가기간(년)','3','최근 몇 년을 실제 평가구간으로 볼지. 3이면 최근 3년 평가')}
        {_num_input('max_backtest_symbols','백테스트 종목 수','500','거래대금 상위 몇 개 종목만 테스트할지. 0이면 전체')}
        {_num_input('min_trading_value','최소 거래대금','300000000','너무 작은 종목 제외 기준. 예: 300000000 = 3억원')}
        {_num_input('buy_commission_pct','매수 수수료(%)','0.015','증권사 수수료. 0.015는 0.015%')}
        {_num_input('sell_commission_pct','매도 수수료(%)','0.015','매도 수수료. 보통 매수와 동일')}
        {_num_input('sell_tax_pct','매도 거래세(%)','0.20','국내 주식 매도세/제세금 보수적 가정. 예: 0.20 = 0.20%')}
        {_num_input('slippage_pct','슬리피지(%)','0.10','실제 체결 불리함. 0.10 = 0.1% 불리하게 체결')}
        {_num_input('max_total_open_positions','전체 최대 보유종목','20','동시에 들고 갈 최대 종목 수')}
        {_num_input('cooldown_after_stop_days','손절 후 재진입 제한일','10','손절된 종목은 며칠 동안 다시 사지 않을지')}
        </div>
        <label class='toggle'><input type='checkbox' name='risk_overlay_enabled' checked> 리스크 오버레이 사용: 시장상태·현금비중·트레일링스탑 적용</label>
        <label class='toggle'><input type='checkbox' name='block_risk_off'> 위험장에서는 신규진입 차단</label>
        """
    return f"""<form method='post' action='{action}'>{_strategy_checkbox_html()} {extra}<button>{button}</button></form>"""


def _home_page(env_vals: dict[str, str]) -> str:
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Stock Swing Terminal v5</title>
<style>
:root{{--bg:#07111f;--panel:#0b1220;--line:#1e293b;--text:#e5e7eb;--muted:#94a3b8;--blue:#38bdf8}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at top left,#102a50,#07111f 42%,#020617);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}
.wrap{{max-width:1440px;margin:0 auto;padding:24px 18px 70px}} .hero{{display:grid;grid-template-columns:1.5fr .9fr;gap:18px;align-items:stretch}}
.box,.card{{background:rgba(11,18,32,.92);border:1px solid var(--line);border-radius:24px;box-shadow:0 18px 44px rgba(0,0,0,.25)}} .box{{padding:26px}} .card{{padding:18px}}
h1{{margin:0 0 8px;font-size:34px;letter-spacing:-.02em}} h2{{margin:0 0 12px;font-size:20px}} h3{{margin:0 0 8px}} p{{color:var(--muted);line-height:1.55}} a{{color:var(--blue);font-weight:800;text-decoration:none}}
.nav{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}} .nav a,.pill{{border:1px solid #334155;background:#0f172a;border-radius:999px;padding:9px 12px;color:#dbeafe;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:16px}} .wide{{grid-column:span 2}} .full{{grid-column:1/-1}}
.statusgrid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}} .status{{border:1px solid #243244;border-radius:14px;padding:12px;background:#08111f}} .status b{{display:block;color:#93c5fd;font-size:12px}} .status span{{font-weight:800}} .status.ok span{{color:#86efac}} .status.bad span{{color:#fca5a5}}
.minirow{{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px}} .mini{{background:#08111f;border:1px solid #243244;border-radius:14px;padding:12px}} .mini b{{display:block;color:#94a3b8;font-size:11px}} .mini span{{font-size:18px;font-weight:900}}
input,select{{width:100%;padding:11px 12px;margin:5px 0 0;border:1px solid #334155;border-radius:12px;background:#07111f;color:#e5e7eb;font-size:14px}} button{{width:100%;padding:13px 16px;margin-top:12px;border:0;border-radius:14px;background:#2563eb;color:white;font-weight:900;cursor:pointer}}
button.secondary{{background:#0f766e}} .subgrid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}} .field span{{display:block;color:#bfdbfe;font-size:13px;font-weight:800;margin-top:8px}} .field em{{display:block;color:#94a3b8;font-style:normal;font-size:12px;line-height:1.35;margin-top:4px}} .check,.toggle{{display:block;margin:7px 0;padding:10px;border:1px solid #334155;background:#08111f;border-radius:13px;color:#dbeafe}} .check input,.toggle input{{width:auto;margin-right:7px}}
.help{{background:#0f172a;border-left:4px solid #38bdf8;border-radius:12px;padding:12px;color:#cbd5e1;font-size:13px;line-height:1.55}} .muted{{color:#94a3b8;font-size:13px}}
@media(max-width:980px){{.hero,.grid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}.minirow,.statusgrid,.subgrid{{grid-template-columns:1fr}}}}
</style></head><body><main class='wrap'>
<section class='hero'><div class='box'><h1>Stock Swing Terminal v5</h1><p>웹에서 데이터 수집, 4전략 체크 분석, 백테스트 설정, 종목별 캔들 차트와 진입/손절/목표가 확인까지 한 번에 통제합니다.</p><div class='nav'><a href='/terminal'>전략 터미널</a><a href='/charts'>차트 대시보드</a><a href='/backtest-report'>백테스트 리포트</a><a href='/multi-strategy'>포트폴리오</a><a href='/job-status'>작업 상태</a></div></div><div class='box'><h2>데이터 상태</h2><div class='statusgrid'>{_data_status_html()}</div></div></section>
<section class='grid'><div class='card wide'><h2>백테스트 요약</h2><div class='minirow'>{_backtest_summary_html()}</div></div><div class='card'><h2>추천 실행 순서</h2><p class='help'>1) 가격 CSV 확인 → 2) 주봉/파생 생성 → 3) 전략 체크 분석 → 4) 같은 체크 조합으로 백테스트 → 5) 터미널에서 종목 클릭 후 캔들 차트 확인.</p></div>
<div class='card'><h2>API 키 저장</h2><p class='muted'>DART/KIS를 나중에 실시간·재무 수집으로 전환할 때 씁니다. 빈칸은 그대로 둬도 됩니다.</p><form method='post' action='/save-env'><label class='field'><span>DART API KEY</span><input name='DART_API_KEY' value='{env_vals.get('DART_API_KEY','')}'><em>OpenDART 개인 인증키. 재무 CSV 수집에 필요</em></label><label class='field'><span>KIS APP KEY</span><input name='KIS_APP_KEY' value='{env_vals.get('KIS_APP_KEY','')}'><em>한국투자증권 실시간/주문 API용</em></label><label class='field'><span>KIS APP SECRET</span><input name='KIS_APP_SECRET' value='{env_vals.get('KIS_APP_SECRET','')}'><em>한국투자증권 앱 시크릿</em></label><button>API 키 저장</button></form></div>
<div class='card wide'><h2>가격 CSV 수집: yfinance</h2><p class='muted'>현재는 yfinance 기반 과거 가격 수집. 나중에는 이 영역을 KIS/키움 실시간 API로 교체할 수 있게 분리해뒀습니다.</p><form method='post' action='/download-yfinance-web'><div class='subgrid'><label class='field'><span>수집 기간</span><select name='period'><option value='1y'>1년</option><option value='3y'>3년</option><option value='5y' selected>5년</option><option value='10y'>10년</option></select><em>스윙 백테스트는 보통 3~5년 권장</em></label><label class='field'><span>테스트 종목 수 제한</span><input name='limit' value=''><em>비우면 전체. 테스트는 20 또는 100 입력</em></label><label class='field'><span>배치 크기</span><input name='batch_size' value='50'><em>한 번에 요청할 종목 수. 실패가 많으면 20~30</em></label><label class='field'><span>요청 간격(초)</span><input name='sleep_sec' value='0.7'><em>기숙사/와이파이가 불안정하면 1.0~1.5</em></label></div><label class='toggle'><input type='checkbox' name='rebuild_universe'> KRX 종목목록을 새로 만들기</label><button>yfinance 가격 CSV 수집 시작</button></form></div>
<div class='card'><h2>파생 데이터</h2><p class='muted'>주봉 30주선, 섹터 강도 등 전략 계산용 파일을 만듭니다.</p><form method='post' action='/build-derived'><button class='secondary'>주봉/파생 데이터 생성</button></form></div>
<div class='card wide'><h2>4개 알고리즘 체크 분석</h2><p class='muted'>체크한 알고리즘만 후보를 만들고, 선택 전략의 비중을 자동으로 100%로 재분배합니다.</p>{_strategy_control_html('/analyze-selected','선택 전략 분석 실행')}</div>
<div class='card full'><h2>선택 전략 백테스트 설정</h2><p class='help'>이 입력값들은 “백테스트 가정”입니다. 수수료/세금/슬리피지를 넣어야 실제에 가까워집니다. 숫자 단위는 아래 설명을 보고 넣으세요.</p>{_strategy_control_html('/backtest-selected-web','선택 전략 백테스트 실행', True)}</div>
<div class='card'><h2>DART 재무 CSV</h2><form method='post' action='/download-dart'>{_num_input('years','수집 연수','5','최근 몇 년 분기보고서를 받을지')}{_num_input('parts','총 분할 수','3','하루 제한 때문에 3 또는 4 권장')}{_num_input('part','이번 분할 번호','1','1일차면 1, 2일차면 2')}{_num_input('sleep_sec','요청 간격 초','0.15','제한/실패가 많으면 0.3~0.5')}<button>DART 분할 수집</button></form></div>
<div class='card'><h2>기관/외국인 수급</h2><form method='post' action='/download-supply-part'>{_num_input('days','거래일 수','1250','5년치면 대략 1250')}{_num_input('parts','총 분할 수','3','종목을 몇 묶음으로 나눌지')}{_num_input('part','이번 분할 번호','1','이번에 받을 묶음 번호')}<button class='secondary'>수급 분할 수집</button></form></div>
<div class='card'><h2>작업/파일</h2><p><a href='/job-status'>현재 백그라운드 작업 상태</a></p><p><a href='/backtest_trades.csv'>거래내역 CSV</a></p><p><a href='/multi_strategy_signals_all.csv'>전략 신호 CSV</a></p><p><a href='/backtest_market_regime.csv'>시장상태 CSV</a></p></div>
</section></main></body></html>"""


def serve(port: int = 8765) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
            self.send_response(status); self.send_header("Content-Type", content_type); self.end_headers(); self.wfile.write(body.encode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            # 리포트 및 산출물 파일 제공
            if parsed.path.startswith("/terminal"):
                self._send(_terminal_page()); return
            if parsed.path.startswith("/stock"):
                qs = parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                self._send(_stock_detail_page(code)); return
            if self.path.startswith("/multi-strategy"):
                path = OUTPUT_DIR / "multi_strategy_portfolio.html"
                if path.exists():
                    self._send(path.read_text(encoding="utf-8")); return
                self._send("<html><meta charset='utf-8'><body><h1>아직 멀티 전략 리포트가 없습니다.</h1><p>먼저 분석 실행을 눌러주세요.</p><p><a href='/'>돌아가기</a></p></body></html>"); return
            if self.path.startswith("/charts"):
                self._send(_charts_page()); return
            if self.path.startswith("/backtest-report"):
                path = OUTPUT_DIR / "backtest_report.html"
                if path.exists():
                    self._send(path.read_text(encoding="utf-8")); return
                self._send("<html><meta charset='utf-8'><body><h1>아직 백테스트 리포트가 없습니다.</h1><p>먼저 백테스트 실행을 누르세요.</p><p><a href='/'>돌아가기</a></p></body></html>"); return
            if self.path.startswith("/job-status"):
                import json as _json
                self._send(_json.dumps(JOB_STATE, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8"); return
            if self.path.startswith("/report"):
                path = OUTPUT_DIR / "daily_signal_report.html"
                if path.exists():
                    self._send(path.read_text(encoding="utf-8")); return
                self._send("<html><meta charset='utf-8'><body><h1>아직 리포트가 없습니다.</h1><p>먼저 분석 실행을 눌러주세요.</p><p><a href='/'>돌아가기</a></p></body></html>"); return
            if self.path in ["/daily_signal_report.csv", "/daily_signal_report.json", "/daily_signal_report.md", "/ranking_full.csv", "/multi_strategy_portfolio.csv", "/multi_strategy_signals_all.csv", "/multi_strategy_portfolio.json", "/backtest_equity_curve.csv", "/backtest_trades.csv", "/backtest_summary.csv", "/backtest_strategy_summary.csv", "/backtest_signals.csv", "/backtest_market_regime.csv"]:
                fname = self.path.strip("/")
                path = OUTPUT_DIR / fname
                if path.exists():
                    ctype = "text/csv; charset=utf-8" if fname.endswith(".csv") else "application/json; charset=utf-8" if fname.endswith(".json") else "text/plain; charset=utf-8"
                    self._send(path.read_text(encoding="utf-8"), content_type=ctype); return
                self._send("파일이 아직 없습니다.", status=404, content_type="text/plain; charset=utf-8"); return

            env_vals = {}
            if Path(".env").exists():
                for line in Path(".env").read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1); env_vals[k] = v
            self._send(_home_page(env_vals))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
            try:
                if self.path == "/save-env":
                    msg = _save_env_from_form(form)
                elif self.path == "/download-naver":
                    download_data(source="naver", days=250)
                    msg = "네이버/pykrx 다운로드 완료"
                elif self.path == "/download-yfinance":
                    _run_job("yfinance 1년 가격 CSV 수집", download_data, source="yfinance", days=250, skip_supply=True, skip_financials=True)
                    msg = "yfinance 국내 전체 1년 가격 CSV 수집 시작. 작업상태에서 확인하세요."
                elif self.path == "/download-yfinance-web":
                    period = form.get("period", ["5y"])[0]
                    batch_size = int(float(form.get("batch_size", ["50"])[0] or 50))
                    sleep_sec = float(form.get("sleep_sec", ["0.7"])[0] or 0.7)
                    limit_raw = str(form.get("limit", [""])[0]).strip()
                    limit = int(float(limit_raw)) if limit_raw else None
                    rebuild = bool(form.get("rebuild_universe"))
                    _run_job(f"yfinance {period} 가격 CSV 수집", download_yfinance_web, period=period, batch_size=batch_size, sleep_sec=sleep_sec, limit=limit, rebuild_universe=rebuild)
                    msg = f"yfinance {period} 가격 CSV 수집 시작. 작업상태에서 확인하세요."
                elif self.path == "/download-kis":
                    # .env 저장 후 현재 프로세스에는 반영되지 않을 수 있어 다시 로드
                    try:
                        from dotenv import load_dotenv; load_dotenv(override=True)
                    except Exception: pass
                    download_data(source="kis", days=250)
                    msg = "KIS/pykrx 다운로드 완료"
                elif self.path == "/download-dart":
                    part = int(form.get("part", ["1"])[0]); parts = int(form.get("parts", ["3"])[0]); years = int(form.get("years", ["5"])[0]); sleep_sec = float(form.get("sleep_sec", ["0.15"])[0])
                    _run_job(f"DART 재무 수집 {part}/{parts}", download_dart_data, part=part, parts=parts, years=years, sleep_sec=sleep_sec)
                    msg = f"DART 재무 수집 시작: {part}/{parts}. 작업상태에서 확인하세요."
                elif self.path == "/download-supply-part":
                    part = int(form.get("part", ["1"])[0]); parts = int(form.get("parts", ["3"])[0]); days = int(form.get("days", ["1250"])[0])
                    _run_job(f"수급 수집 {part}/{parts}", download_supply_partition, days=days, part=part, parts=parts)
                    msg = f"수급 수집 시작: {part}/{parts}. 작업상태에서 확인하세요."
                elif self.path == "/build-derived":
                    build_derived_data()
                    msg = "주봉/섹터 파생 데이터 생성 완료"
                elif self.path == "/backtest-selected":
                    selected = _selected_strategies_from_form(form)
                    run_backtest(enabled_strategies=selected)
                    msg = "선택 전략 백테스트 완료: " + ", ".join(selected)
                elif self.path == "/backtest-selected-web":
                    selected = _selected_strategies_from_form(form)
                    cfg = _save_backtest_config_from_form(form, selected)
                    run_backtest(config_path=cfg, enabled_strategies=selected)
                    msg = "웹 설정 백테스트 완료: " + ", ".join(selected)
                elif self.path == "/analyze-selected":
                    selected = _selected_strategies_from_form(form)
                    analyze(enabled_strategies=selected)
                    msg = "선택 전략 분석 완료: " + ", ".join(selected)
                elif self.path == "/backtest":
                    run_backtest()
                    msg = "백테스트 완료"
                elif self.path == "/analyze":
                    analyze(enabled_strategies=STRATEGY_IDS.copy())
                    msg = "전체 4전략 분석 완료"
                else:
                    msg = "알 수 없는 요청"
                self._send(f"<html><meta charset='utf-8'><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial;background:#07111f;color:#e5e7eb;padding:32px'><div style='max-width:760px;margin:auto;background:#0b1220;border:1px solid #1e293b;border-radius:22px;padding:26px'><h1>{msg}</h1><p style='color:#94a3b8'>긴 작업은 백그라운드로 진행됩니다. 작업상태를 열어 진행 여부를 확인하세요.</p><p><a style='color:#38bdf8;font-weight:800' href='/'>홈</a> · <a style='color:#38bdf8;font-weight:800' href='/job-status'>작업상태</a> · <a style='color:#38bdf8;font-weight:800' href='/terminal'>전략 터미널</a> · <a style='color:#38bdf8;font-weight:800' href='/backtest-report'>백테스트</a></p></div></body></html>")
            except Exception as e:
                self._send(f"<html><meta charset='utf-8'><body><h1>오류</h1><pre>{e}</pre><p><a href='/'>돌아가기</a></p></body></html>", status=500)
    print(f"로컬 사이트 실행: http://127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()




def inspect_quantking_db(db_path: str | Path, output_dir: Path = OUTPUT_DIR) -> pd.DataFrame:
    """QuantKing SQLite DB 테이블/컬럼 인벤토리를 CSV로 저장한다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "quantking_db_inventory.csv"
    inv = write_table_inventory(db_path, out_path)
    print(f"QuantKing DB 인벤토리 저장: {out_path}")
    print(inv.head(30).to_string(index=False))
    return inv


def import_quantking_db(db_path: str | Path, output_dir: Path = DATA_DIR, table: str | None = None, column_map: str | None = None, limit_rows: int | None = None) -> None:
    """QuantKing SQLite DB를 data/csv_import CSV로 변환한다."""
    result = convert_quantking_sqlite_to_csv(
        db_path=db_path,
        output_dir=output_dir,
        table=table,
        column_map_path=column_map,
        limit_rows=limit_rows,
    )
    print(f"변환 완료: table={result.source_table}")
    print(f"prices_daily.csv rows: {len(result.prices):,}")
    print(f"universe.csv rows: {len(result.universe):,}")
    if result.warnings:
        print("주의:")
        for w in result.warnings:
            print(f"- {w}")



def _strategies_from_cli(value: str | None) -> list[str] | None:
    if not value or str(value).strip().lower() in {"all", "전체", ""}:
        return STRATEGY_IDS.copy()
    selected = [x.strip().lower() for x in str(value).split(",") if x.strip().lower() in STRATEGY_IDS]
    return selected or STRATEGY_IDS.copy()

def parse_args():
    parser = argparse.ArgumentParser(description="수학·수급 기반 장기 스윙 후보 탐색기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-templates")
    d = sub.add_parser("download")
    d.add_argument("--source", choices=["naver", "kis", "kiwoom", "yfinance"], default="naver")
    d.add_argument("--days", type=int, default=250)
    d.add_argument("--limit", type=int, default=None, help="테스트용 종목 수 제한")
    d.add_argument("--skip-supply", action="store_true")
    d.add_argument("--skip-financials", action="store_true")
    an = sub.add_parser("analyze")
    an.add_argument("--strategies", default="all", help="vcp,canslim,stage2,darvas 또는 all")
    sub.add_parser("build-derived", help="prices_daily.csv에서 prices_weekly.csv와 sector_strength.csv 생성")
    bt = sub.add_parser("backtest", help="4전략 포트폴리오 백테스트 실행")
    bt.add_argument("--config", default=None)
    bt.add_argument("--strategies", default="all", help="vcp,canslim,stage2,darvas 또는 all")
    dart = sub.add_parser("download-dart", help="OpenDART 분기 재무 CSV 수집")
    dart.add_argument("--part", type=int, default=1)
    dart.add_argument("--parts", type=int, default=3)
    dart.add_argument("--years", type=int, default=5)
    dart.add_argument("--limit", type=int, default=None)
    dart.add_argument("--sleep-sec", type=float, default=0.15)
    sp = sub.add_parser("download-supply-part", help="pykrx 기관/외국인 수급 CSV 분할 수집")
    sp.add_argument("--part", type=int, default=1)
    sp.add_argument("--parts", type=int, default=3)
    sp.add_argument("--days", type=int, default=1250)
    sp.add_argument("--limit", type=int, default=None)
    q = sub.add_parser("inspect-quantking-db", help="QuantKing SQLite DB 테이블/컬럼 구조 확인")
    q.add_argument("--db", required=True, help="QuantKing .db/.sqlite 경로")
    q.add_argument("--output-dir", default=str(OUTPUT_DIR))
    iq = sub.add_parser("import-quantking-db", help="QuantKing SQLite DB를 엔진 CSV로 변환")
    iq.add_argument("--db", required=True, help="QuantKing .db/.sqlite 경로")
    iq.add_argument("--output-dir", default=str(DATA_DIR))
    iq.add_argument("--table", default=None, help="가격 테이블명. 생략 시 자동 탐지")
    iq.add_argument("--column-map", default=None, help="c컬럼 매핑 JSON 경로")
    iq.add_argument("--limit-rows", type=int, default=None, help="테스트 변환용 행 제한")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    a = sub.add_parser("all")
    a.add_argument("--source", choices=["naver", "kis", "kiwoom", "yfinance"], default="naver")
    a.add_argument("--days", type=int, default=250)
    a.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.cmd == "init-templates":
        init_templates()
    elif args.cmd == "download":
        download_data(args.source, args.days, args.limit, args.skip_supply, args.skip_financials)
    elif args.cmd == "analyze":
        analyze(enabled_strategies=_strategies_from_cli(getattr(args, "strategies", None)))
    elif args.cmd == "build-derived":
        build_derived_data()
    elif args.cmd == "backtest":
        run_backtest(config_path=args.config, enabled_strategies=_strategies_from_cli(getattr(args, "strategies", None)))
    elif args.cmd == "download-dart":
        download_dart_data(part=args.part, parts=args.parts, years=args.years, limit=args.limit, sleep_sec=args.sleep_sec)
    elif args.cmd == "download-supply-part":
        download_supply_partition(days=args.days, part=args.part, parts=args.parts, limit=args.limit)
    elif args.cmd == "inspect-quantking-db":
        inspect_quantking_db(args.db, Path(args.output_dir))
    elif args.cmd == "import-quantking-db":
        import_quantking_db(args.db, Path(args.output_dir), args.table, args.column_map, args.limit_rows)
    elif args.cmd == "serve":
        serve(args.port)
    elif args.cmd == "all":
        download_data(args.source, args.days, args.limit)
        analyze()
