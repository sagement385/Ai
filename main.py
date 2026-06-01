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
from tools.ai_theme_mode import build_ai_theme_report
from ai.ai_copilot import CopilotSettings, run_copilot, apply_patch_proposal

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


STRATEGY_IDS = ["vcp", "canslim", "stage2", "darvas", "deep_turnaround"]
STRATEGY_LABELS = {
    "vcp": "1. Minervini VCP",
    "canslim": "2. CANSLIM",
    "stage2": "3. Weinstein Stage2",
    "darvas": "4. Darvas Box",
    "deep_turnaround": "5. Deep Turnaround 10x",
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





def run_ultra10x_optimizer(max_candidates: int | None = None) -> dict:
    """초공격 집중형 10x 프로필을 과최적화 방지 절차로 탐색한다.

    절차: train에서 후보를 만들고 validation으로 선택, test는 선택 후 1회만 평가한다.
    이 함수는 수익률을 높이기 위한 탐색을 하되, 테스트 구간을 반복 튜닝에 쓰지 않도록 고정한다.
    """
    from tools.ultra10x_optimizer import run_ultra10x_optimization
    p = paths()
    result = run_ultra10x_optimization(p["prices"], OUTPUT_DIR, max_candidates=max_candidates)
    print("초공격 10x 최적화 완료")
    for k, v in result.items():
        print(f"{k}: {v}")
    return result

def _prefilter_prices_for_analysis(prices: pd.DataFrame, max_symbols: int | None = None, lookback_rows: int = 80) -> tuple[pd.DataFrame, dict[str, int]]:
    """대형 2700종목 CSV에서 분석 시간을 줄이고 실거래성을 높이기 위한 최근 거래대금 상위 필터."""
    if prices is None or prices.empty:
        return prices, {"symbols_before": 0, "symbols_after": 0}
    if max_symbols is None:
        max_symbols = int(os.getenv("ANALYZE_MAX_SYMBOLS", "500"))
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




def run_ai_theme_mode(max_per_theme: int = 50, min_score: float = 45.0) -> tuple[pd.DataFrame, pd.DataFrame, Path]:
    candidates, summary, html_path = build_ai_theme_report(DATA_DIR, OUTPUT_DIR, max_per_theme=max_per_theme, min_score=min_score)
    print(f"AI 장기 테마 분석 완료: {html_path}")
    print(f"테마 후보: {len(candidates):,}개")
    return candidates, summary, html_path

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
    allowed = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CODE", "KIS_BASE_URL", "KIS_RATE_LIMIT_PER_SEC", "DART_API_KEY", "AI_PROVIDER", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL", "AI_TEMPERATURE"]
    secret_keys = {"AI_API_KEY", "DART_API_KEY", "KIS_APP_SECRET", "KIS_APP_KEY"}
    for k in allowed:
        if k in form:
            val = form[k][0].strip()
            # 빈 비밀번호 입력으로 기존 키가 지워지는 것을 방지
            if k in secret_keys and val == "" and existing.get(k):
                continue
            existing[k] = val
    if "KIS_BASE_URL" not in existing: existing["KIS_BASE_URL"] = "https://openapi.koreainvestment.com:9443"
    if "KIS_RATE_LIMIT_PER_SEC" not in existing: existing["KIS_RATE_LIMIT_PER_SEC"] = "2"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")
    return ".env 저장 완료"


def _save_ai_settings_from_form(form: dict[str, list[str]]) -> CopilotSettings:
    """AI provider settings are saved in .env but never rendered back with the full key."""
    _save_env_from_form(form)
    try:
        from dotenv import load_dotenv; load_dotenv(override=True)
    except Exception:
        pass
    return CopilotSettings.from_env()


def _masked_key(v: str) -> str:
    v = str(v or "")
    if not v:
        return ""
    if len(v) <= 8:
        return "********"
    return v[:4] + "..." + v[-4:]


def _ai_copilot_page(env_vals: dict[str, str] | None = None) -> str:
    env_vals = env_vals or {}
    provider = env_vals.get("AI_PROVIDER", "offline") or "offline"
    model = env_vals.get("AI_MODEL", "")
    base_url = env_vals.get("AI_BASE_URL", "")
    temp = env_vals.get("AI_TEMPERATURE", "0.2")
    has_key = "저장됨: " + _masked_key(env_vals.get("AI_API_KEY", "")) if env_vals.get("AI_API_KEY") else "저장된 API 키 없음"
    last_html = OUTPUT_DIR / "ai_copilot_result.html"
    patch_json = OUTPUT_DIR / "ai_copilot_patch_proposal.json"
    result_link = "<a href='/ai-copilot-result'>최근 AI 결과 보기</a>" if last_html.exists() else "최근 AI 결과 없음"
    patch_link = "<a href='/ai-patch-preview'>최근 패치 미리보기</a>" if patch_json.exists() else "패치 제안 없음"
    providers = [
        ("offline", "offline / 테스트용"),
        ("openai", "OpenAI"),
        ("openai_compatible", "OpenAI 호환 API"),
        ("openrouter", "OpenRouter"),
        ("groq", "Groq"),
        ("deepseek", "DeepSeek"),
        ("ollama", "Ollama 로컬"),
    ]
    opts = "".join(f"<option value='{html.escape(k)}' {'selected' if provider==k else ''}>{html.escape(v)}</option>" for k, v in providers)
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AI Copilot</title>
<style>
body{{margin:0;background:#f5f7fb;color:#111827;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:24px}}.hero,.card{{background:#fff;border:1px solid #e5e7eb;border-radius:24px;box-shadow:0 14px 34px rgba(15,23,42,.06);padding:22px;margin:14px 0}}
h1{{margin:0;font-size:32px;letter-spacing:-.04em}}h2{{margin:0 0 12px}}p{{color:#64748b;line-height:1.55}}a{{color:#2563eb;font-weight:800;text-decoration:none}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}label span{{font-weight:900;color:#334155;display:block;margin-top:8px}}
input,select,textarea{{width:100%;border:1px solid #d1d5db;border-radius:14px;padding:12px;margin-top:6px;font-size:14px;background:#f9fafb}}textarea{{min-height:120px}}
button{{border:0;border-radius:14px;background:#2563eb;color:#fff;font-weight:900;padding:13px 16px;cursor:pointer;width:100%;margin-top:12px}}
.check{{display:block;border:1px solid #e5e7eb;background:#f8fafc;border-radius:14px;padding:12px;margin:8px 0;color:#334155}}.check input{{width:auto;margin-right:8px}}
.warn{{background:#fffbeb;border-left:4px solid #f59e0b;border-radius:12px;padding:12px;color:#92400e}}.ok{{background:#ecfdf5;border-left:4px solid #10b981;border-radius:12px;padding:12px;color:#065f46}}
.pill{{display:inline-block;border-radius:999px;background:#eff6ff;color:#2563eb;padding:6px 10px;font-weight:800;margin:3px}}@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><main class='wrap'>
<section class='hero'><h1>AI Copilot</h1><p>GPT, OpenAI-compatible API, OpenRouter/Groq/DeepSeek 계열, Ollama 로컬 모델까지 바꿔 끼우는 승인형 AI 보조 시스템입니다. AI는 분석/진단/개선/패치를 제안하고, 실제 코드 적용은 사용자가 승인해야만 수행됩니다.</p><p><a href='/'>홈</a> · <a href='/terminal'>터미널</a> · <a href='/backtest-explorer'>백테스트 검증</a></p><div><span class='pill'>현재 Provider: {html.escape(provider)}</span><span class='pill'>모델: {html.escape(model or '-')}</span><span class='pill'>{html.escape(has_key)}</span></div></section>
<section class='grid'><div class='card'><h2>1. AI Provider 설정</h2><form method='post' action='/save-ai-settings'><label><span>AI Provider</span><select name='AI_PROVIDER'>{opts}</select></label><label><span>API Key</span><input name='AI_API_KEY' type='password' placeholder='sk-... 또는 provider key'><small>{html.escape(has_key)}</small></label><label><span>Model</span><input name='AI_MODEL' value='{html.escape(model)}' placeholder='예: gpt-4.1-mini, deepseek-chat, llama3.1'></label><label><span>Base URL</span><input name='AI_BASE_URL' value='{html.escape(base_url)}' placeholder='OpenAI는 비워도 됨. 호환 API는 https://.../v1'></label><label><span>Temperature</span><input name='AI_TEMPERATURE' value='{html.escape(temp)}' placeholder='0.1~0.4 권장'></label><button>AI 설정 저장</button></form></div><div class='card'><h2>2. 최근 결과/패치</h2><p class='ok'>{result_link}</p><p class='warn'>{patch_link}</p><p>패치 제안은 <b>코드 미리보기 → 승인 체크 → 자동 백업 → 적용</b> 순서로만 진행됩니다. .env, data, reports/output, backups 폴더는 수정 대상에서 차단됩니다.</p></div></section>
<section class='card'><h2>3. AI 작업 실행</h2><form method='post' action='/ai-copilot-run'><div class='grid'><div><label class='check'><input type='checkbox' name='ai_tasks' value='stock_analysis' checked> 종목 분석 AI: 선택 종목의 신호, 차트 위치, 진입/손절/목표, 재무·수급 리스크 설명</label><label class='check'><input type='checkbox' name='ai_tasks' value='backtest_diagnosis' checked> 백테스트 진단 AI: 벤치마크, MDD, 초과수익, 거래 수, 과최적화 위험 진단</label><label class='check'><input type='checkbox' name='ai_tasks' value='strategy_improvement'> 전략 개선 AI: 원형 유지/장기 스윙/공격형/10x 프로필 개선안 제시</label><label class='check'><input type='checkbox' name='ai_tasks' value='patch_proposal'> 코드 패치 제안 AI: 변경 내용을 JSON 패치로 제안. 승인 전 실제 적용 안 함</label></div><div><label><span>분석할 종목코드</span><input name='stock_code' placeholder='예: 005930'></label><label><span>추가 요청</span><textarea name='user_prompt' placeholder='예: VCP가 왜 추격매수처럼 보이는지 진단하고, 원형을 깨지 않는 개선안을 제안해줘'></textarea></label><p class='warn'>패치 제안 기능은 AI가 코드 수정안을 만들 수 있지만, 바로 적용하지 않습니다. 반드시 미리보기와 승인 절차를 거칩니다.</p></div></div><button>선택한 AI 작업 실행</button></form></section>
</main></body></html>"""



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


def _interactive_terminal_page() -> str:
    """Toss 스타일 인터랙티브 종목 터미널."""
    return '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Stock Swing Terminal v14</title>
<style>
:root{--bg:#f5f7fb;--panel:#fff;--text:#111827;--muted:#6b7280;--line:#e5e7eb;--blue:#2563eb;--green:#16a34a;--red:#ef4444;--purple:#9333ea;--orange:#f59e0b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}a{color:var(--blue);font-weight:800;text-decoration:none}.app{max-width:1540px;margin:0 auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.brand{display:flex;align-items:center;gap:10px}.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,#3182f6,#0ea5e9);box-shadow:0 12px 30px rgba(37,99,235,.22)}h1{margin:0;font-size:25px;letter-spacing:-.04em}.sub{color:var(--muted);font-size:13px;margin-top:4px}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a{background:#fff;border:1px solid var(--line);border-radius:999px;padding:9px 12px;color:#334155;font-size:13px;box-shadow:0 5px 16px rgba(15,23,42,.05)}.layout{display:grid;grid-template-columns:420px 1fr;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:24px;box-shadow:0 12px 34px rgba(15,23,42,.06)}.left{height:calc(100vh - 104px);overflow:hidden;display:flex;flex-direction:column}.toolbar{padding:16px;border-bottom:1px solid var(--line)}.search{width:100%;border:1px solid var(--line);border-radius:16px;padding:13px 14px;font-size:15px;background:#f9fafb}.list{overflow:auto;padding:8px}.item{display:grid;grid-template-columns:1fr auto;gap:8px;padding:14px;border-radius:18px;cursor:pointer;border:1px solid transparent}.item:hover,.item.active{background:#eef5ff;border-color:#bfdbfe}.item b{font-size:15px}.code{color:var(--muted);font-size:12px;margin-top:3px}.tags{color:#475569;font-size:12px;margin-top:6px;line-height:1.35}.score{background:#111827;color:#fff;border-radius:999px;padding:6px 9px;font-size:12px;font-weight:900}.detail{padding:18px}.headline{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.title h2{margin:0;font-size:28px;letter-spacing:-.04em}.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:14px 0}.metric{background:#f9fafb;border:1px solid var(--line);border-radius:18px;padding:12px}.metric b{display:block;color:var(--muted);font-size:12px}.metric span{font-size:18px;font-weight:900}.chartbox{background:#fff;border:1px solid var(--line);border-radius:22px;padding:14px}.rangebar{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:10px}.rangebar button{border:1px solid var(--line);background:#f9fafb;color:#334155;border-radius:999px;padding:8px 12px;font-weight:800;cursor:pointer}.rangebar button.on{background:#2563eb;color:#fff;border-color:#2563eb}.mode{border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 10px;font-weight:900;cursor:pointer}.mode.on{background:#2563eb;color:#fff;border-color:#2563eb}.legend{display:flex;gap:10px;flex-wrap:wrap;color:#64748b;font-size:12px}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.table{width:100%;border-collapse:collapse}.table th,.table td{padding:10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}.table th{color:#64748b}.pill{display:inline-flex;border-radius:999px;background:#eff6ff;color:#2563eb;padding:5px 8px;font-size:12px;font-weight:800}.empty{padding:40px;text-align:center;color:var(--muted)}svg{width:100%;height:auto}.small{color:var(--muted);font-size:12px;line-height:1.55}.reason{line-height:1.45;color:#334155}@media(max-width:1050px){.layout{grid-template-columns:1fr}.left{height:460px}.metrics{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
</style></head><body><main class="app"><div class="top"><div class="brand"><div class="logo"></div><div><h1>Stock Swing Terminal</h1><div class="sub">체크 전략 후보 · 캔들차트 · 진입/손절/목표가 · 백테스트 매수/매도 화살표</div></div></div><div class="nav"><a href="/">홈</a><a href="/backtest-explorer">백테스트 검증</a><a href="/backtest-report">리포트</a><a href="/job-status">작업상태</a></div></div><div class="layout"><section class="panel left"><div class="toolbar"><input id="q" class="search" placeholder="종목명, 코드, 전략 검색"><div class="small" style="margin-top:8px">현재 후보는 설정된 최대 보유 종목 수 때문에 10~20개 정도만 표시됩니다. 백테스트가 실제로 매일 새 종목을 찾았는지는 아래 모드를 바꿔 확인하세요.</div><div class="modebar" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px"><button class="mode on" data-source="portfolio" onclick="setSource('portfolio')">현재 후보</button><button class="mode" data-source="signals" onclick="setSource('signals')">백테스트 신호</button><button class="mode" data-source="trades" onclick="setSource('trades')">실제 거래</button><button class="mode" data-source="ai_theme" onclick="setSource('ai_theme')">AI 테마</button></div><div id="modeHelp" class="small" style="margin-top:8px;color:#2563eb;font-weight:800"></div></div><div id="list" class="list"><div class="empty">로딩 중...</div></div></section><section class="panel detail"><div id="detail"><div class="empty">왼쪽에서 종목을 선택하세요.</div></div></section></div></main><script>
let rows=[], current=null, currentData=null, rangeYears=5, terminalSource='portfolio';
const money=v=>v==null||isNaN(v)?'-':Math.round(Number(v)).toLocaleString('ko-KR');
const pct=v=>v==null||isNaN(v)?'-':Number(v).toFixed(2)+'%';
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function setSource(src){terminalSource=src; document.querySelectorAll('.mode').forEach(b=>b.classList.toggle('on',b.dataset.source===src)); loadTerminal();}
function loadTerminal(){document.getElementById('list').innerHTML='<div class="empty">로딩 중...</div>'; fetch('/api/terminal?limit=800&source='+terminalSource).then(r=>r.json()).then(d=>{rows=d.rows||[]; current=null; renderList(); const help={portfolio:'현재 분석일 기준 최종 포트폴리오 후보입니다. max_positions와 중복통합 때문에 14개처럼 작게 보이는 것이 정상입니다.',signals:'백테스트 기간 전체에서 매일 새로 생성된 역사적 신호입니다. 기간을 바꾸고 백테스트를 다시 돌리면 이 목록이 바뀝니다.',trades:'백테스트에서 실제 체결된 종목입니다. 신호가 있어도 포지션/현금/쿨다운 조건 때문에 거래가 안 된 종목은 제외됩니다.',ai_theme:'AI 장기 테마 모드가 정리한 미래 기술 섹터 후보입니다. 가격·재무·수급 CSV 기반의 경량 분석입니다.'}; document.getElementById('modeHelp').innerText=help[terminalSource]||''; if(rows[0]) selectStock(rows[0].stock_code);});}
document.getElementById('q').addEventListener('input',renderList);
loadTerminal();
function renderList(){const q=document.getElementById('q').value.toLowerCase(); const list=document.getElementById('list'); const filtered=rows.filter(r=>[r.stock_code,r.stock_name,r.strategies,r.signals].join(' ').toLowerCase().includes(q)).slice(0,250); list.innerHTML=filtered.map(r=>`<div class="item ${current===r.stock_code?'active':''}" onclick="selectStock('${r.stock_code}')"><div><b>${esc(r.stock_name)}</b><div class="code">${r.stock_code}</div><div class="tags">${esc(r.strategies)}<br>${esc(r.signals)}</div></div><div class="score">${r.score==null?'-':Number(r.score).toFixed(0)}</div></div>`).join('')||'<div class="empty">검색 결과 없음</div>';}
function selectStock(code){current=code; renderList(); document.getElementById('detail').innerHTML='<div class="empty">차트 불러오는 중...</div>'; fetch('/api/stock?code='+code).then(r=>r.json()).then(d=>{currentData=d; renderDetail(d);});}
function setRange(y){rangeYears=y; renderDetail(currentData);}
function renderDetail(d){
 if(!d||!d.ok){document.getElementById('detail').innerHTML='<div class="empty">'+esc(d?.error||'데이터 없음')+'</div>';return;}
 const m=d.metrics||{};
 const cards=[['현재가',money(m.latest_close)],['거래대금',money(m.trading_value)],['20일 수익률',pct(m.ret20)],['60일 수익률',pct(m.ret60)],['52주 고점 대비',pct(m.from_52w_high_pct)],['피벗 대비',pct(m.pivot_gap_pct)],['손익비',m.rr==null?'-':Number(m.rr).toFixed(2)]];
 const sigRows=(d.signals||[]).map(s=>`<tr><td>${esc(s.strategy)}</td><td><span class="pill">${esc(s.signal)}</span></td><td>${s.score==null?'-':Number(s.score).toFixed(1)}</td><td>${money(s.entry_price)}</td><td>${money(s.stop_loss)}</td><td>${money(s.target_price)}</td><td>${pct(s.pivot_gap_pct)}</td><td>${pct(s.ma20_gap_pct)}</td><td>${pct(s.stop_distance_pct)}</td><td class="reason">${esc((s.entry_filter_reason||s.reason||'')).slice(0,280)}</td></tr>`).join('')||'<tr><td colspan="10">현재 후보 신호 없음</td></tr>';
 const tradeRows=(d.trades||[]).slice(-12).reverse().map(t=>`<tr><td>${esc(t.strategy)}</td><td>${esc(t.entry_type||'')}</td><td>${esc(t.entry_date)}</td><td>${money(t.entry_price)}</td><td>${pct(t.entry_pivot_gap_pct)}</td><td>${esc(t.exit_date)}</td><td>${money(t.exit_price)}</td><td>${pct(t.return_pct)}</td><td>${esc(t.exit_reason)}</td></tr>`).join('')||'<tr><td colspan="9">이 종목은 현재 후보일 수 있지만, 선택한 백테스트 기간에는 실제 진입 기록이 없습니다.</td></tr>';
 document.getElementById('detail').innerHTML=`<div class="headline"><div class="title"><h2>${esc(d.stock_name)} <span class="small">${d.stock_code}</span></h2><div class="small">현재 후보 신호(◆)와 백테스트 실제 매수/청산(▲/▼)을 분리해 표시합니다.</div></div><a href="/stock?code=${d.stock_code}">상세 페이지</a></div><div class="metrics">${cards.map(c=>`<div class="metric"><b>${c[0]}</b><span>${c[1]}</span></div>`).join('')}</div><div class="chartbox"><div class="rangebar"><div><button onclick="setRange(1)" class="${rangeYears===1?'on':''}">1년</button><button onclick="setRange(2)" class="${rangeYears===2?'on':''}">2년</button><button onclick="setRange(3)" class="${rangeYears===3?'on':''}">3년</button><button onclick="setRange(5)" class="${rangeYears===5?'on':''}">5년</button></div><div class="legend"><span><i class="dot" style="background:#16a34a"></i>진입가</span><span><i class="dot" style="background:#ef4444"></i>손절가</span><span><i class="dot" style="background:#9333ea"></i>목표가</span><span><i class="dot" style="background:#f59e0b"></i>피벗</span><span>◆ 현재 후보 · ▲ 백테스트 매수 · ▼ 백테스트 청산 · 구름=Ichimoku</span></div></div><div id="chart"></div></div><div class="grid2"><section><h3>전략 신호와 근거</h3><table class="table"><thead><tr><th>전략</th><th>신호</th><th>점수</th><th>진입</th><th>손절</th><th>목표</th><th>피벗 이격</th><th>20일선 이격</th><th>손절폭</th><th>근거/제외</th></tr></thead><tbody>${sigRows}</tbody></table></section><section><h3>해당 종목 백테스트 거래</h3><table class="table"><thead><tr><th>전략</th><th>유형</th><th>매수일</th><th>매수가</th><th>피벗이격</th><th>청산일</th><th>청산가</th><th>수익률</th><th>이유</th></tr></thead><tbody>${tradeRows}</tbody></table></section></div>`; drawChart(d, rangeYears);
}
function drawChart(d, years){let candles=(d.candles||[]).filter(c=>c.open&&c.high&&c.low&&c.close); if(!candles.length){document.getElementById('chart').innerHTML='<div class="empty">차트 없음</div>';return;} const maxDate=new Date(candles[candles.length-1].date); const cutoff=new Date(maxDate); cutoff.setFullYear(cutoff.getFullYear()-years); candles=candles.filter(c=>new Date(c.date)>=cutoff); const trades=(d.trades||[]); const W=1060,H=600,L=70,R=24,T=26,B=105,CW=W-L-R,CH=H-T-B; const levelVals=Object.values(d.levels||{}).filter(v=>v&&isFinite(v)); const cloudVals=[]; candles.forEach(c=>{if(c.cloud_a)cloudVals.push(c.cloud_a); if(c.cloud_b)cloudVals.push(c.cloud_b); if(c.ma20)cloudVals.push(c.ma20); if(c.ma60)cloudVals.push(c.ma60); if(c.ma200)cloudVals.push(c.ma200);}); let ymin=Math.min(...candles.map(c=>c.low),...levelVals,...cloudVals), ymax=Math.max(...candles.map(c=>c.high),...levelVals,...cloudVals); const pad=(ymax-ymin)*.07||1; ymin-=pad; ymax+=pad; const x=i=>L+(candles.length===1?CW/2:i/(candles.length-1)*CW); const y=v=>T+(ymax-v)/(ymax-ymin)*CH; const cw=Math.max(2,Math.min(9,CW/candles.length*.62)); let svg=`<svg viewBox="0 0 ${W} ${H}"><rect x="0" y="0" width="${W}" height="${H}" rx="20" fill="#ffffff"/><line x1="${L}" y1="${T+CH}" x2="${L+CW}" y2="${T+CH}" stroke="#e5e7eb"/><line x1="${L}" y1="${T}" x2="${L}" y2="${T+CH}" stroke="#e5e7eb"/>`;
 const validCloud=candles.map((c,i)=>({i,a:c.cloud_a,b:c.cloud_b})).filter(p=>p.a&&p.b&&isFinite(p.a)&&isFinite(p.b)); if(validCloud.length>2){const upper=validCloud.map(p=>`${x(p.i)},${y(Math.max(p.a,p.b))}`).join(' '); const lower=validCloud.slice().reverse().map(p=>`${x(p.i)},${y(Math.min(p.a,p.b))}`).join(' '); svg+=`<polygon points="${upper} ${lower}" fill="#93c5fd" opacity=".18"/><polyline points="${validCloud.map(p=>`${x(p.i)},${y(p.a)}`).join(' ')}" fill="none" stroke="#60a5fa" stroke-width="1.2" opacity=".75"/><polyline points="${validCloud.map(p=>`${x(p.i)},${y(p.b)}`).join(' ')}" fill="none" stroke="#38bdf8" stroke-width="1.2" opacity=".75"/>`;}
 function maLine(key,col){const pts=candles.map((c,i)=>c[key]&&isFinite(c[key])?`${x(i)},${y(c[key])}`:null).filter(Boolean); if(pts.length>1)svg+=`<polyline points="${pts.join(' ')}" fill="none" stroke="${col}" stroke-width="1.35" opacity=".86"/>`;} maLine('ma20','#facc15'); maLine('ma60','#fb923c'); maLine('ma200','#a78bfa');
 candles.forEach((c,i)=>{const col=c.close>=c.open?'#ef4444':'#2563eb'; svg+=`<line x1="${x(i)}" y1="${y(c.high)}" x2="${x(i)}" y2="${y(c.low)}" stroke="${col}" stroke-width="1.1"/>`; const yy=Math.min(y(c.open),y(c.close)), hh=Math.max(1.4,Math.abs(y(c.open)-y(c.close))); svg+=`<rect x="${x(i)-cw/2}" y="${yy}" width="${cw}" height="${hh}" rx="1.5" fill="${col}" opacity=".82"/>`;});
 function line(val,col,label){if(!val||!isFinite(val))return; const yy=y(val); svg+=`<line x1="${L}" y1="${yy}" x2="${L+CW}" y2="${yy}" stroke="${col}" stroke-dasharray="8 5" stroke-width="2"/><text x="${L+CW-170}" y="${yy-6}" fill="${col}" font-size="12" font-weight="800">${label} ${money(val)}</text>`;} line(d.levels.entry,'#16a34a','진입'); line(d.levels.stop,'#ef4444','손절'); line(d.levels.target,'#9333ea','목표'); line(d.levels.pivot,'#f59e0b','피벗');
 const lastIdx=candles.length-1; if(d.levels.entry){svg+=`<polygon points="${x(lastIdx)},${y(d.levels.entry)-10} ${x(lastIdx)-8},${y(d.levels.entry)} ${x(lastIdx)},${y(d.levels.entry)+10} ${x(lastIdx)+8},${y(d.levels.entry)}" fill="#f59e0b" opacity=".95"/><text x="${x(lastIdx)-80}" y="${y(d.levels.entry)-14}" fill="#f59e0b" font-size="11" font-weight="900">CURRENT SIGNAL</text>`;}
 const dateToIndex=new Map(candles.map((c,i)=>[c.date,i])); trades.forEach(t=>{let ei=dateToIndex.get(t.entry_date); if(ei!=null&&t.entry_price){const lab=t.entry_pivot_gap_pct==null?'':` ${Number(t.entry_pivot_gap_pct).toFixed(1)}%`;svg+=`<polygon points="${x(ei)},${y(t.entry_price)-13} ${x(ei)-7},${y(t.entry_price)-1} ${x(ei)+7},${y(t.entry_price)-1}" fill="#16a34a"/><text x="${x(ei)+7}" y="${y(t.entry_price)-9}" font-size="11" fill="#16a34a" font-weight="800">BUY${lab}</text>`;} let xi=dateToIndex.get(t.exit_date); if(xi!=null&&t.exit_price){svg+=`<polygon points="${x(xi)},${y(t.exit_price)+13} ${x(xi)-7},${y(t.exit_price)+1} ${x(xi)+7},${y(t.exit_price)+1}" fill="#ef4444"/><text x="${x(xi)+7}" y="${y(t.exit_price)+16}" font-size="11" fill="#ef4444" font-weight="800">SELL</text>`;}});
 svg+=`<text x="12" y="${T+10}" fill="#64748b" font-size="12">${money(ymax)}</text><text x="12" y="${T+CH}" fill="#64748b" font-size="12">${money(ymin)}</text><text x="${L}" y="${H-22}" fill="#64748b" font-size="12">${candles[0].date}</text><text x="${L+CW-90}" y="${H-22}" fill="#64748b" font-size="12">${candles[candles.length-1].date}</text><text x="${L+250}" y="22" fill="#64748b" font-size="12">MA20/60/200 + Ichimoku Cloud</text></svg>`; document.getElementById('chart').innerHTML=svg;
}
</script></body></html>'''

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
        ("벤치마크", OUTPUT_DIR / "backtest_excess_summary.csv"),
        ("알고리즘 감사", OUTPUT_DIR / "algorithm_audit.csv"),
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
    keys = [("총수익률", "total_return_pct"), ("총투입수익률", "return_on_total_contributed_pct"), ("추가투입금", "external_contributions"), ("CAGR", "cagr_pct"), ("MDD", "mdd_pct"), ("Sharpe", "sharpe"), ("승률", "win_rate_pct"), ("PF", "profit_factor"), ("거래수", "trades")]
    cards = []
    for label, k in keys:
        v = row.get(k, "-")
        if k == "return_on_total_contributed_pct" and (pd.isna(v) if hasattr(pd, "isna") else False):
            continue
        if "pct" in k or k.endswith("rate_pct"):
            val = _fmt_pct_num(v)
        elif k in {"external_contributions"}:
            try:
                val = f"{float(v):,.0f}원"
            except Exception:
                val = html.escape(str(v))
        else:
            try:
                val = f"{int(float(v)):,}" if k == "trades" else f"{float(v):.2f}"
            except Exception:
                val = html.escape(str(v))
        cards.append(f"<div class='mini'><b>{label}</b><span>{val}</span></div>")
    bench = _load_csv_safe(OUTPUT_DIR / "backtest_excess_summary.csv")
    btxt = ""
    if not bench.empty:
        try:
            br = bench.iloc[0]
            btxt = f"<p class='muted'>대표 벤치마크 대비 초과수익: {float(br.get('excess_total_return_pct', 0)):.2f}% · MDD 우위: {float(br.get('mdd_advantage_pct', 0)):.2f}%</p>"
        except Exception:
            pass
    return "".join(cards) + "<p class='muted'>MDD는 고점 대비 최대 낙폭, PF는 총이익/총손실입니다. 실전형은 수익률뿐 아니라 MDD·승률·PF·벤치마크 초과수익을 같이 봐야 합니다.</p>" + btxt



def _apply_backtest_profile(base: dict, profile: str) -> dict:
    """전략 원형을 훼손하지 않도록 청산/자금관리 프로필을 잠금식으로 적용한다.

    숫자 손절비/손익비를 사용자가 마음대로 섞으면 VCP/CANSLIM/Stage2/Darvas의 원형이 깨질 수 있다.
    그래서 웹 UI는 4개 프로필만 제공한다.
    """
    profile = str(profile or "original_locked").strip().lower()
    base["profile_name"] = profile
    base["capital_injection_overlay"] = {**(base.get("capital_injection_overlay", {}) or {}), "enabled": False}
    if profile in {"original_locked", "original", "pure_original", "balanced"}:
        base["exit_profile_mode"] = "1. 원형 유지"
        base["exit_target_mode"] = "signal"
        base["stop_loss_mode"] = "signal"
        base["target_exit_enabled"] = True
        # 원형 모드에서는 전략 자체의 고유 stop/target/MA 청산을 우선한다. 리스크 오버레이는 기본 OFF.
        ro = base.get("risk_overlay", {}) or {}
        ro.update({
            "enabled": False,
            "method_note": "원형 유지: 전략별 고유 손절/청산 기준 사용. 사용자가 임의 손익비를 덮어쓰지 않음.",
            "trailing_stop_enabled": False,
            "risk_off_defensive_exit": False,
            "max_total_open_positions": 20,
        })
        base["risk_overlay"] = ro
        base["strategy_exit"] = {
            "vcp": {"stop_pct": 0.07, "target_pct": 0.18, "max_holding_days": 45},
            "canslim": {"stop_pct": 0.08, "target_pct": 0.25, "max_holding_days": 65},
            "stage2": {"stop_pct": 0.10, "target_pct": 0.35, "max_holding_days": 130},
            "darvas": {"stop_pct": 0.08, "target_pct": 0.18, "max_holding_days": 35},
            "deep_turnaround": {"stop_pct": 0.25, "target_price_multiple": 5.0, "target_pct": 4.0, "max_holding_days": 756},
        }
        return base

    if profile in {"long_swing_locked", "long_swing", "long_swing_aggressive", "long"}:
        base["exit_profile_mode"] = "2. 장기 스윙"
        base["exit_target_mode"] = "max_signal_and_config"
        base["stop_loss_mode"] = "wider_of_signal_and_config"
        # 작은 익절 금지: 목표가는 차트 기준선으로만 쓰고, 실제 청산은 추세/트레일링/최대보유 중심.
        base["target_exit_enabled"] = False
        base["test_years"] = max(int(base.get("test_years", 3) or 3), 5)
        base["strategy_exit"] = {
            "vcp": {"stop_pct": 0.10, "target_price_multiple": 2.0, "target_pct": 1.0, "max_holding_days": 252},
            "canslim": {"stop_pct": 0.10, "target_price_multiple": 2.2, "target_pct": 1.2, "max_holding_days": 315},
            "stage2": {"stop_pct": 0.16, "target_price_multiple": 3.0, "target_pct": 2.0, "max_holding_days": 504},
            "darvas": {"stop_pct": 0.10, "target_price_multiple": 1.8, "target_pct": 0.8, "max_holding_days": 220},
            "deep_turnaround": {"stop_pct": 0.25, "target_price_multiple": 5.0, "target_pct": 4.0, "max_holding_days": 756},
        }
        base["max_positions"] = {"vcp": 4, "canslim": 5, "stage2": 6, "darvas": 3, "deep_turnaround": 4}
        base["signal_top_n_per_day"] = {"vcp": 6, "canslim": 6, "stage2": 8, "darvas": 4, "deep_turnaround": 4}
        ro = base.get("risk_overlay", {}) or {}
        ro.update({
            "enabled": True,
            "method_note": "장기 스윙: 수익 종목은 오래 보유. 작은 익절 금지. 20/50/30주선 및 ATR 트레일링으로 추적.",
            "market_exposure_multiplier": {"risk_on": 1.0, "neutral": 0.9, "risk_off": 0.65},
            "block_new_entries_in_risk_off": False,
            "use_risk_position_sizing": True,
            "risk_per_trade_pct_of_strategy_equity": {"vcp": 0.022, "canslim": 0.022, "stage2": 0.020, "darvas": 0.018, "deep_turnaround": 0.010},
            "cooldown_after_stop_days": 5,
            "breakeven_trigger_pct": {"vcp": 0.25, "canslim": 0.30, "stage2": 0.45, "darvas": 0.22, "deep_turnaround": 1.0},
            "trailing_stop_enabled": True,
            "trailing_activation_pct": {"vcp": 0.45, "canslim": 0.50, "stage2": 0.75, "darvas": 0.40, "deep_turnaround": 1.50},
            "atr_trailing_multiple": {"vcp": 5.0, "canslim": 5.5, "stage2": 6.0, "darvas": 4.5, "deep_turnaround": 8.0},
            "max_total_open_positions": 12,
            "risk_off_defensive_exit": False,
        })
        base["risk_overlay"] = ro
        return base

    if profile in {"aggressive_locked", "aggressive", "aggressive_bounds"}:
        base["exit_profile_mode"] = "3. 공격형"
        base["exit_target_mode"] = "config_multiple"
        base["stop_loss_mode"] = "wider_of_signal_and_config"
        base["target_exit_enabled"] = False
        base["test_years"] = max(int(base.get("test_years", 3) or 3), 5)
        # 손절은 전략별 허용 범위 안에서만 확장한다. VCP/CANSLIM은 과도하게 넓히지 않음.
        base["weights"] = {"vcp": 0.45, "canslim": 0.30, "stage2": 0.10, "darvas": 0.10, "deep_turnaround": 0.05}
        base["max_positions"] = {"vcp": 3, "canslim": 3, "stage2": 2, "darvas": 2, "deep_turnaround": 2}
        base["signal_top_n_per_day"] = {"vcp": 6, "canslim": 6, "stage2": 4, "darvas": 4, "deep_turnaround": 3}
        base["strategy_exit"] = {
            "vcp": {"stop_pct": 0.12, "target_price_multiple": 3.0, "target_pct": 2.0, "max_holding_days": 380},
            "canslim": {"stop_pct": 0.12, "target_price_multiple": 3.0, "target_pct": 2.0, "max_holding_days": 420},
            "stage2": {"stop_pct": 0.18, "target_price_multiple": 3.5, "target_pct": 2.5, "max_holding_days": 600},
            "darvas": {"stop_pct": 0.12, "target_price_multiple": 2.5, "target_pct": 1.5, "max_holding_days": 260},
            "deep_turnaround": {"stop_pct": 0.30, "target_price_multiple": 6.0, "target_pct": 5.0, "max_holding_days": 1008},
        }
        ro = base.get("risk_overlay", {}) or {}
        ro.update({
            "enabled": True,
            "method_note": "공격형: 목표가 크게, 집중도 증가, 손절은 전략별 허용범위 내에서만 확장",
            "market_exposure_multiplier": {"risk_on": 1.15, "neutral": 0.95, "risk_off": 0.55},
            "block_new_entries_in_risk_off": False,
            "allow_exception_score_in_risk_off": 84,
            "use_risk_position_sizing": True,
            "risk_per_trade_pct_of_strategy_equity": {"vcp": 0.035, "canslim": 0.030, "stage2": 0.024, "darvas": 0.025, "deep_turnaround": 0.012},
            "cooldown_after_stop_days": 3,
            "breakeven_trigger_pct": {"vcp": 0.80, "canslim": 0.90, "stage2": 1.20, "darvas": 0.70, "deep_turnaround": 2.0},
            "trailing_stop_enabled": True,
            "trailing_activation_pct": {"vcp": 1.20, "canslim": 1.20, "stage2": 1.80, "darvas": 1.00, "deep_turnaround": 3.0},
            "atr_trailing_multiple": {"vcp": 8.0, "canslim": 8.0, "stage2": 10.0, "darvas": 7.0, "deep_turnaround": 12.0},
            "max_total_open_positions": 8,
            "risk_off_defensive_exit": False,
        })
        base["risk_overlay"] = ro
        return base

    if profile in {"tenx_experiment_locked", "10x", "tenx", "capital_injection_10x", "ultra10x_concentrated"}:
        base["exit_profile_mode"] = "4. 10x 실험"
        base["enabled_strategies"] = [sid for sid in base.get("enabled_strategies", []) if sid in {"vcp", "canslim", "deep_turnaround"}] or ["vcp", "canslim", "deep_turnaround"]
        base["exit_target_mode"] = "config_multiple"
        base["stop_loss_mode"] = "wider_of_signal_and_config"
        base["target_exit_enabled"] = False
        base["test_years"] = max(int(base.get("test_years", 3) or 3), 5)
        base["weights"] = {"vcp": 0.55, "canslim": 0.25, "deep_turnaround": 0.20}
        base["max_positions"] = {"vcp": 3, "canslim": 2, "deep_turnaround": 3}
        base["signal_top_n_per_day"] = {"vcp": 5, "canslim": 4, "deep_turnaround": 4}
        base["strategy_exit"] = {
            "vcp": {"stop_pct": 0.18, "target_price_multiple": 5.0, "target_pct": 4.0, "max_holding_days": 1008},
            "canslim": {"stop_pct": 0.18, "target_price_multiple": 5.0, "target_pct": 4.0, "max_holding_days": 1008},
            "deep_turnaround": {"stop_pct": 0.38, "target_price_multiple": 10.0, "target_pct": 9.0, "max_holding_days": 1260},
        }
        ro = base.get("risk_overlay", {}) or {}
        ro.update({
            "enabled": True,
            "method_note": "10x 실험: 별도 경고. 추가투입은 건전성/거짓저점 필터 통과 종목에만 허용",
            "market_exposure_multiplier": {"risk_on": 1.20, "neutral": 1.00, "risk_off": 0.65},
            "block_new_entries_in_risk_off": False,
            "allow_exception_score_in_risk_off": 82,
            "use_risk_position_sizing": True,
            "risk_per_trade_pct_of_strategy_equity": {"vcp": 0.038, "canslim": 0.030, "deep_turnaround": 0.012},
            "cooldown_after_stop_days": 0,
            "breakeven_trigger_pct": {"vcp": 1.50, "canslim": 1.50, "deep_turnaround": 2.50},
            "trailing_stop_enabled": True,
            "trailing_activation_pct": {"vcp": 2.0, "canslim": 2.0, "deep_turnaround": 4.0},
            "atr_trailing_multiple": {"vcp": 10.0, "canslim": 10.0, "deep_turnaround": 15.0},
            "max_total_open_positions": 7,
            "risk_off_defensive_exit": False,
        })
        base["risk_overlay"] = ro
        base["capital_injection_overlay"] = {
            "enabled": True,
            "allowed_strategies": ["vcp", "canslim", "deep_turnaround"],
            "drawdown_triggers_pct": [-0.20, -0.38, -0.55],
            "add_fraction_of_initial_position": [0.75, 1.00, 1.25],
            "max_additions_per_position": 3,
            "max_external_capital_pct_of_initial_cash": 3.0,
            "max_position_value_pct_of_total_contributed": 0.45,
            "min_health_score": 62,
            "require_not_falling_knife": True,
            "catastrophic_exit_enabled": True,
            "catastrophic_drawdown_pct": -0.82,
        }
        return base

    return base

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
    profile = str(form.get("backtest_profile", ["balanced"])[0] or "balanced")
    base = _apply_backtest_profile(base, profile)
    base["initial_cash"] = ival("initial_cash", base.get("initial_cash", 10000000))
    # 장기 스윙 프로필은 최소 5년 평가를 권장한다.
    base["test_years"] = ival("test_years", base.get("test_years", 3))
    start_date = str(form.get("start_date", [""])[0]).strip()
    end_date = str(form.get("end_date", [""])[0]).strip()
    base["start_date"] = start_date
    base["end_date"] = end_date
    if profile in {"long_swing_aggressive", "aggressive_long_swing", "long", "capital_injection_10x", "add_cash_10x", "compound_10x"}:
        base["test_years"] = max(int(base["test_years"]), 5)
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
        <div class='field'><label>백테스트 프로필</label><select name='backtest_profile'>
          <option value='original_locked'>1. 원형 유지: 각 전략의 고유 손절/청산 기준 사용</option>
          <option value='long_swing_locked'>2. 장기 스윙: 작은 익절 금지, 20/50/30주선·ATR 추적</option>
          <option value='aggressive_locked'>3. 공격형: 목표가 크게, 집중도 증가, 손절은 전략별 허용범위 내</option>
          <option value='tenx_experiment_locked'>4. 10x 실험: 추가투입, 건전성/거짓저점 필터 필수</option>
        </select><small>손절비/손익비를 직접 바꾸지 않고, 알고리즘 원형을 보호하는 청산 프로필만 선택합니다. 10x 실험은 고위험 모드라 총투입원금 기준 수익률을 반드시 봐야 합니다.</small></div>
        <div class='subgrid'>
        {_num_input('initial_cash','초기자금','10000000','백테스트 시작 계좌 금액. 예: 10000000 = 천만원')}
        {_num_input('test_years','평가기간(년)','5','최근 몇 년을 실제 평가구간으로 볼지. 아래 시작/종료일이 비어 있을 때만 사용')}
        <label class='field'><span>백테스트 시작일</span><input type='date' name='start_date'><em>예: 2021-01-01. 비우면 최근 N년으로 자동 계산합니다.</em></label>
        <label class='field'><span>백테스트 종료일</span><input type='date' name='end_date'><em>예: 2026-05-29. 비우면 CSV의 마지막 거래일까지 테스트합니다.</em></label>
        {_num_input('max_backtest_symbols','백테스트 종목 수','300','거래대금 상위 몇 개 종목만 테스트할지. 0이면 전체')}
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
<title>Stock Swing Terminal v14</title>
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
<section class='hero'><div class='box'><h1>Stock Swing Terminal v14</h1><p>웹에서 데이터 수집, 4전략 체크 분석, 백테스트 설정, 종목 선택형 인터랙티브 캔들 차트와 진입/손절/목표가 확인까지 한 번에 통제합니다.</p><div class='nav'><a href='/terminal'>인터랙티브 터미널</a><a href='/backtest-explorer'>백테스트 검증</a><a href='/backtest-report'>백테스트 리포트</a><a href='/multi-strategy'>포트폴리오</a><a href='/job-status'>작업 상태</a></div></div><div class='box'><h2>데이터 상태</h2><div class='statusgrid'>{_data_status_html()}</div></div></section>
<section class='grid'><div class='card wide'><h2>백테스트 요약</h2><div class='minirow'>{_backtest_summary_html()}</div></div><div class='card'><h2>추천 실행 순서</h2><p class='help'>1) 가격 CSV 확인 → 2) 주봉/파생 생성 → 3) 전략 체크 분석 → 4) 같은 체크 조합으로 백테스트 → 5) 터미널에서 종목 클릭 후 캔들 차트 확인.</p></div>
<div class='card'><h2>API 키 저장</h2><p class='muted'>DART/KIS를 나중에 실시간·재무 수집으로 전환할 때 씁니다. 빈칸은 그대로 둬도 됩니다.</p><form method='post' action='/save-env'><label class='field'><span>DART API KEY</span><input name='DART_API_KEY' value='{env_vals.get('DART_API_KEY','')}'><em>OpenDART 개인 인증키. 재무 CSV 수집에 필요</em></label><label class='field'><span>KIS APP KEY</span><input name='KIS_APP_KEY' value='{env_vals.get('KIS_APP_KEY','')}'><em>한국투자증권 실시간/주문 API용</em></label><label class='field'><span>KIS APP SECRET</span><input name='KIS_APP_SECRET' value='{env_vals.get('KIS_APP_SECRET','')}'><em>한국투자증권 앱 시크릿</em></label><button>API 키 저장</button></form></div>
<div class='card wide'><h2>가격 CSV 수집: yfinance</h2><p class='muted'>현재는 yfinance 기반 과거 가격 수집. 나중에는 이 영역을 KIS/키움 실시간 API로 교체할 수 있게 분리해뒀습니다.</p><form method='post' action='/download-yfinance-web'><div class='subgrid'><label class='field'><span>수집 기간</span><select name='period'><option value='1y'>1년</option><option value='3y'>3년</option><option value='5y' selected>5년</option><option value='10y'>10년</option></select><em>스윙 백테스트는 보통 3~5년 권장</em></label><label class='field'><span>테스트 종목 수 제한</span><input name='limit' value=''><em>비우면 전체. 테스트는 20 또는 100 입력</em></label><label class='field'><span>배치 크기</span><input name='batch_size' value='50'><em>한 번에 요청할 종목 수. 실패가 많으면 20~30</em></label><label class='field'><span>요청 간격(초)</span><input name='sleep_sec' value='0.7'><em>기숙사/와이파이가 불안정하면 1.0~1.5</em></label></div><label class='toggle'><input type='checkbox' name='rebuild_universe'> KRX 종목목록을 새로 만들기</label><button>yfinance 가격 CSV 수집 시작</button></form></div>
<div class='card'><h2>파생 데이터</h2><p class='muted'>주봉 30주선, 섹터 강도 등 전략 계산용 파일을 만듭니다.</p><form method='post' action='/build-derived'><button class='secondary'>주봉/파생 데이터 생성</button></form></div>
<div class='card wide'><h2>4개 알고리즘 체크 분석</h2><p class='muted'>체크한 알고리즘만 후보를 만들고, 선택 전략의 비중을 자동으로 100%로 재분배합니다.</p>{_strategy_control_html('/analyze-selected','선택 전략 분석 실행')}</div>
<div class='card full'><h2>선택 전략 백테스트 설정</h2><p class='help'>이 입력값들은 “백테스트 가정”입니다. 수수료/세금/슬리피지를 넣어야 실제에 가까워집니다. 숫자 단위는 아래 설명을 보고 넣으세요.</p>{_strategy_control_html('/backtest-selected-web','선택 전략 백테스트 실행', True)}</div>
<div class='card'><h2>DART 재무 CSV</h2><form method='post' action='/download-dart'>{_num_input('years','수집 연수','5','최근 몇 년 분기보고서를 받을지')}{_num_input('parts','총 분할 수','3','하루 제한 때문에 3 또는 4 권장')}{_num_input('part','이번 분할 번호','1','1일차면 1, 2일차면 2')}{_num_input('sleep_sec','요청 간격 초','0.15','제한/실패가 많으면 0.3~0.5')}<button>DART 분할 수집</button></form></div>
<div class='card'><h2>기관/외국인 수급</h2><form method='post' action='/download-supply-part'>{_num_input('days','거래일 수','1250','5년치면 대략 1250')}{_num_input('parts','총 분할 수','3','종목을 몇 묶음으로 나눌지')}{_num_input('part','이번 분할 번호','1','이번에 받을 묶음 번호')}<button class='secondary'>수급 분할 수집</button></form></div>
<div class='card'><h2>초공격 10x 최적화</h2><p class='muted'>훈련/검증/테스트를 분리해서 후보 프로필을 탐색합니다. 테스트 구간은 선택 후 1회만 평가해 과최적화를 줄입니다.</p><form method='post' action='/optimize-ultra10x'><label class='field'><span>후보 수 제한</span><input name='max_candidates' value=''><em>비우면 전체 후보. 빠른 테스트는 3~4 입력</em></label><button>초공격 10x 최적화 실행</button></form><p><a href='/ultra10x-report'>최적화 결과 보기</a></p></div>

<div class='card'><h2>AI Copilot</h2><p class='muted'>GPT/OpenAI 호환 API/Ollama 등 원하는 AI를 연결해 종목 분석, 백테스트 진단, 전략 개선, 코드 패치 제안을 실행합니다. 실제 코드 수정은 승인 후에만 적용됩니다.</p><p><a href='/ai-copilot'>AI Copilot 열기</a></p></div>
<div class='card'><h2>AI 장기 테마 모드</h2><p class='muted'>수소, 전고체배터리, 피지컬AI, 우주, 드론, 반도체, 자동차반도체 등 미래 기술 섹터별로 최대 50개씩 정리합니다. 호재성 테마뿐 아니라 재무·수급·가격위험도 같이 표시합니다.</p><form method='post' action='/ai-theme-run'>{_num_input('max_per_theme','테마별 최대 종목 수','50','각 테마에서 최대 몇 개 종목을 보여줄지')}{_num_input('min_score','최소 AI 테마 점수','45','너무 낮은 후보를 제외하는 점수. 40~55 권장')}<button class='secondary'>AI 장기 테마 분석 실행</button></form><p><a href='/ai-theme-report'>AI 테마 리포트</a></p></div>
<div class='card'><h2>작업/파일</h2><p><a href='/job-status'>현재 백그라운드 작업 상태</a></p><p><a href='/backtest_trades.csv'>거래내역 CSV</a></p><p><a href='/multi_strategy_signals_all.csv'>전략 신호 CSV</a></p><p><a href='/backtest_market_regime.csv'>시장상태 CSV</a></p><p><a href='/backtest_capital_injections.csv'>추가투입 기록 CSV</a></p><p><a href='/docs/LONG_SWING_AGGRESSIVE_V8.md'>장기 스윙 v8 설명</a></p></div>
</section></main></body></html>"""



def _safe_float(v, default=None):
    try:
        x = float(v)
        if np.isfinite(x):
            return x
    except Exception:
        pass
    return default


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default



def _api_terminal_rows(limit: int = 250, source: str = "portfolio") -> list[dict]:
    """터미널 좌측 리스트 데이터.

    source="portfolio"  : 현재 분석일 기준 최종 포트폴리오 후보. max_positions/중복통합 때문에 10~20개 정도가 정상.
    source="signals"    : 백테스트 기간 전체에서 매일 새로 생성된 역사적 신호. 기간을 바꾸고 백테스트를 다시 돌리면 이 목록이 바뀜.
    source="trades"     : 백테스트에서 실제 체결된 종목. 선택 기간 내 실제 매매 이력 확인용.
    """
    source = (source or "portfolio").lower().strip()
    if source in {"signal", "historical", "history"}:
        source = "signals"
    if source in {"trade", "backtest"}:
        source = "trades"
    if source in {"ai", "theme", "ai_theme", "themes"}:
        source = "ai_theme"



    if source == "ai_theme":
        data = _load_csv_safe(OUTPUT_DIR / "ai_theme_candidates.csv", dtype={"stock_code": str})
        if data.empty:
            return []
        data["stock_code"] = data["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        data = data.sort_values(["ai_theme_score", "theme"], ascending=[False, True]).drop_duplicates("stock_code", keep="first")
        rows=[]
        for _, r in data.head(limit).iterrows():
            rows.append({
                "rank": len(rows)+1,
                "source": "ai_theme",
                "stock_code": str(r.get("stock_code", "")).zfill(6),
                "stock_name": str(r.get("stock_name", r.get("name", ""))),
                "strategies": str(r.get("theme", "AI 장기 테마")),
                "signals": f"AI 테마점수 {r.get('ai_theme_score','')} · {r.get('matched_keyword','')}",
                "score": _safe_float(r.get("ai_theme_score", None), None),
                "close": _safe_float(r.get("close", None), None),
                "entry_price": None,
                "stop_loss": None,
                "target_price": None,
                "pivot_price": None,
                "portfolio_weight_pct": None,
                "reason": f"{r.get('good_points','')} | {r.get('financial_brief','')} | 주의: {r.get('risk_points','')}",
            })
        return rows

    if source == "signals":
        data = _load_csv_safe(OUTPUT_DIR / "backtest_signals.csv", dtype={"stock_code": str})
        if data.empty:
            return []
        if "stock_code" in data:
            data["stock_code"] = data["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        if "date" in data:
            data["_dt"] = pd.to_datetime(data["date"], errors="coerce")
        else:
            data["_dt"] = pd.NaT
        score_col = "signal_score" if "signal_score" in data.columns else "strategy_score" if "strategy_score" in data.columns else None
        sort_cols = ["_dt"] + ([score_col] if score_col else [])
        data = data.sort_values(sort_cols, ascending=[False] + ([False] if score_col else []))
        # 터미널 목록은 종목당 최신/최고 신호 1개만 보여준다. 전체 신호는 backtest_signals.csv에서 확인.
        data = data.drop_duplicates("stock_code", keep="first")
        rows=[]
        for _, r in data.head(limit).iterrows():
            score = r.get("signal_score", r.get("strategy_score", ""))
            rows.append({
                "rank": len(rows)+1,
                "source": "backtest_signals",
                "stock_code": str(r.get("stock_code", "")).zfill(6),
                "stock_name": str(r.get("stock_name", r.get("name", ""))),
                "strategies": str(r.get("strategy_name", r.get("strategy_id", ""))),
                "signals": f"{str(r.get('entry_type', r.get('signal','')))} · {str(r.get('date',''))[:10]}",
                "score": _safe_float(score, None),
                "close": _safe_float(r.get("close", None), None),
                "entry_price": _safe_float(r.get("entry_price", None), None),
                "stop_loss": _safe_float(r.get("stop_loss", None), None),
                "target_price": _safe_float(r.get("target_price", None), None),
                "pivot_price": _safe_float(r.get("pivot_price", r.get("entry_price", None)), None),
                "portfolio_weight_pct": None,
                "reason": "백테스트 기간 중 발생한 역사적 신호입니다. 종목은 기간 내 매일 새로 탐색됩니다.",
            })
        return rows

    if source == "trades":
        data = _load_csv_safe(OUTPUT_DIR / "backtest_trades.csv", dtype={"stock_code": str})
        if data.empty:
            return []
        if "stock_code" in data:
            data["stock_code"] = data["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        if "entry_date" in data:
            data["_dt"] = pd.to_datetime(data["entry_date"], errors="coerce")
        else:
            data["_dt"] = pd.NaT
        if "return_pct" in data:
            data["return_pct"] = pd.to_numeric(data["return_pct"], errors="coerce")
        data = data.sort_values(["_dt", "return_pct"], ascending=[False, False])
        data = data.drop_duplicates("stock_code", keep="first")
        rows=[]
        for _, r in data.head(limit).iterrows():
            ret = _safe_float(r.get("return_pct", None), None)
            rows.append({
                "rank": len(rows)+1,
                "source": "backtest_trades",
                "stock_code": str(r.get("stock_code", "")).zfill(6),
                "stock_name": str(r.get("stock_name", r.get("name", ""))),
                "strategies": str(r.get("strategy_name", r.get("strategy_id", ""))),
                "signals": f"진입 {str(r.get('entry_date',''))[:10]} · 청산 {str(r.get('exit_date',''))[:10]} · {ret:.2f}%" if ret is not None else f"진입 {str(r.get('entry_date',''))[:10]}",
                "score": ret,
                "close": None,
                "entry_price": _safe_float(r.get("entry_price", None), None),
                "stop_loss": _safe_float(r.get("initial_stop_loss", r.get("final_stop_loss", None)), None),
                "target_price": _safe_float(r.get("target_price", None), None),
                "pivot_price": None,
                "portfolio_weight_pct": None,
                "reason": str(r.get("exit_reason", "")),
            })
        return rows

    # default: current portfolio candidates. This is intentionally small.
    pf = _load_csv_safe(OUTPUT_DIR / "multi_strategy_portfolio.csv", dtype={"stock_code": str})
    sig = _load_csv_safe(OUTPUT_DIR / "multi_strategy_signals_all.csv", dtype={"stock_code": str})
    data = pf.copy() if not pf.empty else sig.copy()
    if data.empty:
        return []
    if "stock_code" in data:
        data["stock_code"] = data["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    rows = []
    for _, r in data.head(limit).iterrows():
        score = r.get("best_strategy_score", r.get("strategy_score", r.get("signal_score", "")))
        rows.append({
            "rank": len(rows) + 1,
            "source": "current_portfolio",
            "stock_code": str(r.get("stock_code", "")).zfill(6),
            "stock_name": str(r.get("stock_name", r.get("name", ""))),
            "strategies": str(r.get("strategy_names", r.get("strategies", r.get("strategy_id", "")))),
            "signals": str(r.get("signals", r.get("signal", ""))),
            "score": _safe_float(score, None),
            "close": _safe_float(r.get("close", None), None),
            "entry_price": _safe_float(r.get("entry_price", None), None),
            "stop_loss": _safe_float(r.get("stop_loss", None), None),
            "target_price": _safe_float(r.get("target_price", None), None),
            "pivot_price": _safe_float(r.get("pivot_price", r.get("entry_price", None)), None),
            "portfolio_weight_pct": _safe_float(r.get("portfolio_weight_pct", None), None),
            "reason": str(r.get("reason", "")),
        })
    return rows

def _api_stock_payload(code: str) -> dict:
    code = str(code).strip().replace(".0", "").zfill(6)
    prices = read_prices(DATA_DIR / "prices_daily.csv")
    if prices.empty:
        return {"ok": False, "error": "prices_daily.csv 없음", "stock_code": code}
    prices["stock_code"] = prices["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    prices["date"] = pd.to_datetime(prices["date"])
    pr = prices[prices["stock_code"] == code].copy().sort_values("date")
    if pr.empty:
        return {"ok": False, "error": "해당 종목 가격 데이터 없음", "stock_code": code}
    for c in ["open", "high", "low", "close", "volume", "trading_value"]:
        if c in pr:
            pr[c] = pd.to_numeric(pr[c], errors="coerce")
    sig = _load_csv_safe(OUTPUT_DIR / "multi_strategy_signals_all.csv", dtype={"stock_code": str})
    if not sig.empty and "stock_code" in sig:
        sig["stock_code"] = sig["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        sig_code = sig[sig["stock_code"] == code].copy()
    else:
        sig_code = pd.DataFrame()
    best = {}
    if not sig_code.empty:
        score_col = "strategy_score" if "strategy_score" in sig_code else "signal_score" if "signal_score" in sig_code else None
        best = sig_code.sort_values(score_col, ascending=False).iloc[0].to_dict() if score_col else sig_code.iloc[0].to_dict()
    name_series = pr.get("stock_name", pr.get("name", pd.Series([code])))
    name = str(name_series.dropna().iloc[-1] if hasattr(name_series, "dropna") and len(name_series.dropna()) else code)
    levels = {
        "entry": _safe_float(best.get("entry_price", None), None),
        "stop": _safe_float(best.get("stop_loss", None), None),
        "target": _safe_float(best.get("target_price", None), None),
        "pivot": _safe_float(best.get("pivot_price", best.get("entry_price", None)), None),
    }
    show = pr.tail(1260).copy()
    # 이동평균 + Ichimoku 구름대. 차트 기간을 바꿔도 동일한 기준선/구름/거래 화살표를 유지한다.
    show["ma20"] = show["close"].rolling(20, min_periods=5).mean()
    show["ma60"] = show["close"].rolling(60, min_periods=15).mean()
    show["ma200"] = show["close"].rolling(200, min_periods=60).mean()
    conv = (show["high"].rolling(9, min_periods=5).max() + show["low"].rolling(9, min_periods=5).min()) / 2
    base = (show["high"].rolling(26, min_periods=13).max() + show["low"].rolling(26, min_periods=13).min()) / 2
    show["cloud_a"] = ((conv + base) / 2).shift(26)
    show["cloud_b"] = ((show["high"].rolling(52, min_periods=26).max() + show["low"].rolling(52, min_periods=26).min()) / 2).shift(26)
    candles = []
    for _, r in show.iterrows():
        candles.append({"date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"), "open": _safe_float(r.get("open"), None), "high": _safe_float(r.get("high"), None), "low": _safe_float(r.get("low"), None), "close": _safe_float(r.get("close"), None), "volume": _safe_float(r.get("volume"), 0), "ma20": _safe_float(r.get("ma20"), None), "ma60": _safe_float(r.get("ma60"), None), "ma200": _safe_float(r.get("ma200"), None), "cloud_a": _safe_float(r.get("cloud_a"), None), "cloud_b": _safe_float(r.get("cloud_b"), None)})
    latest = pr.iloc[-1]
    high52 = pr.tail(252)["high"].max() if "high" in pr else np.nan
    def ret(n):
        if len(pr) <= n: return None
        a = float(pr["close"].iloc[-n-1]); b = float(pr["close"].iloc[-1])
        return (b / a - 1) * 100 if a else None
    metrics = {"latest_close": _safe_float(latest.get("close"), None), "trading_value": _safe_float(latest.get("trading_value"), None), "ret20": ret(20), "ret60": ret(60), "ret120": ret(120), "from_52w_high_pct": _safe_float((float(latest.get("close"))/float(high52)-1)*100 if high52 and np.isfinite(high52) else np.nan, None)}
    if levels.get("pivot") and latest.get("close"):
        metrics["pivot_gap_pct"] = _safe_float((float(latest.get("close")) / float(levels.get("pivot")) - 1) * 100, None)
    entry, stop, target = levels.get("entry"), levels.get("stop"), levels.get("target")
    if entry and stop and entry > stop: metrics["risk_pct"] = ((entry - stop) / entry) * 100
    if entry and target: metrics["reward_pct"] = (target / entry - 1) * 100
    if metrics.get("risk_pct") and metrics.get("reward_pct"): metrics["rr"] = metrics["reward_pct"] / metrics["risk_pct"]
    signals = []
    if not sig_code.empty:
        for _, r in sig_code.head(20).iterrows():
            signals.append({"strategy": str(r.get("strategy_name", r.get("strategy_id", ""))), "signal": str(r.get("signal", "")), "score": _safe_float(r.get("strategy_score", r.get("signal_score", None)), None), "entry_price": _safe_float(r.get("entry_price", None), None), "stop_loss": _safe_float(r.get("stop_loss", None), None), "target_price": _safe_float(r.get("target_price", None), None), "reason": str(r.get("reason", "")), "pivot_price": _safe_float(r.get("pivot_price", None), None), "pivot_gap_pct": _safe_float(r.get("pivot_gap_pct", None), None), "ma20_gap_pct": _safe_float(r.get("ma20_gap_pct", None), None), "ma60_gap_pct": _safe_float(r.get("ma60_gap_pct", None), None), "stop_distance_pct": _safe_float(r.get("stop_distance_pct", None), None), "entry_quality_status": str(r.get("entry_quality_status", "")), "entry_filter_reason": str(r.get("entry_filter_reason", ""))})
    trades_df = _load_csv_safe(OUTPUT_DIR / "backtest_trades.csv", dtype={"stock_code": str})
    trade_markers = []
    if not trades_df.empty and "stock_code" in trades_df:
        trades_df["stock_code"] = trades_df["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        td = trades_df[trades_df["stock_code"] == code].tail(30).copy()
        for _, r in td.iterrows():
            trade_markers.append({"strategy": str(r.get("strategy_name", r.get("strategy_id", ""))), "entry_date": str(r.get("entry_date", "")), "exit_date": str(r.get("exit_date", "")), "entry_price": _safe_float(r.get("entry_price", None), None), "exit_price": _safe_float(r.get("exit_price", None), None), "return_pct": _safe_float(r.get("return_pct", None), None), "exit_reason": str(r.get("exit_reason", "")), "holding_days": _safe_int(r.get("holding_days", 0), 0), "entry_type": str(r.get("entry_type", "")), "entry_pivot_gap_pct": _safe_float(r.get("entry_pivot_gap_pct", None), None)})
    financial = {}
    fin = _load_csv_safe(DATA_DIR / "financials.csv", dtype={"stock_code": str})
    if not fin.empty and "stock_code" in fin:
        fin["stock_code"] = fin["stock_code"].astype(str).str.zfill(6)
        fr = fin[fin["stock_code"] == code]
        if not fr.empty: financial = {str(k): str(v) for k, v in fr.iloc[-1].to_dict().items() if str(k) != "stock_code"}
    supply = {}
    sup = _load_csv_safe(DATA_DIR / "supply_daily.csv", dtype={"stock_code": str})
    if not sup.empty and "stock_code" in sup:
        sup["stock_code"] = sup["stock_code"].astype(str).str.zfill(6)
        sr = sup[sup["stock_code"] == code].tail(20).copy()
        if not sr.empty:
            for col in ["institution_net_buy_value", "foreign_net_buy_value", "individual_net_buy_value"]:
                if col in sr: supply[col + "_20sum"] = _safe_float(pd.to_numeric(sr[col], errors="coerce").sum(), None)
    return {"ok": True, "stock_code": code, "stock_name": name, "candles": candles, "levels": levels, "metrics": metrics, "signals": signals, "trades": trade_markers, "financial": financial, "supply": supply}


def _api_backtest_audit() -> dict:
    summary = _load_csv_safe(OUTPUT_DIR / "backtest_summary.csv")
    trades = _load_csv_safe(OUTPUT_DIR / "backtest_trades.csv")
    signals = _load_csv_safe(OUTPUT_DIR / "backtest_signals.csv")
    audit = _load_csv_safe(OUTPUT_DIR / "backtest_audit.csv")
    excess = _load_csv_safe(OUTPUT_DIR / "backtest_excess_summary.csv")
    algo = _load_csv_safe(OUTPUT_DIR / "algorithm_audit.csv")
    injections = _load_csv_safe(OUTPUT_DIR / "backtest_capital_injections.csv")
    entry_filters = _load_csv_safe(OUTPUT_DIR / "backtest_entry_filter_audit.csv")
    out = {"summary": summary.iloc[0].to_dict() if not summary.empty else {}}
    if not audit.empty and set(["item", "value"]).issubset(audit.columns):
        out.update(dict(zip(audit["item"].astype(str), audit["value"].astype(str))))
    if not trades.empty and "entry_date" in trades:
        t = trades.copy(); t["entry_date"] = pd.to_datetime(t["entry_date"], errors="coerce"); t["year"] = t["entry_date"].dt.year
        if "return_pct" in t:
            t["return_pct"] = pd.to_numeric(t["return_pct"], errors="coerce")
        yrs = t.groupby("year").agg(trades=("stock_code","count"), avg_return_pct=("return_pct","mean"), wins=("return_pct", lambda x: int((pd.to_numeric(x, errors='coerce')>0).sum()))).reset_index()
        out["trade_years"] = yrs.fillna(0).to_dict("records")
    else:
        out["trade_years"] = []
    if not signals.empty and "date" in signals:
        s = signals.copy(); s["date"] = pd.to_datetime(s["date"], errors="coerce"); s["year"] = s["date"].dt.year
        gy = s.groupby(["year", "strategy_id"]).size().reset_index(name="signals") if "strategy_id" in s else pd.DataFrame()
        out["signal_years"] = gy.to_dict("records")
    else:
        out["signal_years"] = []
    out["benchmarks"] = excess.fillna("").to_dict("records") if not excess.empty else []
    out["entry_filter_audit"] = entry_filters.fillna("").to_dict("records") if not entry_filters.empty else []
    out["algorithm_audit"] = algo.fillna("").to_dict("records") if not algo.empty else []
    if not injections.empty:
        out["capital_injections"] = injections.fillna("").tail(100).to_dict("records")
        try:
            out["capital_injection_count"] = int(len(injections))
            out["capital_injection_total"] = float(pd.to_numeric(injections.get("contribution_cash", 0), errors="coerce").fillna(0).sum())
        except Exception:
            out["capital_injection_count"] = len(injections)
    else:
        out["capital_injections"] = []
        out["capital_injection_count"] = 0
    return out

def _backtest_explorer_page() -> str:
    return r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>백테스트 검증</title><style>body{margin:0;background:#07111f;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.wrap{max-width:1250px;margin:0 auto;padding:24px 16px 60px}.hero{background:linear-gradient(135deg,#020617,#1d4ed8);border:1px solid #1e293b;border-radius:22px;padding:22px}a{color:#38bdf8;text-decoration:none;font-weight:800}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}.card{background:#0b1220;border:1px solid #1e293b;border-radius:15px;padding:12px}.card b{display:block;color:#93c5fd;font-size:12px}.card span{font-size:19px;font-weight:900}.panel{background:#0b1220;border:1px solid #1e293b;border-radius:18px;padding:16px;margin-top:14px}table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #1e293b;padding:8px;text-align:left;font-size:13px}th{color:#93c5fd}.muted{color:#94a3b8;line-height:1.55}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style></head><body><main class="wrap"><section class="hero"><h1>백테스트 검증 패널</h1><p>이 화면은 “과거 데이터로 실제로 테스트했는지” 확인하는 용도입니다. 신호일 종가 확정 후 다음 거래일 시가 진입, 수수료/세금/슬리피지 반영, 손절·익절 일봉 체결 로직을 사용합니다.</p><p><a href="/terminal">인터랙티브 터미널</a> · <a href="/">홈</a> · <a href="/backtest-report">기존 리포트</a></p></section><div id="cards" class="grid"></div><section class="panel"><h2>연도별 거래 수</h2><div id="years" class="muted">로딩 중</div></section><section class="panel"><h2>연도·전략별 신호 수</h2><div id="signals" class="muted">로딩 중</div></section><section class="panel"><h2>벤치마크 비교</h2><div id="bench" class="muted">로딩 중</div></section><section class="panel"><h2>알고리즘 구현 감사</h2><div id="algo" class="muted">로딩 중</div></section><section class="panel"><h2>추격매수 제외 감사</h2><div id="entryfilter" class="muted">로딩 중</div></section><section class="panel"><h2>검증 판단</h2><div id="judge" class="muted">-</div></section></main><script>const f=v=>v==null||isNaN(v)?'-':Number(v).toFixed(2), p=v=>v==null||isNaN(v)?'-':Number(v).toFixed(2)+'%';fetch('/api/backtest-audit').then(r=>r.json()).then(d=>{const s=d.summary||{};const cards=[['평가 시작',d.equity_start||'-'],['평가 종료',d.equity_end||'-'],['총수익률',p(s.total_return_pct)],['MDD',p(s.mdd_pct)],['거래수',s.trades??'-'],['승률',p(s.win_rate_pct)],['PF',f(s.profit_factor)],['대상 종목',s.symbols_after??'-'],['추가투입 횟수',d.capital_injection_count??0],['총투입수익률',p(s.return_on_total_contributed_pct)]];document.getElementById('cards').innerHTML=cards.map(c=>`<div class="card"><b>${c[0]}</b><span>${c[1]}</span></div>`).join('');document.getElementById('years').innerHTML=d.trade_years?.length?`<table><thead><tr><th>연도</th><th>거래수</th><th>평균수익률</th><th>승리거래</th></tr></thead><tbody>${d.trade_years.map(r=>`<tr><td>${r.year}</td><td>${r.trades}</td><td>${p(r.avg_return_pct)}</td><td>${r.wins}</td></tr>`).join('')}</tbody></table>`:'거래내역 없음';document.getElementById('signals').innerHTML=d.signal_years?.length?`<table><thead><tr><th>연도</th><th>전략</th><th>신호 수</th></tr></thead><tbody>${d.signal_years.map(r=>`<tr><td>${r.year}</td><td>${r.strategy_id}</td><td>${r.signals}</td></tr>`).join('')}</tbody></table>`:'신호 없음';document.getElementById('bench').innerHTML=d.benchmarks?.length?`<table><thead><tr><th>벤치마크</th><th>벤치 수익률</th><th>초과수익</th><th>벤치 MDD</th><th>MDD 우위</th></tr></thead><tbody>${d.benchmarks.map(r=>`<tr><td>${r.benchmark_name}</td><td>${p(r.benchmark_total_return_pct)}</td><td>${p(r.excess_total_return_pct)}</td><td>${p(r.benchmark_mdd_pct)}</td><td>${p(r.mdd_advantage_pct)}</td></tr>`).join('')}</tbody></table>`:'벤치마크 결과 없음';document.getElementById('entryfilter').innerHTML=d.entry_filter_audit?.length?`<table><thead><tr><th>전략</th><th>유형</th><th>제외사유</th><th>제외수</th></tr></thead><tbody>${d.entry_filter_audit.map(r=>`<tr><td>${r.strategy_id}</td><td>${r.entry_type}</td><td>${r.reason}</td><td>${r.excluded}</td></tr>`).join('')}</tbody></table>`:'제외 내역 없음';document.getElementById('algo').innerHTML=d.algorithm_audit?.length?`<table><thead><tr><th>전략</th><th>상태</th><th>판정</th><th>한계</th></tr></thead><tbody>${d.algorithm_audit.map(r=>`<tr><td>${r.strategy_name}</td><td>${r.implementation_status}</td><td>${r.verdict}</td><td>${r.known_limits}</td></tr>`).join('')}</tbody></table>`:'알고리즘 감사 결과 없음';let judge=''; if((s.trades||0)<100)judge+='거래 수가 적어 통계 신뢰도가 낮습니다. 평가기간 또는 대상종목 수를 늘려보세요.<br>'; if((s.mdd_pct||0)<-25)judge+='MDD가 큽니다. 리스크 오버레이/현금비중/시장필터를 강화해야 합니다.<br>'; if((s.profit_factor||0)<1.2)judge+='Profit Factor가 낮습니다. 전략 필터나 청산 로직 개선이 필요합니다.<br>'; if(!judge)judge='현재 백테스트는 과거 구간 전체에서 신호와 거래가 생성되고 있으며, 벤치마크 대비 초과수익/낙폭도 함께 확인할 수 있습니다.';document.getElementById('judge').innerHTML=judge;});</script></body></html>"""

def serve(port: int = 8765) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
            self.send_response(status); self.send_header("Content-Type", content_type); self.end_headers(); self.wfile.write(body.encode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            # 리포트 및 산출물 파일 제공
            if parsed.path.startswith("/docs/"):
                rel = parsed.path.lstrip("/")
                doc_path = Path(rel)
                if doc_path.exists() and doc_path.is_file():
                    self._send(doc_path.read_text(encoding="utf-8"), content_type="text/plain; charset=utf-8")
                else:
                    self._send("문서를 찾을 수 없습니다", status=404, content_type="text/plain; charset=utf-8")
                return
            if parsed.path.startswith("/ai-copilot-result"):
                path = OUTPUT_DIR / "ai_copilot_result.html"
                if path.exists():
                    self._send(path.read_text(encoding="utf-8")); return
                self._send("<html><meta charset='utf-8'><body><h1>아직 AI 결과가 없습니다.</h1><p><a href='/ai-copilot'>AI Copilot</a></p></body></html>"); return
            if parsed.path.startswith("/ai-patch-preview"):
                html_path = OUTPUT_DIR / "ai_copilot_result.html"
                if html_path.exists():
                    self._send(html_path.read_text(encoding="utf-8")); return
                prop = OUTPUT_DIR / "ai_copilot_patch_proposal.json"
                if prop.exists():
                    self._send("<html><meta charset='utf-8'><body><pre>" + html.escape(prop.read_text(encoding='utf-8')) + "</pre><form method='post' action='/ai-apply-patch'><label><input type='checkbox' name='approve' value='yes'> 승인</label><button>적용</button></form></body></html>"); return
                self._send("<html><meta charset='utf-8'><body><h1>패치 제안 없음</h1><p><a href='/ai-copilot'>AI Copilot</a></p></body></html>"); return
            if parsed.path.startswith("/ai-copilot"):
                env_vals = {}
                if Path(".env").exists():
                    for line in Path(".env").read_text(encoding="utf-8").splitlines():
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1); env_vals[k] = v
                self._send(_ai_copilot_page(env_vals)); return
            if parsed.path.startswith("/terminal"):
                self._send(_interactive_terminal_page()); return
            if parsed.path.startswith("/api/terminal"):
                qs = parse_qs(parsed.query)
                limit = int(float((qs.get("limit") or ["250"])[0] or 250))
                source = (qs.get("source") or ["portfolio"])[0]
                self._send(json.dumps({"rows": _api_terminal_rows(limit, source), "source": source}, ensure_ascii=False), content_type="application/json; charset=utf-8"); return
            if parsed.path.startswith("/api/stock"):
                qs = parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                self._send(json.dumps(_api_stock_payload(code), ensure_ascii=False), content_type="application/json; charset=utf-8"); return
            if parsed.path.startswith("/api/backtest-audit"):
                self._send(json.dumps(_api_backtest_audit(), ensure_ascii=False), content_type="application/json; charset=utf-8"); return
            if parsed.path.startswith("/backtest-explorer"):
                self._send(_backtest_explorer_page()); return
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
            if self.path in ["/daily_signal_report.csv", "/daily_signal_report.json", "/daily_signal_report.md", "/ranking_full.csv", "/multi_strategy_portfolio.csv", "/multi_strategy_signals_all.csv", "/multi_strategy_portfolio.json", "/backtest_equity_curve.csv", "/backtest_trades.csv", "/backtest_summary.csv", "/backtest_strategy_summary.csv", "/backtest_signals.csv", "/backtest_market_regime.csv", "/backtest_audit.csv", "/backtest_benchmark_equity.csv", "/backtest_benchmark_summary.csv", "/backtest_excess_summary.csv", "/algorithm_audit.csv"]:
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
                elif self.path == "/save-ai-settings":
                    _save_ai_settings_from_form(form)
                    msg = "AI Provider 설정 저장 완료"
                elif self.path == "/ai-copilot-run":
                    tasks = [str(x) for x in form.get("ai_tasks", []) if str(x).strip()]
                    if not tasks:
                        tasks = ["stock_analysis", "backtest_diagnosis"]
                    stock_code = str(form.get("stock_code", [""])[0]).strip() or None
                    user_prompt = str(form.get("user_prompt", [""])[0]).strip()
                    result = run_copilot(DATA_DIR, OUTPUT_DIR, tasks=tasks, stock_code=stock_code, user_prompt=user_prompt, settings=CopilotSettings.from_env())
                    msg = "AI Copilot 실행 완료. <a href='/ai-copilot-result'>결과 보기</a>"
                elif self.path == "/ai-apply-patch":
                    if form.get("approve", [""])[0] != "yes":
                        raise RuntimeError("승인 체크가 필요합니다.")
                    apply_result = apply_patch_proposal(Path('.'), OUTPUT_DIR / "ai_copilot_patch_proposal.json")
                    msg = "AI 패치 적용 완료. 적용: " + str(len(apply_result.get('applied', []))) + ", 스킵: " + str(len(apply_result.get('skipped', [])))
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
                elif self.path == "/ai-theme-run":
                    max_per_theme = int(float(form.get("max_per_theme", ["50"])[0] or 50))
                    min_score = float(form.get("min_score", ["45"])[0] or 45)
                    _run_job("AI 장기 테마 분석", run_ai_theme_mode, max_per_theme=max_per_theme, min_score=min_score)
                    msg = "AI 장기 테마 분석 시작. 완료 후 /ai-theme-report를 확인하세요."
                elif self.path == "/optimize-ultra10x":
                    raw = str(form.get("max_candidates", [""])[0]).strip()
                    max_candidates = int(float(raw)) if raw else None
                    _run_job("초공격 10x 과최적화 방지 최적화", run_ultra10x_optimizer, max_candidates=max_candidates)
                    msg = "초공격 10x 최적화 시작. 작업상태에서 확인하세요. 완료 후 /ultra10x-report를 확인하세요."
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
    an.add_argument("--strategies", default="all", help="vcp,canslim,stage2,darvas,deep_turnaround 또는 all")
    sub.add_parser("build-derived", help="prices_daily.csv에서 prices_weekly.csv와 sector_strength.csv 생성")
    theme = sub.add_parser("ai-theme", help="AI 장기 테마 섹터별 후보 CSV/리포트 생성")
    theme.add_argument("--max-per-theme", type=int, default=50)
    theme.add_argument("--min-score", type=float, default=45.0)
    opt = sub.add_parser("optimize-ultra10x", help="초공격 10x 집중형 프로필을 train/validation/test 분리로 탐색")
    opt.add_argument("--max-candidates", type=int, default=None, help="빠른 테스트용 후보 수 제한")
    bt = sub.add_parser("backtest", help="4전략 포트폴리오 백테스트 실행")
    bt.add_argument("--config", default=None)
    bt.add_argument("--strategies", default="all", help="vcp,canslim,stage2,darvas,deep_turnaround 또는 all")
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
    elif args.cmd == "ai-theme":
        run_ai_theme_mode(max_per_theme=args.max_per_theme, min_score=args.min_score)
    elif args.cmd == "optimize-ultra10x":
        run_ultra10x_optimizer(max_candidates=args.max_candidates)
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
