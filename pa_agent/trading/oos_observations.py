"""Append-only production observations for the post-freeze OOS dataset."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from typing import Any

from pa_agent.trading.hotspots import HOTSPOT_RULE_VERSION, HotspotService
from pa_agent.trading.topdown import HotspotSnapshot
from pa_agent.trading.universe import (
    CLOUD_AI_RISK_THEME,
    cloud_ai_member,
)
from pa_agent.trading.validation_epoch import ValidationEpochRegistry

EASTMONEY_DAILY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_INTRADAY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_SENTIMENT_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_HOTSPOT_URL = "https://finance.eastmoney.com/"
POOL_MONITOR_STRATEGY_PREFIX = "ashare_private_pool_monitor_v1"


def pool_monitor_strategy_version(pool_version: str) -> str:
    """Return an isolated append-only ledger namespace for one pool revision."""
    value = str(pool_version or "").strip()
    if not value:
        raise ValueError("pool monitor requires a pool version")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{POOL_MONITOR_STRATEGY_PREFIX}-{digest}"


def record_pool_monitor_row(
    store: Any,
    *,
    pool_version: str,
    symbol: str,
    row: dict[str, Any],
) -> str | None:
    """Persist one timely 15-minute fact without polluting frozen OOS data."""
    effective = _time(row.get("time"))
    observed = _time(row.get("observed_at"))
    if observed < effective or observed - effective > timedelta(minutes=5):
        return None
    if any(row.get(key) is None for key in ("suspended", "limit_locked")):
        return None
    record = {
        "pool_version": str(pool_version),
        "effective_at": effective.isoformat(),
        "source_published_at": effective.isoformat(),
        "observed_at": observed.isoformat(),
        "instrument_type": "stock",
        "symbol": str(symbol),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row.get("volume") or 0),
        "amount": float(row.get("amount") or 0),
        "suspended": bool(row["suspended"]),
        "limit_locked": bool(row["limit_locked"]),
    }
    return store.add_oos_observation(
        strategy_version=pool_monitor_strategy_version(pool_version),
        kind="intraday_15m",
        symbol=str(symbol),
        effective_at=effective.isoformat(),
        source_published_at=effective.isoformat(),
        source_kind="eastmoney_market",
        source_url=EASTMONEY_INTRADAY_URL,
        payload=record,
        captured_at=observed.isoformat(),
    )


class OosObservationRecorder:
    """Translate production collector outputs into immutable audit rows."""

    def __init__(
        self, store: Any, *, validation_epochs: ValidationEpochRegistry | None = None
    ) -> None:
        self.store = store
        self.validation_epochs = validation_epochs or ValidationEpochRegistry(store)

    @property
    def epoch(self):
        return self.validation_epochs.require_current()

    @property
    def frozen_at(self) -> datetime:
        return _time(self.epoch.activated_at)

    def record_universe(self, snapshot: Any) -> str | None:
        payload = _payload(snapshot)
        if set(payload.get("symbols") or []) != set(self.epoch.symbols):
            return None
        return self.record_strategy_definition()

    def record_strategy_definition(self) -> str:
        # One second after the immutable strategy freeze creates a stable,
        # idempotent point-in-time definition without importing pre-freeze bars.
        effective = self.frozen_at + timedelta(seconds=1)
        source_published = self.frozen_at
        epoch = self.epoch
        record = {
            "effective_at": effective.isoformat(),
            "source_published_at": source_published.isoformat(),
            "symbols": list(epoch.symbols),
            "authorization_symbols": list(epoch.authorization_symbols),
            "universe_id": epoch.universe_id,
            "universe_source_hash": epoch.member_hash,
            "pool_version": epoch.pool_version,
        }
        return self._add(
            "historical_constituents", record, effective=effective,
            published=source_published, source_kind="strategy_definition",
            source_url=f"pa-agent://validation-epoch/{epoch.epoch_id}",
        )

    def record_hotspot(self, snapshot: Any) -> str | None:
        payload = _payload(snapshot)
        epoch = self.epoch
        if str(payload.get("symbol") or "") not in epoch.symbols:
            return None
        if epoch.is_private_pool and (
            payload.get("validation_epoch_id") != epoch.epoch_id
            or payload.get("member_hash") != epoch.member_hash
            or payload.get("pool_version") not in epoch.pool_versions
        ):
            return None
        effective = _time(payload.get("frozen_at"))
        if effective <= self.frozen_at or payload.get("rule_version") != HOTSPOT_RULE_VERSION:
            return None
        observed = _time(payload.get("captured_at") or payload.get("frozen_at"))
        if observed < effective or observed - effective > timedelta(minutes=5):
            return None
        source_times = []
        items = []
        for item in payload.get("items") or []:
            published_raw = str(item.get("published_at") or "")
            if not published_raw:
                continue
            published = _time(published_raw)
            if published > effective:
                continue
            source_times.append(published)
            source_kind = _hotspot_source_kind(item)
            items.append({**item, "source_kind": source_kind})
        if not source_times:
            # A board-only snapshot is still valid raw evidence. It cannot
            # create a positive news score, but preserving it avoids selecting
            # only symbols that happened to have news items.
            if not payload.get("board_strength"):
                return None
            published = effective
        else:
            published = max(source_times)
        record = {
            **payload,
            "effective_at": effective.isoformat(),
            "source_published_at": published.isoformat(),
            "observed_at": observed.isoformat(),
            "items": items,
        }
        metrics = HotspotService.theme_metrics(
            HotspotSnapshot.model_validate(payload)
        )
        if metrics is not None:
            # Persist the deterministic inputs consumed by the 4:3:2:1 theme
            # score. Missing board-flow dimensions stay missing; they are
            # never replaced with zero.
            record["theme_metrics"] = metrics
        return self._add(
            "hotspots", record, symbol=str(payload.get("symbol") or ""),
            effective=effective, published=published,
            source_kind="eastmoney_news", source_url=EASTMONEY_HOTSPOT_URL,
            captured=observed,
        )

    def record_sentiment(self, snapshot: Any) -> str | None:
        payload = _payload(snapshot)
        if not payload.get("data_complete") or not isinstance(payload.get("input"), dict):
            return None
        effective = _time(payload.get("captured_at"))
        observed = _time(payload.get("observed_at") or payload.get("captured_at"))
        published = _time(payload.get("source_as_of"))
        if (
            effective <= self.frozen_at
            or published > effective
            or observed < effective
            or observed - effective > timedelta(minutes=5)
        ):
            return None
        record = {
            "effective_at": effective.isoformat(),
            "source_published_at": published.isoformat(),
            "observed_at": observed.isoformat(),
            **payload["input"],
        }
        return self._add(
            "market_sentiment", record, effective=effective, published=published,
            source_kind="eastmoney_market", source_url=EASTMONEY_SENTIMENT_URL,
        )

    def record_intraday_bar(self, symbol: str, bar: Any) -> str | None:
        # KlineBar timestamps in this application are the East Money close label.
        if str(symbol) not in self.epoch.authorization_symbols:
            return None
        effective = datetime.fromtimestamp(float(bar.ts_open) / 1000).astimezone()
        if effective <= self.frozen_at or not bool(getattr(bar, "closed", False)):
            return None
        record = {
            "effective_at": effective.isoformat(),
            "source_published_at": effective.isoformat(),
            "instrument_type": "stock",
            "symbol": symbol,
            "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
            "close": float(bar.close), "volume": float(bar.volume),
            "amount": float(getattr(bar, "amount", 0) or 0),
            "adjustment_factor": 1,
        }
        return self._add(
            "intraday_15m", record, symbol=symbol, effective=effective,
            published=effective, source_kind="eastmoney_market",
            source_url=EASTMONEY_INTRADAY_URL,
        )

    def record_market_row(
        self,
        *,
        kind: str,
        instrument_type: str,
        symbol: str,
        row: dict[str, Any],
    ) -> str | None:
        if instrument_type == "stock" and symbol not in self.epoch.authorization_symbols:
            return None
        effective = _time(row.get("time"))
        if effective <= self.frozen_at:
            return None
        observed = _time(row.get("observed_at"))
        max_delay = timedelta(
            minutes=15 if kind == "daily_bars" else 5
        )
        if observed < effective or observed - effective > max_delay:
            return None
        record = {
            "effective_at": effective.isoformat(),
            "source_published_at": effective.isoformat(),
            "observed_at": observed.isoformat(),
            "instrument_type": instrument_type,
            "symbol": symbol,
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row.get("volume") or 0),
            "amount": float(row.get("amount") or 0),
            # Collection is intentionally raw/unadjusted. Export enrichment
            # must establish any non-1 adjustment relationship explicitly.
            "adjustment_factor": 1,
        }
        if instrument_type == "stock":
            required = ["suspended", "limit_locked"]
            if kind == "daily_bars":
                required.extend(("is_st", "delisting", "listed_days", "industry"))
            if any(row.get(key) is None for key in required):
                return None
            record.update({key: row[key] for key in required})
        return self._add(
            kind, record, symbol=symbol, effective=effective, published=effective,
            source_kind="eastmoney_market",
            source_url=(EASTMONEY_DAILY_URL if kind == "daily_bars" else EASTMONEY_INTRADAY_URL),
            captured=observed,
        )

    def _add(
        self, kind: str, record: dict[str, Any], *, effective: datetime,
        published: datetime, source_kind: str, source_url: str, symbol: str = "",
        captured: datetime | None = None,
    ) -> str:
        epoch = self.epoch
        record = {
            **record,
            "validation_epoch_id": epoch.epoch_id,
            "pool_version": epoch.pool_version,
            "member_hash": epoch.member_hash,
        }
        return self.store.add_oos_observation(
            strategy_version=epoch.observation_strategy_version,
            kind=kind,
            symbol=symbol,
            effective_at=effective.isoformat(),
            source_published_at=published.isoformat(),
            source_kind=source_kind,
            source_url=source_url,
            payload=record,
            captured_at=captured.isoformat() if captured is not None else "",
        )


def _payload(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        point = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}T00:00:00+08:00"
        point = datetime.fromisoformat(text)
    if point.tzinfo is None:
        point = point.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return point


def _hotspot_source_kind(item: dict[str, Any]) -> str:
    if item.get("official"):
        return "company_announcement"
    return "eastmoney_news"


class OosMarketObservationService:
    """Capture the fixed pool and four indexes without conditioning on signals."""

    INDEXES = ("000300", "000001", "000852", "399006")

    def __init__(
        self,
        recorder: OosObservationRecorder,
        *,
        minute_loader=None,
        daily_loader=None,
        index_daily_loader=None,
        profile_loader=None,
        clock=None,
    ) -> None:
        if (
            minute_loader is None
            or daily_loader is None
            or index_daily_loader is None
            or profile_loader is None
        ):
            from pa_agent.data.eastmoney_client import (
                fetch_index_daily,
                fetch_stock_daily_recent,
                fetch_stock_listing_profile,
                fetch_stock_minute,
            )
            minute_loader = minute_loader or fetch_stock_minute
            daily_loader = daily_loader or fetch_stock_daily_recent
            index_daily_loader = index_daily_loader or fetch_index_daily
            profile_loader = profile_loader or fetch_stock_listing_profile
        self.recorder = recorder
        self.minute_loader = minute_loader
        self.daily_loader = daily_loader
        self.index_daily_loader = index_daily_loader
        self.profile_loader = profile_loader
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._profile_cache: dict[tuple[str, Any], dict[str, Any]] = {}

    def _profile_for(self, symbol: str, expected: datetime) -> dict[str, Any]:
        key = (symbol, expected.date())
        cached = self._profile_cache.get(key)
        if cached is not None:
            return cached
        profile = self.profile_loader(symbol)
        if not profile:
            raise ValueError("listing_profile_missing")
        value = dict(profile)
        self._profile_cache[key] = value
        return value

    def capture(
        self,
        *,
        captured_at: datetime | None = None,
        monitor_universe: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from pa_agent.trading.topdown_market_data import expected_oos_market_close

        now = (captured_at or datetime.now().astimezone()).astimezone()
        self.recorder.record_strategy_definition()
        expected = expected_oos_market_close(now)
        if expected is None:
            return {"status": "outside_market_window", "captured": 0, "failures": []}
        daily_recovery_only = (
            expected.time() == time(15, 0)
            and now - expected > timedelta(minutes=5)
        )
        # Only authorization-eligible instruments are required for joint
        # daily/15m OOS scoring.  Analysis-only members remain in the frozen
        # strategy definition and hotspot/announcement monitoring.
        epoch = self.recorder.epoch
        oos_jobs = [
            ("stock", symbol) for symbol in epoch.authorization_symbols
        ] + [("index", symbol) for symbol in self.INDEXES]
        oos_job_set = set(oos_jobs)
        monitor_pool_version = str((monitor_universe or {}).get("version") or "")
        monitor_symbols = {
            str(symbol)
            for symbol in (monitor_universe or {}).get("monitor_symbols") or []
            if str(symbol)
        }
        monitor_jobs = (
            []
            if daily_recovery_only
            else [("stock", symbol) for symbol in sorted(monitor_symbols)]
        )
        jobs = list(dict.fromkeys([*oos_jobs, *monitor_jobs]))
        captured = 0
        failures: list[str] = []
        monitor_captured = 0
        monitor_failures: list[str] = []
        start = (expected - timedelta(days=3)).strftime("%Y-%m-%d 00:00:00")
        end = expected.strftime("%Y-%m-%d %H:%M:%S")

        def fetch_minute(job):
            instrument_type, symbol = job
            rows = (
                []
                if daily_recovery_only
                else self.minute_loader(
                    symbol, period="15", start_date=start, end_date=end,
                    adjust="none", is_index=instrument_type == "index",
                )
            )
            daily_rows = (
                self.daily_loader(symbol, n=3, adjust="none")
                if instrument_type == "stock"
                else []
            )
            profile = (
                self._profile_for(symbol, expected)
                if instrument_type == "stock"
                else {}
            )
            return job, rows, daily_rows, profile

        stock_daily_rows: dict[str, list[dict[str, Any]]] = {}
        stock_profiles: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_minute, job): job for job in jobs}
            for future in as_completed(futures):
                instrument_type, symbol = futures[future]
                try:
                    _, rows, daily_rows, profile = future.result()
                    if instrument_type == "stock":
                        stock_daily_rows[symbol] = daily_rows
                        stock_profiles[symbol] = profile
                    if daily_recovery_only:
                        continue
                    row = next(
                        (item for item in reversed(rows) if _time(item.get("time")) == expected),
                        None,
                    )
                    if row is None:
                        if (instrument_type, symbol) in oos_job_set:
                            failures.append(f"intraday_{symbol}_expected_bar_missing")
                        if instrument_type == "stock" and symbol in monitor_symbols:
                            monitor_failures.append(
                                f"intraday_{symbol}_expected_bar_missing"
                            )
                        continue
                    if instrument_type == "stock":
                        row = {
                            **row,
                            **_intraday_stock_metadata(
                                symbol, row, daily_rows, profile, expected
                            ),
                        }
                    row = {**row, "observed_at": self.clock().astimezone()}
                    if (instrument_type, symbol) in oos_job_set:
                        observation_id = self.recorder.record_market_row(
                            kind="intraday_15m", instrument_type=instrument_type,
                            symbol=symbol, row=row,
                        )
                        if observation_id:
                            captured += 1
                        else:
                            failures.append(f"intraday_{symbol}_not_recorded")
                    if instrument_type == "stock" and symbol in monitor_symbols:
                        monitor_id = record_pool_monitor_row(
                            self.recorder.store,
                            pool_version=monitor_pool_version,
                            symbol=symbol,
                            row=row,
                        )
                        if monitor_id:
                            monitor_captured += 1
                        else:
                            monitor_failures.append(
                                f"intraday_{symbol}_not_recorded"
                            )
                except Exception as exc:  # noqa: BLE001
                    if (instrument_type, symbol) in oos_job_set:
                        failure_kind = (
                            "daily" if daily_recovery_only else "intraday"
                        )
                        failures.append(
                            f"{failure_kind}_{symbol}:{type(exc).__name__}"
                        )
                    if instrument_type == "stock" and symbol in monitor_symbols:
                        monitor_failures.append(
                            f"intraday_{symbol}:{type(exc).__name__}"
                        )

        if expected.time() == time(15, 0):
            day = expected.strftime("%Y%m%d")
            for instrument_type, symbol in oos_jobs:
                try:
                    rows = (
                        self.index_daily_loader(symbol, start_date=day, end_date=day)
                        if instrument_type == "index"
                        else stock_daily_rows.get(symbol)
                        or self.daily_loader(symbol, n=3, adjust="none")
                    )
                    row = next(
                        (item for item in reversed(rows) if _time(item.get("time")).date() == expected.date()),
                        None,
                    )
                    if row is None:
                        failures.append(f"daily_{symbol}_expected_bar_missing")
                        continue
                    row = {**row, "time": expected}
                    if instrument_type == "stock":
                        profile = stock_profiles.get(symbol) or self._profile_for(
                            symbol, expected
                        )
                        row.update(
                            _daily_stock_metadata(
                                symbol, row, rows, profile, expected
                            )
                        )
                    row["observed_at"] = self.clock().astimezone()
                    observation_id = self.recorder.record_market_row(
                        kind="daily_bars", instrument_type=instrument_type,
                        symbol=symbol, row=row,
                    )
                    if observation_id:
                        captured += 1
                    else:
                        failures.append(f"daily_{symbol}_not_recorded")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"daily_{symbol}:{type(exc).__name__}")
        required = len(oos_jobs) * (
            1 if daily_recovery_only or expected.time() != time(15, 0) else 2
        )
        complete = not failures and captured == required
        monitor_required = 0 if daily_recovery_only else len(monitor_symbols)
        monitor_complete = (
            monitor_required == 0
            or (not monitor_failures and monitor_captured == monitor_required)
        )
        return {
            "status": "complete" if complete else "data_incomplete",
            "validation_epoch_id": epoch.epoch_id,
            "validation_pool_version": epoch.pool_version,
            "bar_closed_at": expected.isoformat(),
            "daily_recovery_only": daily_recovery_only,
            "captured": captured,
            "required": required,
            "failures": failures,
            "monitor_status": (
                "not_requested"
                if monitor_required == 0
                else "complete" if monitor_complete else "data_incomplete"
            ),
            "monitor_pool_version": monitor_pool_version,
            "monitor_captured": monitor_captured,
            "monitor_required": monitor_required,
            "monitor_failures": monitor_failures,
        }


def _intraday_stock_metadata(
    symbol: str,
    row: dict[str, Any],
    daily_rows: list[dict[str, Any]],
    profile: dict[str, Any],
    expected: datetime,
) -> dict[str, bool]:
    previous = _latest_daily_before(daily_rows, expected.date())
    if previous is None:
        raise ValueError("previous_daily_close_missing")
    member = cloud_ai_member(symbol)
    name = str(profile.get("name") or (member.name if member is not None else ""))
    if not name:
        raise ValueError("stock_name_missing")
    upper, lower = _price_limits(symbol, name, previous)
    close = float(row.get("close") or 0)
    return {
        "suspended": False,
        # Treat a close at either daily limit as locked.  This is deliberately
        # conservative because a completed 15-minute bar cannot prove that a
        # queue was executable at its close.
        "limit_locked": (
            abs(close - upper) <= 0.015 or abs(close - lower) <= 0.015
        ),
    }


def _daily_stock_metadata(
    symbol: str,
    current: dict[str, Any],
    daily_rows: list[dict[str, Any]],
    profile: dict[str, Any] | None,
    expected: datetime,
) -> dict[str, Any]:
    if not profile:
        raise ValueError("listing_profile_missing")
    listing_date = _listing_date(profile.get("listing_date"))
    if listing_date is None or listing_date > expected.date():
        raise ValueError("listing_date_missing_or_invalid")
    previous = _latest_daily_before(daily_rows, expected.date())
    if previous is None:
        raise ValueError("previous_daily_close_missing")
    name = str(profile.get("name") or "")
    if not name:
        raise ValueError("stock_name_missing")
    upper, lower = _price_limits(symbol, name, previous)
    prices = [
        float(current.get(key) or 0)
        for key in ("open", "high", "low", "close")
    ]
    locked = all(abs(value - upper) <= 0.015 for value in prices) or all(
        abs(value - lower) <= 0.015 for value in prices
    )
    return {
        "suspended": False,
        "limit_locked": locked,
        "is_st": "ST" in name.upper(),
        "delisting": "退" in name,
        "listed_days": (expected.date() - listing_date).days,
        "industry": str(
            CLOUD_AI_RISK_THEME
            if cloud_ai_member(symbol) is not None
            else profile.get("industry") or "用户关注"
        ),
    }


def _latest_daily_before(
    rows: list[dict[str, Any]], day: Any
) -> dict[str, Any] | None:
    candidates = [row for row in rows if _time(row.get("time")).date() < day]
    return max(candidates, key=lambda row: _time(row.get("time")), default=None)


def _price_limits(
    symbol: str, name: str, previous: dict[str, Any]
) -> tuple[float, float]:
    from pa_agent.data.ashare_limits import limit_pct, limit_prices

    previous_close = float(previous.get("close") or 0)
    if previous_close <= 0:
        raise ValueError("previous_daily_close_invalid")
    return limit_prices(previous_close, limit_pct(symbol, name))


def _listing_date(value: Any) -> Any:
    text = str(value or "").strip().replace("-", "")[:8]
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None
