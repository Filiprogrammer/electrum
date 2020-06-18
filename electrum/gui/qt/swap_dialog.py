from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QMenu, QHBoxLayout, QLabel, QVBoxLayout, QGridLayout, QLineEdit,
                             QPushButton, QAbstractItemView, QComboBox)
from PyQt5.QtGui import QFont, QStandardItem, QBrush

from electrum.util import bh2u, NotEnoughFunds, NoDynamicFeeEstimates
from electrum.i18n import _
from electrum.lnchannel import AbstractChannel, PeerState
from electrum.wallet import Abstract_Wallet
from electrum.lnutil import LOCAL, REMOTE, format_short_channel_id, LN_MAX_FUNDING_SAT
from electrum.lnutil import ln_dummy_address
from electrum.lnworker import LNWallet
from electrum.transaction import PartialTxOutput

from .util import (MyTreeView, WindowModalDialog, Buttons, OkButton, CancelButton,
                   EnterButton, WaitingDialog, MONOSPACE_FONT, ColorScheme)
from .amountedit import BTCAmountEdit, FreezableLineEdit
from .util import WWLabel
from .fee_slider import FeeSlider, FeeComboBox

import asyncio
from .util import read_QIcon

CANNOT_RECEIVE_WARNING = """
The requested amount is higher than what you can receive in your currently open channels.
If you continue, your funds will be locked until the remote server can find a path to pay you.
If the swap cannot be performed after 24h, you will be refunded.
Do you want to continue?
"""

class SwapDialog(WindowModalDialog):

    def __init__(self, window):
        WindowModalDialog.__init__(self, window, _('Submarine Swap'))
        self.window = window
        self.config = window.config
        self.lnworker = self.window.wallet.lnworker
        self.swap_manager = self.lnworker.swap_manager
        self.network = window.network
        vbox = QVBoxLayout(self)
        vbox.addWidget(WWLabel('Swap lightning funds for on-chain funds if you need to increase your receiving capacity. This service is powered by the Boltz backend.'))
        self.send_amount_e = BTCAmountEdit(self.window.get_decimal_point)
        self.send_amount_e.shortcut.connect(self.spend_max)
        self.recv_amount_e = BTCAmountEdit(self.window.get_decimal_point)
        self.max_button = EnterButton(_("Max"), self.spend_max)
        self.max_button.setFixedWidth(100)
        self.max_button.setCheckable(True)
        self.send_button = QPushButton('')
        self.recv_button = QPushButton('')
        self.send_follows = False
        self.is_reverse = True
        self.send_amount_e.follows = False
        self.recv_amount_e.follows = False
        self.send_button.clicked.connect(self.toggle_direction)
        self.recv_button.clicked.connect(self.toggle_direction)
        self.send_amount_e.textChanged.connect(self.on_send_edited)
        self.recv_amount_e.textChanged.connect(self.on_recv_edited)
        fee_slider = FeeSlider(self.window, self.config, self.fee_slider_callback)
        fee_combo = FeeComboBox(fee_slider)
        fee_slider.update()
        self.fee_label = QLabel()
        self.server_fee_label = QLabel()
        h = QGridLayout()
        h.addWidget(QLabel(_('You send')+':'), 1, 0)
        h.addWidget(self.send_amount_e, 1, 1)
        h.addWidget(self.send_button, 1, 2)
        h.addWidget(self.max_button, 1, 3)
        h.addWidget(QLabel(_('You receive')+':'), 2, 0)
        h.addWidget(self.recv_amount_e, 2, 1)
        h.addWidget(self.recv_button, 2, 2)
        h.addWidget(QLabel(_('Server fee')+':'), 4, 0)
        h.addWidget(self.server_fee_label, 4, 1)
        h.addWidget(QLabel(_('Mining fee')+':'), 5, 0)
        h.addWidget(self.fee_label, 5, 1)
        h.addWidget(fee_slider, 6, 1)
        h.addWidget(fee_combo, 6, 2)
        vbox.addLayout(h)
        vbox.addStretch(1)
        ok_button = OkButton(self)
        ok_button.setDefault(True)
        vbox.addLayout(Buttons(CancelButton(self), ok_button))
        self.update()

    def fee_slider_callback(self, dyn, pos, fee_rate):
        if dyn:
            if self.config.use_mempool_fees():
                self.config.set_key('depth_level', pos, False)
            else:
                self.config.set_key('fee_level', pos, False)
        else:
            self.config.set_key('fee_per_kb', fee_rate, False)
        if self.send_follows:
            self.on_recv_edited()
        else:
            self.on_send_edited()
        self.update()

    def toggle_direction(self):
        self.is_reverse = not self.is_reverse
        self.send_amount_e.setAmount(None)
        self.recv_amount_e.setAmount(None)
        self.update()

    def spend_max(self):
        if self.is_reverse:
            return
        if self.max_button.isChecked():
            self.update_tx('!')
            if self.tx:
                txo = self.tx.outputs()[0]
                self.send_amount_e.setAmount(txo.value)
        else:
            self.tx = None
            self.send_amount_e.setAmount(None)
            self.update_fee()

    def on_send_edited(self):
        if self.send_amount_e.follows:
            return
        self.send_amount_e.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
        send_amount = self.send_amount_e.get_amount()
        recv_amount = self.swap_manager.get_recv_amount(send_amount, self.is_reverse)
        if self.is_reverse and send_amount and send_amount > self.lnworker.num_sats_can_send():
            recv_amount = None
        self.recv_amount_e.follows = True
        self.recv_amount_e.setAmount(recv_amount)
        self.recv_amount_e.setStyleSheet(ColorScheme.BLUE.as_stylesheet())
        self.recv_amount_e.follows = False
        self.send_follows = False
        self.update_fee()

    def on_recv_edited(self):
        if self.recv_amount_e.follows:
            return
        self.recv_amount_e.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
        recv_amount = self.recv_amount_e.get_amount()
        send_amount = self.swap_manager.get_send_amount(recv_amount, self.is_reverse)
        if self.is_reverse and send_amount and send_amount > self.lnworker.num_sats_can_send():
            send_amount = None
        self.send_amount_e.follows = True
        self.send_amount_e.setAmount(send_amount)
        self.send_amount_e.setStyleSheet(ColorScheme.BLUE.as_stylesheet())
        self.send_amount_e.follows = False
        self.send_follows = True
        self.update_fee()

    def update(self):
        sm = self.swap_manager
        self.send_button.setIcon(read_QIcon("lightning.png" if self.is_reverse else "bitcoin.png"))
        self.recv_button.setIcon(read_QIcon("lightning.png" if not self.is_reverse else "bitcoin.png"))
        self.max_button.setEnabled(not self.is_reverse)
        server_mining_fee = sm.lockup_fee if self.is_reverse else sm.normal_fee
        server_fee_str = '%.2f'%sm.percentage + '%  +  '  + self.window.format_amount(server_mining_fee) + ' ' + self.window.base_unit()
        self.server_fee_label.setText(server_fee_str)
        self.update_fee()

    def update_fee(self):
        if self.is_reverse:
            sm = self.swap_manager
            fee = sm.get_claim_fee()
        else:
            is_max = self.max_button.isChecked()
            onchain_amount = '!' if is_max else self.send_amount_e.get_amount()
            self.update_tx(onchain_amount)
            fee = self.tx.get_fee() if self.tx else None
            if is_max and self.tx:
                txo = self.tx.outputs()[0]
                self.send_amount_e.setAmount(txo.value)
        fee_text = self.window.format_amount(fee) + ' ' + self.window.base_unit() if fee else ''
        self.fee_label.setText(fee_text)

    def run(self):
        self.window.run_coroutine_from_thread(self.swap_manager.get_pairs(), lambda x: self.update())
        if not self.exec_():
            return
        if self.is_reverse:
            lightning_amount = self.send_amount_e.get_amount()
            onchain_amount = self.recv_amount_e.get_amount()
            if lightning_amount is None or onchain_amount is None:
                return
            coro = self.swap_manager.reverse_swap(lightning_amount, onchain_amount + self.swap_manager.get_claim_fee())
            self.window.run_coroutine_from_thread(coro)
        else:
            lightning_amount = self.recv_amount_e.get_amount()
            onchain_amount = self.send_amount_e.get_amount()
            if lightning_amount is None or onchain_amount is None:
                return
            if lightning_amount > self.lnworker.num_sats_can_receive():
                if not self.window.question(CANNOT_RECEIVE_WARNING):
                    return
            self.window.protect(self.do_normal_swap, (lightning_amount, onchain_amount))

    def update_tx(self, onchain_amount):
        if onchain_amount is None:
            self.tx = None
            return
        outputs = [PartialTxOutput.from_address_and_value(ln_dummy_address(), onchain_amount)]
        coins = self.window.get_coins()
        self.tx = self.window.wallet.make_unsigned_transaction(
            coins=coins,
            outputs=outputs)

    def do_normal_swap(self, lightning_amount, onchain_amount, password):
        coro = self.swap_manager.normal_swap(lightning_amount, onchain_amount, password, tx=self.tx)
        self.window.run_coroutine_from_thread(coro)