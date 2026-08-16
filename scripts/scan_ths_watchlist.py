"""Synchronize and deterministically scan TongHuaShun A-share watchlists."""

# ruff: noqa: E402 -- support both ``python -m`` and direct script execution.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pa_agent.config.paths import TRADES_DB_PATH
from pa_agent.config.settings import load_settings
from pa_agent.trading.daily_candidates import DailyCandidateScanner
from pa_agent.trading.quant import Hs300DailyPullbackStrategy
from pa_agent.trading.store import SCHEMA_VERSION, TradeStore
from pa_agent.trading.ths_watchlist import ThsWatchlistScanService


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "只读导入同花顺全部自选分类中的沪深A股，并使用现有确定性策略逐只扫描"
        )
    )
    parser.add_argument(
        "--install-root",
        type=Path,
        required=True,
        help="同花顺远航版安装根目录（其下应存在 bin/users）",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=TRADES_DB_PATH,
        help="PA Agent交易数据库路径",
    )
    parser.add_argument("--force", action="store_true", help="忽略结果缓存并重新扫描")
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="只同步自选分类，不请求行情或运行策略",
    )
    parser.add_argument("--output", type=Path, help="可选：写入完整JSON报告")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    settings = load_settings()
    store = TradeStore(args.database)
    if not store.available:
        raise RuntimeError(f"交易数据库不可用：{store.error}")
    scanner = DailyCandidateScanner(Hs300DailyPullbackStrategy(settings.strategy))
    service = ThsWatchlistScanService(
        store,
        scanner,
        install_root=args.install_root,
    )
    if args.sync_only:
        snapshot = service.synchronize()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": "sync_only",
            "source_hash": snapshot.source_hash,
            "source_updated_at": snapshot.source_updated_at,
            "category_count": len(snapshot.categories),
            "categories": snapshot.categories,
            "a_share_count": len(snapshot.members),
            "rejected_count": len(snapshot.rejected),
            "rejected": snapshot.rejected,
        }
    else:
        report = service.scan(
            force=args.force,
            progress=lambda current, total, symbol: print(
                f"[{current:>3}/{total}] {symbol}", file=sys.stderr, flush=True
            ),
        )
        sync = store.latest_ths_watchlist_sync() or {}
        snapshot = dict(sync.get("snapshot") or {})
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": "full_scan",
            "source_hash": report.source_hash,
            "source_updated_at": snapshot.get("source_updated_at", ""),
            "category_count": len(snapshot.get("categories") or []),
            "categories": snapshot.get("categories") or [],
            "a_share_count": report.total,
            "rejected_count": len(snapshot.get("rejected") or []),
            "rejected": snapshot.get("rejected") or [],
            **report.model_dump(mode="json"),
        }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
