"""Fail-closed TongHuaShun desktop adapter for the local Windows client.

The adapter only uses window/control handles and copied table text.  It never
stores credentials and never invokes a submit, confirm, cancel, or sell button.
"""
# ruff: noqa: RUF001
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any, Protocol

from pa_agent.trading.broker_models import (
    AuthorizedOrder,
    BrokerCashFlow,
    BrokerConnectionStatus,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
    ConnectionState,
    PrefillReceipt,
    ReconciliationResult,
    ThsBinding,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def account_fingerprint(
    *, install_path: str, client_version: str, broker_name: str, masked_account: str
) -> str:
    canonical = "|".join(
        item.strip().casefold()
        for item in (install_path, client_version, broker_name, masked_account)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mask_account(value: str) -> str:
    """Discard a raw account value immediately and keep only its last four digits."""
    digits = "".join(character for character in str(value) if character.isdigit())
    return f"****{digits[-4:]}" if len(digits) >= 4 else ""


def _trading_window_score(title: str, *, visible: bool) -> int:
    """Prefer the real trading shell over transient order/editor windows."""
    main_title = any(
        marker in title
        for marker in ("网上股票交易系统", "网上交易系统", "股票交易系统")
    )
    trading_title = "交易" in title or "委托" in title
    return int(visible) + 2 * int(trading_title) + 20 * int(main_title)


def _account_identity_from_controls(
    root_title: str,
    controls: list[dict[str, Any]],
    *,
    modal_texts: list[str],
) -> dict[str, Any]:
    """Derive identity from the target trading-window subtree without coordinates."""
    texts = [root_title, *(str(item.get("text") or "") for item in controls)]
    joined = "\n".join(texts)
    modal_joined = "\n".join(modal_texts)
    blocked = any(
        marker in modal_joined
        for marker in ("验证码", "错误提示", "连接失败", "账户锁定", "异常交易核查")
    )
    has_exit = any(item.get("text") == "退出" for item in controls)
    has_asset_view = any(
        marker in joined for marker in ("总资产", "可用金额", "股票市值", "持仓")
    )
    has_position_grid = any(
        item.get("class_name") == "CVirtualGridCtrl" for item in controls
    )
    login_required = not (has_exit and has_asset_view and has_position_grid)

    account = ""
    account_labels = {
        item["hwnd"]: item
        for item in controls
        if item.get("class_name") == "Static"
        and str(item.get("text") or "").replace(" ", "")
        in {"资金账号", "资金帐户", "资金账户", "客户号", "账号"}
    }
    for label in account_labels.values():
        same_parent = [
            item for item in controls
            if item.get("parent") == label.get("parent")
            and item.get("class_name") == "ComboBox"
            and item.get("selected_text")
        ]
        numeric = [
            item for item in same_parent
            if len("".join(c for c in str(item["selected_text"]) if c.isdigit())) >= 6
        ]
        selected = numeric[0] if len(numeric) == 1 else None
        label_rect = tuple(label.get("rect") or ())
        if len(numeric) > 1 and len(label_rect) == 4:
            right, center_y = label_rect[2], (label_rect[1] + label_rect[3]) / 2
            candidates: list[tuple[float, dict[str, Any]]] = []
            for item in numeric:
                rect = tuple(item.get("rect") or ())
                if len(rect) != 4 or rect[0] < right:
                    continue
                item_center_y = (rect[1] + rect[3]) / 2
                vertical = abs(item_center_y - center_y)
                if vertical <= max(8, (label_rect[3] - label_rect[1]) / 2):
                    candidates.append((rect[0] - right + vertical * 10, item))
            candidates.sort(key=lambda item: item[0])
            if candidates and (
                len(candidates) == 1 or candidates[0][0] < candidates[1][0]
            ):
                selected = candidates[0][1]
        if selected is not None:
            account = _mask_account(str(selected["selected_text"]))
            break

    broker_candidates: list[str] = []
    for text in texts:
        match = re.search(r"([^\s|｜]{2,20}(?:证券|券商))", text)
        if match:
            candidate = match.group(1)
            if candidate not in {"网上证券", "证券代码", "证券名称"}:
                broker_candidates.append(candidate)
    broker = next(
        (
            candidate for candidate in broker_candidates
            if not re.search(r"[XxＸｘ*＊]{2,}", candidate)
        ),
        "",
    )
    return {
        "login_required": login_required,
        "blocked_by_modal": blocked,
        "detected_broker_name": broker,
        "detected_masked_account": account,
    }


def _funds_text_from_controls(controls: list[dict[str, Any]]) -> str:
    """Pair read-only fund labels and values inside the same UI container."""
    aliases = {
        "total_equity": ("总资产", "资产总值"),
        "available_cash": ("可用资金", "可用金额"),
        "position_value": ("股票市值", "证券市值", "持仓市值"),
        "daily_pnl": ("当日盈亏", "今日盈亏"),
    }
    canonical = {
        "total_equity": "总资产",
        "available_cash": "可用资金",
        "position_value": "股票市值",
        "daily_pnl": "当日盈亏",
    }
    statics = [
        (index, item)
        for index, item in enumerate(controls)
        if item.get("class_name") == "Static"
    ]
    parent_labels: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for _index, item in statics:
        normalized = str(item.get("text") or "").replace(" ", "")
        for key, names in aliases.items():
            if normalized in names:
                parent = int(item.get("parent") or 0)
                parent_labels.setdefault(parent, {}).setdefault(key, []).append(item)

    def parent_score(item: tuple[int, dict[str, list[dict[str, Any]]]]) -> tuple[int, int]:
        _parent, labels_by_key = item
        coverage = len(labels_by_key)
        x_spread = 1_000_000
        if coverage:
            choices = [
                [tuple(label.get("rect") or (0, 0, 0, 0))[0] for label in labels]
                for labels in labels_by_key.values()
            ]
            x_spread = min(max(values) - min(values) for values in product(*choices))
        return (-coverage, x_spread)

    selected_parent = min(parent_labels.items(), key=parent_score)[0] if parent_labels else 0
    output: list[str] = []
    for key, names in aliases.items():
        labels = [
            (index, item) for index, item in statics
            if item.get("parent") == selected_parent
            if str(item.get("text") or "").replace(" ", "") in names
        ]
        matches: list[tuple[int, str]] = []
        for label_index, label in labels:
            lrect = tuple(label.get("rect") or (0, 0, 0, 0))
            lx, ly = lrect[2], (lrect[1] + lrect[3]) // 2
            for value_index, value in statics:
                if value.get("parent") != label.get("parent"):
                    continue
                number = _number(str(value.get("text") or ""))
                if number is None:
                    continue
                vrect = tuple(value.get("rect") or (0, 0, 0, 0))
                vx, vy = vrect[0], (vrect[1] + vrect[3]) // 2
                if vx < lx or abs(vy - ly) > 5:
                    continue
                distance = (
                    100 * (abs(vx - lx) + 10 * abs(vy - ly))
                    + abs(value_index - label_index)
                )
                matches.append((distance, str(value["text"])))
        matches.sort()
        nearest_by_value: dict[str, int] = {}
        for distance, value in matches:
            nearest_by_value[value] = min(distance, nearest_by_value.get(value, distance))
        unique_matches = sorted(
            (distance, value) for value, distance in nearest_by_value.items()
        )
        if unique_matches and (
            len(unique_matches) == 1 or unique_matches[0][0] < unique_matches[1][0]
        ):
            output.append(f"{canonical[key]}: {unique_matches[0][1]}")
    return "\n".join(output)


class ThsUiBackend(Protocol):
    def launch_clients(self, install_path: str = "") -> list[str]: ...

    def discover(self) -> dict[str, Any]: ...
    def read_snapshot_tables(self) -> dict[str, str]: ...
    def visible_texts(self) -> list[str]: ...
    def prefill_fields(self, order: AuthorizedOrder) -> dict[str, Any]: ...


class ThsBrokerAdapter:
    """Small broker interface backed by a replaceable Windows UI adapter."""

    def __init__(
        self,
        binding: ThsBinding | None = None,
        *,
        backend: ThsUiBackend | None = None,
    ) -> None:
        self.binding = binding or ThsBinding()
        self.backend = backend or Win32ThsBackend(
            self.binding.market_executable, self.binding.trading_executable
        )
        self._connection = ConnectionState(
            status=BrokerConnectionStatus.DISCONNECTED,
            message="尚未检测同花顺",
            checked_at=_now(),
        )
        self._last_snapshot: BrokerSnapshot | None = None

    @property
    def connection(self) -> ConnectionState:
        return self._connection

    def connect(self, binding: ThsBinding | None = None) -> ConnectionState:
        if binding is not None:
            self.binding = binding
        try:
            found = self.backend.discover()
        except Exception as exc:
            logger.exception("同花顺客户端检测失败")
            self._connection = ConnectionState(
                status=BrokerConnectionStatus.ERROR,
                message=str(exc), checked_at=_now(),
            )
            return self._connection
        market_pid = found.get("market_pid")
        trading_pid = found.get("trading_pid")
        detected_broker = str(found.get("detected_broker_name") or "").strip()
        detected_account = str(found.get("detected_masked_account") or "").strip()
        if not market_pid:
            status = BrokerConnectionStatus.DISCONNECTED
            message = "未检测到同花顺远航版 happ.exe"
        elif not trading_pid:
            status = BrokerConnectionStatus.LOGIN_REQUIRED
            message = "已检测行情客户端，但交易客户端 xiadan.exe 未运行或尚未登录"
        elif found.get("blocked_by_modal"):
            status = BrokerConnectionStatus.BLOCKED_BY_MODAL
            message = "同花顺交易端存在验证码、错误或其他阻断窗口"
        elif found.get("login_required") or not (detected_broker and detected_account):
            status = BrokerConnectionStatus.LOGIN_REQUIRED
            message = "交易端尚未完成登录，或无法回读券商与脱敏资金账号"
        else:
            status = BrokerConnectionStatus.CONNECTED_READ_ONLY
            message = "已连接同花顺；账户快照通过后才允许风控授权"
        install_path = str(found.get("install_path") or "")
        version = str(found.get("client_version") or "")
        detected_fingerprint = account_fingerprint(
            install_path=install_path,
            client_version=version,
            broker_name=detected_broker,
            masked_account=detected_account,
        )
        if (
            status is BrokerConnectionStatus.CONNECTED_READ_ONLY
            and self.binding.confirmed
        ):
            if self.binding.account_fingerprint != detected_fingerprint:
                status = BrokerConnectionStatus.ACCOUNT_MISMATCH
                message = "当前同花顺客户端与已确认账户指纹不一致"
            elif self.binding.allow_prefill and not self.binding.read_only:
                status = BrokerConnectionStatus.CONNECTED
        self._connection = ConnectionState(
            status=status,
            message=message,
            market_pid=market_pid,
            trading_pid=trading_pid,
            market_window=found.get("market_window"),
            trading_window=found.get("trading_window"),
            detected_install_path=install_path,
            client_version=version,
            account_fingerprint=detected_fingerprint,
            detected_broker_name=detected_broker,
            detected_masked_account=detected_account,
            checked_at=_now(),
        )
        return self._connection

    def launch_clients(self, *, install_path: str = "") -> ConnectionState:
        """Start the two configured clients without touching their login UI."""
        launcher = getattr(self.backend, "launch_clients", None)
        if launcher is None:
            raise RuntimeError("当前同花顺适配器不支持安全启动客户端")
        selected = (install_path or self.binding.install_path or "").strip()
        launched = launcher(selected)
        logger.info("同花顺客户端安全启动: %s", launched or "已在运行")
        return self.connect()

    def confirmed_binding(self, *, broker_name: str, masked_account: str) -> ThsBinding:
        """Return a confirmed binding without persisting credentials."""
        state = self.connect()
        if not state.market_pid or not state.trading_pid:
            raise RuntimeError("同花顺行情与交易客户端必须同时运行并完成登录")
        if not state.detected_broker_name or not state.detected_masked_account:
            raise RuntimeError("必须先从同花顺交易端回读券商和脱敏资金账号")
        if broker_name.strip() != state.detected_broker_name:
            raise RuntimeError("填写的券商名称与同花顺回读结果不一致")
        if masked_account.strip() != state.detected_masked_account:
            raise RuntimeError("填写的脱敏资金账号与同花顺回读结果不一致")
        install_path = state.detected_install_path
        fingerprint = account_fingerprint(
            install_path=install_path,
            client_version=state.client_version,
            broker_name=state.detected_broker_name,
            masked_account=state.detected_masked_account,
        )
        return self.binding.model_copy(update={
            "enabled": True,
            "install_path": install_path,
            "client_version": state.client_version,
            "broker_name": state.detected_broker_name,
            "masked_account": state.detected_masked_account,
            "account_fingerprint": fingerprint,
            "confirmed": True,
        })

    def snapshot(self, *, read_current_cash_flow_page: bool = False) -> BrokerSnapshot:
        state = self.connect()
        warnings: list[str] = []
        captured_at = _now()
        captured_dt = datetime.fromisoformat(captured_at)
        default_range_start = captured_dt.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        if not state.usable:
            return BrokerSnapshot(
                connection=state,
                account_fingerprint=state.account_fingerprint,
                captured_at=captured_at,
                complete=False,
                warnings=[state.message],
            )
        try:
            tables = self.backend.read_snapshot_tables()
            if read_current_cash_flow_page:
                reader = getattr(self.backend, "read_current_cash_flow_page", None)
                if reader is None:
                    warnings.append("cash_flow_page_reader_unavailable")
                else:
                    tables.update(reader(captured_at=captured_at))
        except Exception as exc:
            logger.warning("同花顺只读快照失败: %s", exc)
            tables = {}
            warnings.append(f"snapshot_read_error:{exc}")
        positions_text = tables.get("positions", "")
        current_grid = tables.get("current_grid", "")
        if not positions_text and _is_position_grid(current_grid):
            positions_text = current_grid
        positions = _parse_positions(positions_text, warnings)
        orders = _parse_orders(tables.get("orders", ""), warnings)
        fills = _parse_fills(tables.get("fills", ""), warnings)
        cash_flow_text = tables.get("cash_flows", "")
        cash_flows = _parse_cash_flows(cash_flow_text, warnings)
        cash_flow_range_start = str(
            tables.get("cash_flow_range_start") or default_range_start
        )
        cash_flow_range_end = str(tables.get("cash_flow_range_end") or captured_at)
        cash_flow_complete = bool(tables.get("cash_flows_complete", False)) and bool(
            cash_flow_text
        )
        if any(
            item in warnings
            for item in ("cash_flow_row_incomplete", "cash_flow_table_present_but_unparseable")
        ):
            cash_flow_complete = False
        if not cash_flow_complete:
            warnings.append("cash_flow_history_not_verified")
        funds = _parse_funds(tables.get("funds", ""), warnings)
        quote = _parse_quote(tables.get("quote", ""), warnings)
        complete = all(
            key in funds and funds[key] is not None
            for key in ("total_equity", "available_cash", "position_value")
        ) and bool(self.binding.confirmed)
        if not complete:
            warnings.append("账户资金或账户绑定尚未完成，实盘授权保持关闭")
        snapshot = BrokerSnapshot(
            connection=state,
            account_fingerprint=self.binding.account_fingerprint or state.account_fingerprint,
            total_equity=funds.get("total_equity"),
            available_cash=funds.get("available_cash"),
            position_value=funds.get("position_value"),
            daily_pnl=funds.get("daily_pnl"),
            positions=positions,
            orders=orders,
            fills=fills,
            cash_flows=cash_flows,
            cash_flow_complete=cash_flow_complete,
            cash_flow_range_start=cash_flow_range_start,
            cash_flow_range_end=cash_flow_range_end,
            quote=quote,
            captured_at=captured_at,
            complete=complete,
            warnings=list(dict.fromkeys(warnings)),
        )
        self._last_snapshot = snapshot
        return snapshot

    def prefill(self, order: AuthorizedOrder) -> PrefillReceipt:
        state = self.connect()
        if state.status is not BrokerConnectionStatus.CONNECTED:
            return PrefillReceipt(
                status="blocked", message=f"同花顺状态不允许预填：{state.status.value}",
                created_at=_now(), final_confirmation_clicked=False,
            )
        if order.account_fingerprint != self.binding.account_fingerprint:
            return PrefillReceipt(
                status="blocked", message="订单账户指纹与绑定账户不一致",
                created_at=_now(), final_confirmation_clicked=False,
            )
        if not order.name:
            return PrefillReceipt(
                status="blocked", message="缺少证券名称，无法完成代码-名称双重校验",
                created_at=_now(), final_confirmation_clicked=False,
            )
        try:
            result = self.backend.prefill_fields(order)
        except Exception as exc:
            logger.exception("同花顺委托预填失败")
            return PrefillReceipt(
                status="failed", message=str(exc), created_at=_now(),
                final_confirmation_clicked=False,
            )
        verified = result.get("verified_fields") or {}
        expected = {
            "symbol": order.symbol,
            "direction": order.direction,
            "price": order.price,
            "quantity": order.quantity,
            "name": order.name,
        }
        if any(str(verified.get(key, "")) != str(value) for key, value in expected.items()):
            return PrefillReceipt(
                status="failed", message="预填后回读字段不一致，输入内容已清空",
                verified_fields=verified, created_at=_now(), final_confirmation_clicked=False,
            )
        return PrefillReceipt(
            status="awaiting_user_confirmation",
            message="委托字段已回读校验，请在同花顺中人工确认",
            verified_fields=verified,
            created_at=_now(),
            final_confirmation_clicked=False,
        )

    def reconcile(
        self,
        order: AuthorizedOrder,
        snapshot: BrokerSnapshot | None = None,
        *,
        time_window_seconds: int = 60,
    ) -> ReconciliationResult:
        current = snapshot or self.snapshot()
        candidates = [
            item for item in current.orders
            if _order_matches(item, order, time_window_seconds=time_window_seconds)
        ]
        if len(candidates) != 1:
            return ReconciliationResult(
                status="reconciliation_required",
                plan_id=order.plan_id,
                candidates=[item.broker_order_id for item in candidates],
                message="没有唯一匹配的同花顺委托，需要用户人工关联",
            )
        broker_order = candidates[0]
        fills = [
            fill for fill in current.fills
            if fill.broker_order_id and fill.broker_order_id == broker_order.broker_order_id
        ]
        return ReconciliationResult(
            status="matched",
            plan_id=order.plan_id,
            matched_order_ids=[broker_order.broker_order_id],
            matched_fill_ids=[fill.broker_fill_id for fill in fills],
            message="已唯一匹配同花顺委托和成交",
        )


class Win32ThsBackend:
    """Native Win32 implementation.  Importable on non-Windows for tests."""

    def __init__(self, market_executable: str, trading_executable: str) -> None:
        self.market_executable = market_executable.casefold()
        self.trading_executable = trading_executable.casefold()
        self._trading_window: int | None = None

    def discover(self) -> dict[str, Any]:
        if os.name != "nt":
            return {}
        import win32api
        import win32con
        import win32gui
        import win32process

        found: dict[str, Any] = {}
        process_paths: dict[int, str] = {}

        # Discover the processes independently of their top-level windows.
        # During startup/login the market client may not yet expose a normal
        # window, but its PID and verified executable path are already valid.
        for pid in win32process.EnumProcesses():
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                    False,
                    pid,
                )
                try:
                    path = win32process.GetModuleFileNameEx(handle, 0)
                finally:
                    handle.Close()
            except Exception:
                continue
            process_paths[pid] = path
            executable = Path(path).name.casefold()
            if executable == self.market_executable:
                found["market_pid"] = pid
                found["install_path"] = str(Path(path).parent.parent)
                found["client_version"] = _file_version(path)
            elif executable == self.trading_executable:
                found["trading_pid"] = pid

        def callback(hwnd: int, _extra: object) -> bool:
            if not win32gui.IsWindow(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = process_paths.get(pid)
            if path is None:
                try:
                    handle = win32api.OpenProcess(
                        win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                        False,
                        pid,
                    )
                    try:
                        path = win32process.GetModuleFileNameEx(handle, 0)
                    finally:
                        handle.Close()
                except Exception:
                    path = ""
                process_paths[pid] = path
            executable = Path(path).name.casefold() if path else ""
            title = win32gui.GetWindowText(hwnd)
            visible = bool(win32gui.IsWindowVisible(hwnd))
            if executable == self.market_executable and (
                visible or not found.get("market_window")
            ):
                found["market_pid"] = pid
                found["market_window"] = hwnd
                found["install_path"] = str(Path(path).parent.parent)
                found["client_version"] = _file_version(path)
            current_score = int(found.get("trading_window_score") or -1)
            score = _trading_window_score(title, visible=visible)
            if executable == self.trading_executable and score > current_score:
                found["trading_pid"] = pid
                found["trading_window"] = hwnd
                found["trading_window_score"] = score
            return True

        win32gui.EnumWindows(callback, None)
        self._trading_window = found.get("trading_window")
        found.pop("trading_window_score", None)
        if self._trading_window:
            identity = self._detect_account_identity(self._trading_window)
            found.update(identity)
        return found

    def launch_clients(self, install_path: str = "") -> list[str]:
        """Launch only happ.exe and xiadan.exe from one verified install root."""
        if os.name != "nt":
            raise RuntimeError("同花顺客户端启动仅支持 Windows")
        root = self._resolve_install_root(install_path)
        market = (root / "bin" / self.market_executable).resolve()
        trading = (root / "transaction" / self.trading_executable).resolve()
        root_resolved = root.resolve()
        for executable in (market, trading):
            try:
                executable.relative_to(root_resolved)
            except ValueError as exc:
                raise RuntimeError("同花顺可执行文件超出所选安装目录") from exc
            if not executable.is_file():
                raise RuntimeError(f"未找到同花顺可执行文件: {executable}")

        found = self.discover()
        launched: list[str] = []
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        if not found.get("market_pid"):
            subprocess.Popen(  # noqa: S603 - fixed verified executable, no shell
                [str(market)], cwd=str(market.parent), shell=False,
                creationflags=flags,
            )
            launched.append(str(market))
        if not found.get("trading_pid"):
            subprocess.Popen(  # noqa: S603 - fixed verified executable, no shell
                [str(trading)], cwd=str(trading.parent), shell=False,
                creationflags=flags,
            )
            launched.append(str(trading))
        return launched

    @staticmethod
    def _resolve_install_root(install_path: str) -> Path:
        selected = Path(install_path).expanduser() if install_path else None
        candidates = [selected] if selected is not None else []
        candidates.extend([
            Path(r"D:\soft_common\同花顺\同花顺远航版"),
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "同花顺" / "同花顺远航版",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "同花顺" / "同花顺远航版",
        ])
        for candidate in candidates:
            if candidate is None:
                continue
            root = candidate.resolve()
            if (root / "bin" / "happ.exe").is_file() and (
                root / "transaction" / "xiadan.exe"
            ).is_file():
                return root
        if install_path:
            raise RuntimeError("所选目录不是有效的同花顺远航版安装目录")
        raise RuntimeError("未自动找到同花顺远航版，请先选择安装目录")

    @staticmethod
    def _detect_account_identity(root: int) -> dict[str, Any]:
        """Read only the target trading-window subtree; raw accounts never escape."""
        import win32con
        import win32gui

        controls: list[dict[str, Any]] = []

        def callback(hwnd: int, _extra: object) -> bool:
            class_name = win32gui.GetClassName(hwnd)
            selected_text = ""
            if class_name == "ComboBox":
                selected_text = Win32ThsBackend._selected_combo_text(hwnd, win32con)
            controls.append({
                "hwnd": hwnd,
                "parent": win32gui.GetParent(hwnd),
                "class_name": class_name,
                "text": win32gui.GetWindowText(hwnd).strip(),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "selected_text": selected_text,
                "rect": win32gui.GetWindowRect(hwnd),
            })
            return True

        win32gui.EnumChildWindows(root, callback, None)
        return _account_identity_from_controls(
            win32gui.GetWindowText(root).strip(),
            controls,
            modal_texts=Win32ThsBackend._visible_blocking_modal_texts(root),
        )

    @staticmethod
    def _selected_combo_text(hwnd: int, win32con: Any) -> str:
        """Read a ComboBox selection without opening it or changing its state."""
        import win32gui

        try:
            index = win32gui.SendMessage(hwnd, win32con.CB_GETCURSEL, 0, 0)
            if index < 0 or index == win32con.CB_ERR:
                return ""
            length = win32gui.SendMessage(hwnd, win32con.CB_GETLBTEXTLEN, index, 0)
            if length < 0 or length == win32con.CB_ERR:
                return ""
            buffer = win32gui.PyMakeBuffer((length + 1) * 2)
            win32gui.SendMessage(hwnd, win32con.CB_GETLBTEXT, index, buffer)
            return buffer[: length * 2].tobytes().decode("utf-16-le").strip()
        except Exception:
            return ""

    @staticmethod
    def _visible_blocking_modal_texts(root: int) -> list[str]:
        """Read visible modal text owned by the same trading process only."""
        import win32gui
        import win32process

        _, target_pid = win32process.GetWindowThreadProcessId(root)
        values: list[str] = []

        def top_callback(hwnd: int, _extra: object) -> bool:
            if hwnd == root or not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid != target_pid or win32gui.GetClassName(hwnd) != "#32770":
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if title:
                values.append(title)

            def child_callback(child: int, _child_extra: object) -> bool:
                text = win32gui.GetWindowText(child).strip()
                if text:
                    values.append(text)
                return True

            win32gui.EnumChildWindows(hwnd, child_callback, None)
            return True

        win32gui.EnumWindows(top_callback, None)
        return values

    def visible_texts(self) -> list[str]:
        import win32gui

        root = self._require_trading_window()
        values: list[str] = []

        def callback(hwnd: int, _extra: object) -> bool:
            value = win32gui.GetWindowText(hwnd).strip()
            if value:
                values.append(value)
            return True

        win32gui.EnumChildWindows(root, callback, None)
        return values

    def _control_snapshot(self) -> list[dict[str, Any]]:
        import win32gui

        root = self._require_trading_window()
        controls: list[dict[str, Any]] = []

        def callback(hwnd: int, _extra: object) -> bool:
            controls.append({
                "hwnd": hwnd,
                "parent": win32gui.GetParent(hwnd),
                "class_name": win32gui.GetClassName(hwnd),
                "text": win32gui.GetWindowText(hwnd).strip(),
                "rect": win32gui.GetWindowRect(hwnd),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
            })
            return True

        win32gui.EnumChildWindows(root, callback, None)
        return controls

    def read_snapshot_tables(self) -> dict[str, str]:
        """Read pages through their own copy command; failures remain incomplete."""
        tables: dict[str, str] = {}
        controls = self._control_snapshot()
        control_funds = _funds_text_from_controls(controls)
        if control_funds:
            tables["funds"] = control_funds
        texts = self.visible_texts()
        tables["quote"] = "\n".join(texts)
        if "funds" not in tables:
            tables["funds"] = ""
        return tables

    def read_current_cash_flow_page(self, *, captured_at: str) -> dict[str, Any]:
        """Copy only an already-open cash-flow result page; never navigate or query it."""
        root = self._require_trading_window()
        modal_texts = self._visible_blocking_modal_texts(root)
        if modal_texts:
            raise RuntimeError("同花顺存在模态窗口，禁止读取资金流水")
        controls = self._control_snapshot()
        visible_text = "\n".join(
            str(item.get("text") or "")
            for item in controls if item.get("visible")
        )
        page_markers = ("资金流水", "转账查询", "银证转账查询", "历史转账")
        if not any(marker in visible_text for marker in page_markers):
            raise RuntimeError("请先在同花顺打开资金流水查询结果页")
        grids = [
            item for item in controls
            if item.get("visible") and item.get("class_name") == "CVirtualGridCtrl"
        ]
        if len(grids) != 1:
            raise RuntimeError("未能唯一识别当前资金流水表格")
        text = self._copy_existing_grid(int(grids[0]["hwnd"]))
        if not _is_cash_flow_grid(text):
            raise RuntimeError("当前表格列名不是可识别的资金流水")
        start, end = _cash_flow_query_range(controls, captured_at=captured_at)
        if not start or not end:
            raise RuntimeError("未能回读资金流水查询起止日期")
        return {
            "cash_flows": text,
            "cash_flows_complete": True,
            "cash_flow_range_start": start,
            "cash_flow_range_end": end,
        }

    def _copy_existing_grid(self, grid: int) -> str:
        """Copy one verified result grid and restore the user's text clipboard."""
        import win32clipboard
        import win32con
        import win32gui

        previous: str | None = None
        try:
            win32clipboard.OpenClipboard()
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                previous = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
        except Exception:
            previous = None
        finally:
            with suppress(Exception):
                win32clipboard.CloseClipboard()
        root = self._require_trading_window()
        win32gui.SetForegroundWindow(root)
        win32gui.SetFocus(grid)
        self._press_ctrl_key(ord("A"))
        self._press_ctrl_key(ord("C"))
        time.sleep(0.15)
        try:
            win32clipboard.OpenClipboard()
            value = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)).strip()
        finally:
            with suppress(Exception):
                win32clipboard.CloseClipboard()
        if previous is not None:
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(previous, win32con.CF_UNICODETEXT)
            finally:
                with suppress(Exception):
                    win32clipboard.CloseClipboard()
        return value

    def prefill_fields(self, order: AuthorizedOrder) -> dict[str, Any]:
        import win32con
        import win32gui

        root = self._require_trading_window()
        win32gui.ShowWindow(root, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(root)
        self._press_function_key(1 if order.direction == "buy" else 2)
        time.sleep(0.5)
        mapping = self._labeled_edit_controls()
        required = {"symbol", "price", "quantity"}
        if not required.issubset(mapping):
            raise RuntimeError("未能按标签唯一识别代码、价格和数量输入框；拒绝预填")
        entered = {
            "symbol": order.symbol,
            "price": f"{order.price:g}",
            "quantity": str(order.quantity),
        }
        touched: list[int] = []
        try:
            for key, value in entered.items():
                hwnd = mapping[key]
                win32gui.SendMessage(hwnd, win32con.WM_SETTEXT, 0, value)
                touched.append(hwnd)
            time.sleep(0.4)
            readback = {
                key: win32gui.GetWindowText(mapping[key]).strip()
                for key in required
            }
            visible = self.visible_texts()
            name_ok = any(order.name == text or order.name in text for text in visible)
            verified = {
                "symbol": readback["symbol"],
                "direction": order.direction,
                "price": float(readback["price"]),
                "quantity": int(float(readback["quantity"])),
                "name": order.name if name_ok else "",
            }
            if not name_ok:
                raise RuntimeError("同花顺未回显匹配的证券名称")
            return {"verified_fields": verified}
        except Exception:
            for hwnd in touched:
                win32gui.SendMessage(hwnd, win32con.WM_SETTEXT, 0, "")
            raise

    def _require_trading_window(self) -> int:
        if not self._trading_window:
            self.discover()
        if not self._trading_window:
            raise RuntimeError("同花顺交易窗口不可用")
        return self._trading_window

    def _copy_named_grid(self, labels: tuple[str, ...]) -> str:
        import win32clipboard
        import win32con
        import win32gui

        root = self._require_trading_window()
        navigation: list[int] = []
        grids: list[tuple[int, int]] = []

        def callback(hwnd: int, _extra: object) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            text = win32gui.GetWindowText(hwnd).strip()
            class_name = win32gui.GetClassName(hwnd)
            if text in labels and class_name in {"Button", "Static"}:
                navigation.append(hwnd)
            if class_name == "CVirtualGridCtrl":
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                grids.append((max(0, right - left) * max(0, bottom - top), hwnd))
            return True

        win32gui.EnumChildWindows(root, callback, None)
        if not navigation or not grids:
            return ""
        nav = navigation[0]
        if win32gui.GetClassName(nav) != "Button":
            return ""  # Static labels are not invoked because that can hit an unknown action.
        win32gui.SendMessage(nav, win32con.BM_CLICK, 0, 0)
        time.sleep(0.25)
        grid = max(grids)[1]
        win32gui.SetForegroundWindow(root)
        win32gui.SetFocus(grid)
        self._press_ctrl_key(ord("A"))
        self._press_ctrl_key(ord("C"))
        time.sleep(0.15)
        try:
            win32clipboard.OpenClipboard()
            value = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:
            return ""
        finally:
            with suppress(Exception):
                win32clipboard.CloseClipboard()
        return str(value).strip()

    def _labeled_edit_controls(self) -> dict[str, int]:
        import win32gui

        root = self._require_trading_window()
        labels: list[tuple[str, tuple[int, int, int, int]]] = []
        edits: list[tuple[int, tuple[int, int, int, int]]] = []

        def callback(hwnd: int, _extra: object) -> bool:
            if not win32gui.IsWindowVisible(hwnd) or not win32gui.IsWindowEnabled(hwnd):
                return True
            rect = win32gui.GetWindowRect(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            text = win32gui.GetWindowText(hwnd).strip()
            if class_name in {"Static", "Button"} and text:
                labels.append((text, rect))
            elif class_name in {"Edit", "RICHEDIT"}:
                edits.append((hwnd, rect))
            return True

        win32gui.EnumChildWindows(root, callback, None)
        aliases = {
            "symbol": ("证券代码", "股票代码", "代码"),
            "price": ("委托价格", "买入价格", "卖出价格", "价格"),
            "quantity": ("委托数量", "买入数量", "卖出数量", "数量"),
        }
        mapping: dict[str, int] = {}
        used: set[int] = set()
        for role, names in aliases.items():
            role_labels = [rect for text, rect in labels if any(name in text for name in names)]
            candidates: list[tuple[int, int]] = []
            for label in role_labels:
                lx, ly = label[2], (label[1] + label[3]) // 2
                for hwnd, rect in edits:
                    if hwnd in used:
                        continue
                    ex, ey = rect[0], (rect[1] + rect[3]) // 2
                    distance = abs(ex - lx) + 3 * abs(ey - ly)
                    if abs(ey - ly) <= 40:
                        candidates.append((distance, hwnd))
            candidates.sort()
            if candidates and (len(candidates) == 1 or candidates[0][0] < candidates[1][0]):
                mapping[role] = candidates[0][1]
                used.add(candidates[0][1])
        return mapping

    @staticmethod
    def _press_ctrl_key(key: int) -> None:
        import win32api
        import win32con

        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

    @staticmethod
    def _press_function_key(number: int) -> None:
        import win32api
        import win32con

        key = win32con.VK_F1 + number - 1
        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)


def _file_version(path: str) -> str:
    if not path or os.name != "nt":
        return ""
    try:
        import win32api

        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return "unknown"


def _rows(text: str) -> list[dict[str, str]]:
    if not text.strip():
        return []
    lines = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    delimiter = "\t" if "\t" in lines[0] else ","
    try:
        return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error:
        return []


def _value(row: dict[str, str], *aliases: str) -> str:
    normalized = {str(key).replace(" ", "").strip(): str(value).strip() for key, value in row.items()}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return ""


def _number(value: str) -> float | None:
    value = value.replace(",", "").replace("¥", "").replace("￥", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def _parse_positions(text: str, warnings: list[str]) -> list[BrokerPosition]:
    result: list[BrokerPosition] = []
    for row in _rows(text):
        symbol = _value(row, "证券代码", "股票代码", "代码")
        qty = _number(_value(row, "股票余额", "证券数量", "持仓数量", "数量"))
        sellable = _number(_value(row, "可用余额", "可卖数量", "可用数量"))
        cost = _number(_value(row, "成本价", "成本价格"))
        last = _number(_value(row, "市价", "当前价", "最新价"))
        market = _number(_value(row, "市值", "证券市值"))
        if symbol and None not in (qty, sellable, cost, last, market):
            result.append(BrokerPosition(
                symbol=symbol.zfill(6), name=_value(row, "证券名称", "股票名称", "名称"),
                quantity=int(qty), sellable_quantity=int(sellable), cost_price=cost,
                last_price=last, market_value=market,
            ))
    if text and not result:
        warnings.append("持仓表存在但无法按已知列名解析")
    return result


def _is_position_grid(text: str) -> bool:
    if not text:
        return False
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    headers = {item.strip() for item in first_line.split("\t")}
    return (
        bool(headers & {"证券代码", "股票代码", "代码"})
        and bool(headers & {"股票余额", "持仓数量"})
        and bool(headers & {"可用余额", "可卖数量", "可用数量"})
        and bool(headers & {"成本价", "成本价格"})
        and bool(headers & {"市价", "当前价", "最新价"})
        and bool(headers & {"市值", "证券市值"})
    )


def _is_cash_flow_grid(text: str) -> bool:
    if not text:
        return False
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    headers = {item.replace(" ", "").strip() for item in first_line.split("\t")}
    return (
        bool(headers & {"业务名称", "业务类型", "发生类型", "资金方向"})
        and bool(headers & {"发生金额", "转账金额", "金额"})
        and bool(headers & {"发生时间", "日期", "时间"})
    )


def _cash_flow_query_range(
    controls: list[dict[str, Any]], *, captured_at: str
) -> tuple[str, str]:
    """Read an explicit current-month date range from visible, labeled controls."""
    visible_text = "\n".join(
        str(item.get("text") or "") for item in controls if item.get("visible")
    )
    matches = re.findall(
        r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)",
        visible_text,
    )
    dates = sorted({datetime(int(y), int(m), int(d)).date() for y, m, d in matches})
    if len(dates) < 2:
        return "", ""
    captured = datetime.fromisoformat(captured_at)
    start_date, end_date = dates[0], dates[-1]
    if start_date != captured.date().replace(day=1) or end_date < captured.date():
        return "", ""
    start = datetime.combine(
        start_date, datetime.min.time(), tzinfo=captured.tzinfo
    ).isoformat()
    end = min(
        captured,
        datetime.combine(end_date, datetime.max.time(), tzinfo=captured.tzinfo),
    ).isoformat()
    return start, end


def _parse_orders(text: str, warnings: list[str]) -> list[BrokerOrder]:
    result: list[BrokerOrder] = []
    for row in _rows(text):
        symbol = _value(row, "证券代码", "股票代码", "代码")
        price = _number(_value(row, "委托价格", "价格"))
        qty = _number(_value(row, "委托数量", "数量"))
        filled = _number(_value(row, "成交数量", "已成数量")) or 0
        if symbol and price and qty:
            result.append(BrokerOrder(
                broker_order_id=_value(row, "合同编号", "委托编号", "合同序号"),
                symbol=symbol.zfill(6), direction=_normalize_direction(_value(row, "买卖标志", "操作", "方向")),
                price=price, quantity=int(qty), filled_quantity=int(filled),
                status=_value(row, "备注", "委托状态", "状态"),
                submitted_at=_value(row, "委托时间", "时间") or _now(),
            ))
    if text and not result:
        warnings.append("委托表存在但无法按已知列名解析")
    return result


def _parse_fills(text: str, warnings: list[str]) -> list[BrokerFill]:
    result: list[BrokerFill] = []
    for row in _rows(text):
        symbol = _value(row, "证券代码", "股票代码", "代码")
        price = _number(_value(row, "成交价格", "成交均价", "价格"))
        qty = _number(_value(row, "成交数量", "数量"))
        if symbol and price and qty:
            result.append(BrokerFill(
                broker_fill_id=_value(row, "成交编号", "成交序号"),
                broker_order_id=_value(row, "合同编号", "委托编号", "合同序号"),
                symbol=symbol.zfill(6), direction=_normalize_direction(_value(row, "买卖标志", "操作", "方向")),
                price=price, quantity=int(qty),
                fees=_number(_value(row, "费用", "手续费")) or 0,
                filled_at=_value(row, "成交时间", "时间") or _now(),
            ))
    if text and not result:
        warnings.append("成交表存在但无法按已知列名解析")
    return result


def _parse_funds(text: str, warnings: list[str]) -> dict[str, float | None]:
    aliases = {
        "total_equity": ("总资产", "资产总值"),
        "available_cash": ("可用资金", "可用金额"),
        "position_value": ("股票市值", "证券市值", "持仓市值"),
        "daily_pnl": ("当日盈亏", "今日盈亏"),
    }
    result: dict[str, float | None] = {key: None for key in aliases}
    rows = _rows(text)
    for row in rows:
        for key, names in aliases.items():
            value = _number(_value(row, *names))
            if value is not None:
                result[key] = value
    for key, names in aliases.items():
        if result[key] is not None:
            continue
        for name in names:
            match = re.search(rf"{name}\s*[:：]?\s*([\d,.-]+)", text)
            if match:
                result[key] = _number(match.group(1))
                break
    if text and not any(value is not None for value in result.values()):
        warnings.append("资金信息存在但无法按已知标签解析")
    return result


def _parse_cash_flows(text: str, warnings: list[str]) -> list[BrokerCashFlow]:
    result: list[BrokerCashFlow] = []
    for row in _rows(text):
        direction_raw = _value(row, "业务名称", "业务类型", "发生类型", "资金方向")
        normalized = direction_raw.replace(" ", "")
        direction = (
            "deposit" if any(value in normalized for value in ("银转证", "入金", "转入"))
            else "withdrawal" if any(value in normalized for value in ("证转银", "出金", "转出"))
            else ""
        )
        amount = _number(_value(row, "发生金额", "转账金额", "金额"))
        occurred_at = _value(row, "发生时间", "委托时间", "时间", "日期")
        status = _value(row, "处理状态", "状态") or "confirmed"
        flow_id = _value(row, "流水号", "业务流水号", "银行流水号")
        if direction and amount and amount > 0 and occurred_at:
            result.append(BrokerCashFlow(
                broker_flow_id=flow_id,
                direction=direction,
                amount=amount,
                occurred_at=occurred_at,
                status=status,
                description=direction_raw,
            ))
        elif direction or amount or occurred_at:
            warnings.append("cash_flow_row_incomplete")
    if text and not result and "无记录" not in text and "暂无数据" not in text:
        warnings.append("cash_flow_table_present_but_unparseable")
    return result


def _parse_quote(text: str, warnings: list[str]) -> BrokerQuote | None:
    symbol_match = re.search(r"(?<!\d)([036]\d{5})(?!\d)", text)
    price_match = re.search(r"(?:最新价|现价)\s*[:：]?\s*(\d+(?:\.\d+)?)", text)
    if not symbol_match or not price_match:
        return None
    return BrokerQuote(
        symbol=symbol_match.group(1), last_price=float(price_match.group(1)), captured_at=_now()
    )


def _normalize_direction(value: str) -> str:
    value = value.strip().casefold()
    return "buy" if "买" in value or value == "buy" else "sell" if "卖" in value or value == "sell" else value


def _order_matches(item: BrokerOrder, order: AuthorizedOrder, *, time_window_seconds: int) -> bool:
    if (
        item.symbol != order.symbol
        or item.direction != order.direction
        or item.quantity != order.quantity
        or abs(item.price - order.price) > 1e-8
    ):
        return False
    try:
        left = datetime.fromisoformat(item.submitted_at)
        right = datetime.fromisoformat(order.authorized_at)
        return abs((left - right).total_seconds()) <= time_window_seconds
    except (TypeError, ValueError):
        return True
