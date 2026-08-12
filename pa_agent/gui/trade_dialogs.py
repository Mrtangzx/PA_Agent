"""Human confirmation dialogs for real executions and instrument parameters."""
from __future__ import annotations

import uuid
from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from pa_agent.trading.models import AssetClass, Execution, InstrumentProfile
from pa_agent.trading.profiles import is_continuous_futures_symbol


class ExecutionDialog(QDialog):
    def __init__(self, plan: dict, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("确认真实成交")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.time = QDateTimeEdit(QDateTime.currentDateTime())
        self.time.setCalendarPopup(True)
        self.price = _money_spin(float(plan["entry_price"]))
        self.quantity = _money_spin(0.0)
        if plan["asset_class"] in {AssetClass.A_SHARE.value, AssetClass.CN_FUTURES.value}:
            self.quantity.setDecimals(0)
            self.quantity.setMinimum(1)
            if plan["asset_class"] == AssetClass.A_SHARE.value:
                self.quantity.setSingleStep(100)
        else:
            self.quantity.setDecimals(4)
            self.quantity.setMinimum(0.0001)
        self.fees = _money_spin(0.0)
        self.contract = QLineEdit()
        self.note = QLineEdit()
        form.addRow("成交时间", self.time)
        form.addRow("实际价格", self.price)
        form.addRow("实际数量", self.quantity)
        form.addRow("已知费用", self.fees)
        if plan["asset_class"] == AssetClass.CN_FUTURES.value:
            form.addRow("真实期货合约", self.contract)
        form.addRow("备注", self.note)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_accept(self) -> None:
        if self.plan["asset_class"] == AssetClass.CN_FUTURES.value:
            contract = self.contract.text().strip().upper()
            if not contract or is_continuous_futures_symbol(contract):
                QMessageBox.warning(self, "真实合约必填", "主力连续合约只能分析，请填写真实交割合约。")
                return
        self.accept()

    def execution(self) -> Execution:
        return Execution(
            id=uuid.uuid4().hex,
            plan_id=self.plan["id"],
            executed_at=self.time.dateTime().toPyDateTime().astimezone().isoformat(),
            price=self.price.value(), quantity=self.quantity.value(),
            real_contract=self.contract.text().strip().upper(), fees=self.fees.value(),
            note=self.note.text().strip(),
        )


class ExitDialog(QDialog):
    def __init__(self, plan: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认真实退出")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.time = QDateTimeEdit(QDateTime.currentDateTime())
        self.time.setCalendarPopup(True)
        self.price = _money_spin(float(plan["take_profit_price"]))
        self.fees = _money_spin(0.0)
        self.note = QLineEdit()
        form.addRow("退出时间", self.time)
        form.addRow("真实退出价格", self.price)
        form.addRow("退出费用", self.fees)
        form.addRow("备注", self.note)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "exited_at": self.time.dateTime().toPyDateTime().astimezone().isoformat(),
            "exit_price": self.price.value(), "exit_fees": self.fees.value(),
            "note": self.note.text().strip(),
        }


class InstrumentProfileDialog(QDialog):
    def __init__(self, profile: InstrumentProfile, parent=None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(f"品种参数 - {profile.symbol}")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.tick = _optional_spin(profile.tick_size)
        self.precision = QSpinBox(); self.precision.setRange(0, 12); self.precision.setValue(profile.price_precision or 2)
        self.costs = QCheckBox("成本参数已核实"); self.costs.setChecked(profile.costs_configured)
        self.commission = _optional_spin(profile.commission_rate, decimals=8)
        self.minimum_commission = _optional_spin(profile.minimum_commission)
        self.sell_tax = _optional_spin(profile.sell_tax_rate, decimals=8)
        self.multiplier = _optional_spin(profile.contract_multiplier)
        self.margin = _optional_spin(profile.margin_rate, decimals=6)
        self.fee_lot = _optional_spin(profile.fee_per_lot)
        self.slippage = _optional_spin(profile.estimated_slippage_ticks)
        self.real_contract = QLineEdit(profile.real_contract)
        form.addRow("最小跳动", self.tick)
        form.addRow("价格精度", self.precision)
        form.addRow("成本状态", self.costs)
        if profile.asset_class is AssetClass.A_SHARE:
            form.addRow("佣金率", self.commission)
            form.addRow("最低佣金", self.minimum_commission)
            form.addRow("卖出税率", self.sell_tax)
        elif profile.asset_class is AssetClass.CN_FUTURES:
            form.addRow("默认真实合约", self.real_contract)
            form.addRow("合约乘数", self.multiplier)
            form.addRow("保证金率", self.margin)
            form.addRow("每手手续费", self.fee_lot)
            form.addRow("估计滑点(跳)", self.slippage)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> InstrumentProfile:
        updates = {
            "tick_size": _none_if_zero(self.tick.value()), "price_precision": self.precision.value(),
            "costs_configured": self.costs.isChecked(), "confirmed": True,
            "commission_rate": _none_if_zero(self.commission.value()),
            "minimum_commission": _none_if_zero(self.minimum_commission.value()),
            "sell_tax_rate": _none_if_zero(self.sell_tax.value()),
            "real_contract": self.real_contract.text().strip().upper(),
            "contract_multiplier": _none_if_zero(self.multiplier.value()),
            "margin_rate": _none_if_zero(self.margin.value()),
            "fee_per_lot": _none_if_zero(self.fee_lot.value()),
            "estimated_slippage_ticks": _none_if_zero(self.slippage.value()),
        }
        return self.profile.model_copy(update=updates)


def _money_spin(value: float) -> QDoubleSpinBox:
    spin = QDoubleSpinBox(); spin.setRange(0, 1_000_000_000); spin.setDecimals(6); spin.setValue(value)
    return spin


def _optional_spin(value: float | None, decimals: int = 6) -> QDoubleSpinBox:
    spin = _money_spin(float(value or 0)); spin.setDecimals(decimals); return spin


def _none_if_zero(value: float) -> float | None:
    return value if value > 0 else None
