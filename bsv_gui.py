"""
bsv_gui.py - aplikacja z oknem (tkinter): zapis tekstu w OP_RETURN na BSV, z opcjonalną płatnością.

Co robi:
  1. Przełącznik sieci testnet / mainnet.
  2. Klucz prywatny WIF: wklejenie albo wygenerowanie nowego, zapis jawnym tekstem do
     config.json (TYLKO NA TESTY).
  3. Adres wyliczony z klucza (format zależy od sieci), saldo, link do kranu testnetu.
  4. Opcjonalna płatność na wybrany adres (domyślnie adres i kwota z DEFAULT_PAY_*),
     w tej samej transakcji co tekst.
  5. Tekst do zapisania, obliczenie kosztu, wysłanie i wyświetlenie TXID.

Uruchomienie:
  pip install bsv-sdk requests aiohttp pycryptodomex
  python bsv_gui.py
(bsv_common.py musi leżeć obok tego pliku).
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from bsv.constants import Network
from bsv.fee_models import SatoshisPerKilobyte
from bsv.keys import PrivateKey
from bsv.script.type import OpReturn, P2PKH
from bsv.transaction import Transaction
from bsv.transaction_output import TransactionOutput

import bsv_common as bc
from bsv_common import (ChainError, InsufficientFunds, MAINNET_PHRASE, Utxo, broadcast_async, explorer_tx_url,
                        fetch_balance, finalize_transaction, get_network, make_inputs,
                        run_async, select_utxos, validate_address, woc_url)

# ---------------------------------------------------------------------------
# KONFIGURACJA
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_PAY_ADDRESS = "mkEUEycqYbDxvkE5PB9RfaiJ9XJMZVHPAD"   # domyślny odbiorca (adres testnetu)
DEFAULT_PAY_AMOUNT = 333                                       # domyślna kwota w satoshi
DEFAULT_CONFIG = {
    "chain": "testnet",          # "testnet" albo "mainnet"
    "wif": "",                   # UWAGA: jawny tekst, tylko do testów!
    "message": "",
    "pay_enabled": True,         # czy dołączyć płatność do transakcji
    "pay_address": DEFAULT_PAY_ADDRESS,
    "pay_amount": DEFAULT_PAY_AMOUNT,
    "fee_sat_per_kb": 100,       # stała stawka opłaty (sat/kB), patrz known-gaps.md pkt 1-2
}
MIN_PAYMENT_SATS = 1             # najmniejsza dozwolona kwota (limit "dust" sieci: niezweryfikowany)
MAX_PAYLOAD_BYTES = 4096         # bezpiecznik przed przypadkowo wielkim zapisem (NIE limit sieci)
MAX_FEE_SATS = 10_000            # odrzuć transakcję, jeśli opłata wyjdzie większa
FAUCET_URL = "https://faucet.bananablocks.com/"   # kran testnetu


# ---------------------------------------------------------------------------
# LOGIKA (bez okna - łatwo testować)
# ---------------------------------------------------------------------------
def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update({k: loaded[k] for k in DEFAULT_CONFIG if k in loaded})
    except (OSError, json.JSONDecodeError):
        pass  # brak pliku lub uszkodzony - zaczynamy od domyślnych
    return config


def save_config(config: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        raise ChainError(f"Nie można zapisać {CONFIG_PATH}: {e}") from e


def parse_wif(wif: str) -> PrivateKey:
    """WIF -> klucz. Komunikat błędu nie zawiera klucza."""
    wif = wif.strip()
    if not wif:
        raise ChainError("Brak klucza prywatnego.")
    try:
        return PrivateKey(wif)
    except Exception:
        raise ChainError("Niepoprawny klucz WIF.") from None


def generate_key(net: dict) -> PrivateKey:
    """Nowy losowy klucz. WIF zawiera sieć: testnet zaczyna się od 'c', mainnet od 'K' lub 'L'."""
    return PrivateKey(network=Network.TESTNET if net["name"] == "testnet" else Network.MAINNET)


def address_for(key: PrivateKey, net: dict) -> str:
    """Ten sam klucz daje inny zapis adresu zależnie od sieci (testnet: m/n, mainnet: 1)."""
    return key.address(network=net["sdk"])


def check_recipient(address: str, net: dict) -> tuple[bool, str]:
    """Sprawdza adres odbiorcy względem wybranej sieci. Zwraca (czy_ok, komunikat)."""
    address = address.strip()
    if not address:
        return False, "Podaj adres odbiorcy."
    if validate_address(address, net):
        return True, f"Poprawny adres {net['name']}."
    other = get_network("mainnet" if net["name"] == "testnet" else "testnet")
    if validate_address(address, other):
        return False, f"To adres sieci {other['name']}, a wybrana jest {net['name']}."
    return False, "Niepoprawny adres (zła długość, suma kontrolna albo znaki)."


def parse_amount(text: str) -> int:
    text = str(text).strip()
    if not text.isdigit() or len(text) > 15:
        raise ChainError("Kwota musi być liczbą całkowitą satoshi (bez przecinków i spacji).")
    amount = int(text)
    if amount < MIN_PAYMENT_SATS:
        raise ChainError(f"Kwota musi wynosić co najmniej {MIN_PAYMENT_SATS} sat.")
    return amount


@dataclass
class UtxoReport:
    """Rozbicie tego, co leży na adresie. Wyjaśnia, czemu 'do wydania' różni się od salda potwierdzonego."""
    usable: list[Utxo]        # te wolno wydać na zwykłą płatność
    unconfirmed_value: int    # część usable jeszcze bez potwierdzenia
    small_count: int          # pominięte: UTXO po 1 sat (mogą być NFT / tokeny 1Sat)
    small_value: int
    spent_count: int          # pominięte: już wydane w mempoolu
    spent_value: int

    @property
    def usable_value(self) -> int:
        return sum(u.value for u in self.usable)


def fetch_utxo_report(address: str, net: dict) -> UtxoReport:
    items = bc.http_get(woc_url(f"address/{address}/unspent/all", net)).get("result", [])
    usable, unconfirmed = [], 0
    small_n = small_v = spent_n = spent_v = 0
    for item in items:
        value = int(item["value"])
        if item.get("isSpentInMempoolTx"):
            spent_n, spent_v = spent_n + 1, spent_v + value
        elif value <= bc.ORDINAL_MAX_VALUE:
            small_n, small_v = small_n + 1, small_v + value
        else:
            height = item.get("height") or 0
            usable.append(Utxo(item["tx_hash"], int(item["tx_pos"]), value, height))
            if height <= 0:
                unconfirmed += value
    return UtxoReport(usable, unconfirmed, small_n, small_v, spent_n, spent_v)


def format_balance(confirmed: int, report: UtxoReport) -> str:
    lines = [f"Saldo potwierdzone (całość na adresie): {confirmed} sat",
             f"Do wydania: {report.usable_value} sat w {len(report.usable)} UTXO"
             + (f" (w tym niepotwierdzone: {report.unconfirmed_value} sat)" if report.unconfirmed_value else "")]
    skipped = []
    if report.small_count:
        skipped.append(f"{report.small_count} UTXO po 1 sat ({report.small_value} sat, mogą być NFT)")
    if report.spent_count:
        skipped.append(f"{report.spent_count} UTXO już wydane w mempoolu ({report.spent_value} sat)")
    if skipped:
        lines.append("Pominięte: " + "; ".join(skipped))
    return "\n".join(lines)


@dataclass
class Prepared:
    tx: Transaction
    text: str
    chain: str
    address: str
    payload_bytes: int
    pay_address: str | None   # None = bez płatności, tylko zapis tekstu
    pay_amount: int
    fee: int
    tx_size: int
    available: int            # suma wszystkich UTXO do wydania
    inputs_used: int          # ile UTXO faktycznie użyto
    inputs_total: int         # ile UTXO było do wyboru
    inputs_value: int         # suma wartości użytych UTXO
    change: int
    change_dropped: bool


def prepare_transaction(text: str, key: PrivateKey, net: dict, fee_sat_per_kb: int,
                        pay_address: str | None = None, pay_amount: int = 0) -> Prepared:
    """
    Buduje i podpisuje transakcję: OP_RETURN z tekstem, opcjonalna płatność i reszta do nadawcy.
    Nic nie rozgłasza. UTXO są dobierane od największego i dokładane po jednym, aż suma wejść
    pokryje kwotę i opłatę policzoną dla DOKŁADNIE tej transakcji (a nie tylko oszacowaną).
    """
    if not text.strip():
        raise ChainError("Wpisz tekst do zapisania.")
    size = len(text.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise ChainError(f"Tekst ma {size} B, a bezpiecznik to {MAX_PAYLOAD_BYTES} B. Skróć tekst.")
    if pay_address is not None:
        ok, message = check_recipient(pay_address, net)
        if not ok:
            raise ChainError(f"Adres odbiorcy: {message}")
        if pay_amount < MIN_PAYMENT_SATS:
            raise ChainError(f"Kwota musi wynosić co najmniej {MIN_PAYMENT_SATS} sat.")
        pay_address = pay_address.strip()
    else:
        pay_amount = 0

    address = address_for(key, net)
    report = fetch_utxo_report(address, net)
    usable = sorted(report.usable, key=lambda u: u.value, reverse=True)
    if not usable:
        extra = ""
        if report.small_count or report.spent_count:
            extra = " (UTXO po 1 sat i wydane w mempoolu są pomijane)"
        raise ChainError(f"Adres {address} nie ma środków do wydania{extra}. Wyślij na niego trochę BSV "
                         f"({net['name']}) i poczekaj na pojawienie się wpłaty.")

    data_script = OpReturn().lock([text])
    fee_model = SatoshisPerKilobyte(fee_sat_per_kb)
    try:  # punkt startowy: szacunek, ile UTXO zwykle wystarczy
        count = len(select_utxos(usable, pay_amount, n_outputs=3 if pay_address else 2,
                                 extra_bytes=len(data_script.serialize()), sat_per_kb=fee_sat_per_kb))
    except ChainError:
        count = len(usable)

    inputs: list = []
    while True:
        while len(inputs) < count:  # hex źródłowej transakcji pobieramy tylko raz dla każdego UTXO
            inputs += make_inputs([usable[len(inputs)]], key, net)
        tx = Transaction()
        for tx_input in inputs:
            tx.add_input(tx_input)
        tx.add_output(TransactionOutput(locking_script=data_script, satoshis=0))
        if pay_address:
            tx.add_output(TransactionOutput(locking_script=P2PKH().lock(pay_address), satoshis=pay_amount))
        tx.add_output(TransactionOutput(locking_script=P2PKH().lock(address), change=True))
        try:
            fee_report = finalize_transaction(tx, fee_model, max_fee=MAX_FEE_SATS)
            break
        except InsufficientFunds:
            if count >= len(usable):
                what = f"kwoty {pay_amount} sat i opłaty" if pay_address else "opłaty"
                raise ChainError(f"Za mało środków: wszystkie UTXO do wydania ({len(usable)} szt., razem "
                                 f"{report.usable_value} sat) nie pokrywają {what}. Drobne UTXO mogą kosztować "
                                 f"więcej (każde wejście to ok. 148 B), niż są warte.") from None
            count += 1  # dobierz kolejne UTXO i policz od nowa

    change = sum(out.satoshis for out in tx.outputs if out.change)
    return Prepared(tx, text, net["name"], address, size, pay_address, pay_amount, fee_report.actual,
                    tx.byte_length(), report.usable_value, count, len(usable),
                    sum(u.value for u in usable[:count]), change, fee_report.change_dropped)


# ---------------------------------------------------------------------------
# WYGLĄD
# ---------------------------------------------------------------------------
BG = "#eef1f6"            # tło okna
CARD = "#ffffff"          # tło kart
BORDER = "#d9dee8"
TEXT = "#1f2937"
MUTED = "#6b7280"
HEADER = "#1e293b"
ACCENT = "#4f46e5"
ACCENT_DARK = "#4338ca"
OK = "#16a34a"
OK_DARK = "#15803d"
DANGER = "#dc2626"
WARN = "#b45309"
BTN = "#e5e7eb"
BTN_ACTIVE = "#d1d5db"
DISABLED_BG = "#e5e7eb"
DISABLED_FG = "#9ca3af"


def pick_font(root, candidates: tuple[str, ...], fallback: str) -> str:
    available = set(tkfont.families(root))
    return next((name for name in candidates if name in available), fallback)


def setup_style(root) -> dict:
    """Konfiguruje motyw ttk. Zwraca czcionki."""
    family = pick_font(root, ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans"), "TkDefaultFont")
    mono = pick_font(root, ("Consolas", "Menlo", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono"), "TkFixedFont")
    fonts = {
        "base": (family, 10), "small": (family, 9), "bold": (family, 10, "bold"),
        "title": (family, 16, "bold"), "card": (family, 9, "bold"), "mono": (mono, 10), "mono_small": (mono, 8),
        "text": (family, 11),
    }
    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", font=fonts["base"], background=BG, foreground=TEXT)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Card.TLabel", background=CARD, foreground=TEXT)
    st.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=fonts["card"])
    st.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED, font=fonts["small"])
    st.configure("Warn.Card.TLabel", background=CARD, foreground=WARN, font=fonts["small"])
    st.configure("Ok.Card.TLabel", background=CARD, foreground=OK, font=fonts["small"])
    st.configure("Bad.Card.TLabel", background=CARD, foreground=DANGER, font=fonts["small"])
    st.configure("Key.Card.TLabel", background=CARD, foreground=MUTED, font=fonts["small"])
    st.configure("Status.TLabel", background=BORDER, foreground=TEXT, font=fonts["small"], padding=(14, 6))

    st.configure("Card.TCheckbutton", background=CARD, foreground=TEXT)
    st.map("Card.TCheckbutton", background=[("active", CARD)], foreground=[("disabled", DISABLED_FG)],
           indicatorbackground=[("selected", ACCENT), ("!selected", "#ffffff")],
           indicatorforeground=[("selected", "#ffffff")])
    st.configure("Card.TCheckbutton", indicatorbackground="#ffffff", bordercolor=BORDER, focuscolor=CARD)

    st.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                 padding=7, insertcolor=TEXT)
    st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
           fieldbackground=[("readonly", "#f3f4f6"), ("disabled", "#f3f4f6")], foreground=[("disabled", DISABLED_FG)])

    def button(name: str, bg: str, hover: str, fg: str = TEXT) -> None:
        st.configure(name, background=bg, foreground=fg, borderwidth=0, focusthickness=0, focuscolor=bg,
                     padding=(14, 8), relief="flat", font=fonts["bold"])
        st.map(name, background=[("disabled", DISABLED_BG), ("pressed", hover), ("active", hover)],
               foreground=[("disabled", DISABLED_FG)])
    button("TButton", BTN, BTN_ACTIVE)
    button("Accent.TButton", ACCENT, ACCENT_DARK, "#ffffff")
    button("Success.TButton", OK, OK_DARK, "#ffffff")
    st.configure("Small.TButton", padding=(10, 5), font=fonts["small"])

    # przełącznik sieci (dwa przyciski obok siebie)
    for name, color in (("Test.Seg.Toolbutton", OK), ("Main.Seg.Toolbutton", DANGER)):
        st.configure(name, background=BTN, foreground=TEXT, padding=(18, 9), relief="flat", borderwidth=0,
                     font=fonts["bold"], anchor="center")
        st.map(name, background=[("selected", color), ("active", BTN_ACTIVE)], foreground=[("selected", "#ffffff")])
    return fonts


# ---------------------------------------------------------------------------
# OKNO
# ---------------------------------------------------------------------------
class App(tk.Tk):
    COST_ROWS = ("Płatność", "Opłata", "Łącznie z konta", "Użyte UTXO", "Reszta")

    def __init__(self):
        super().__init__()
        self.title("BSV - zapis tekstu na blockchainie")
        height = min(820, self.winfo_screenheight() - 90)
        self.geometry(f"1100x{height}")
        self.minsize(960, 480)
        self.f = setup_style(self)

        config = load_config()
        self.fee_sat_per_kb = int(config.get("fee_sat_per_kb") or DEFAULT_CONFIG["fee_sat_per_kb"])
        self.chain_var = tk.StringVar(value=config["chain"] if config["chain"] in ("testnet", "mainnet") else "testnet")
        self.wif_var = tk.StringVar(value=config["wif"])
        self.show_wif_var = tk.BooleanVar(value=False)
        self.address_var = tk.StringVar()
        self.balance_var = tk.StringVar(value="Saldo: —")
        self.pay_enabled_var = tk.BooleanVar(value=bool(config["pay_enabled"]))
        self.pay_address_var = tk.StringVar(value=str(config["pay_address"]))
        self.pay_amount_var = tk.StringVar(value=str(config["pay_amount"]))
        self.pay_msg_var = tk.StringVar()
        self.cost_vars = {name: tk.StringVar(value="—") for name in self.COST_ROWS}
        self.cost_note_var = tk.StringVar()
        self.size_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Gotowe.")
        self.txid_var = tk.StringVar()
        self.prepared: Prepared | None = None
        self.busy = False
        self.results: queue.Queue = queue.Queue()
        self._balance_job = None
        self._balance_seq = 0
        self._last_explorer_url = None

        self._build_widgets(config["message"])
        self.chain_var.trace_add("write", lambda *_: self._on_network_or_key_changed())
        self.wif_var.trace_add("write", lambda *_: self._on_network_or_key_changed())
        for var in (self.pay_enabled_var, self.pay_address_var, self.pay_amount_var):
            var.trace_add("write", lambda *_: self._on_payment_changed())
        self._on_network_or_key_changed()
        self.after(100, self._poll_results)

    # --- budowa okna ---
    def _card(self, parent, title: str, expand: bool = False):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="both" if expand else "x", expand=expand, pady=(0, 10))
        ttk.Label(outer, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(10, 0))
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(16, 6, 16, 12))
        inner.pack(fill="both", expand=True)
        return inner

    def _entry(self, parent, var, mono: bool = False, **kw) -> ttk.Entry:
        return ttk.Entry(parent, textvariable=var, font=self.f["mono"] if mono else self.f["base"], **kw)

    def _build_widgets(self, message: str) -> None:
        # nagłówek
        header = tk.Frame(self, bg=HEADER)
        header.pack(fill="x")
        left = tk.Frame(header, bg=HEADER)
        left.pack(side="left", padx=22, pady=10)
        tk.Label(left, text="BSV  ·  zapis tekstu na blockchainie", bg=HEADER, fg="#ffffff", font=self.f["title"]).pack(anchor="w")
        tk.Label(left, text="Tekst trafia do wyjścia OP_RETURN, opcjonalnie razem z płatnością.", bg=HEADER,
                 fg="#94a3b8", font=self.f["small"]).pack(anchor="w")
        self.badge = tk.Label(header, text="", font=self.f["bold"], fg="#ffffff", padx=16, pady=7)
        self.badge.pack(side="right", padx=22)

        # status na dole
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(side="bottom", fill="x")

        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._vsb_shown = False
        body = ttk.Frame(self.canvas, padding=(20, 16, 20, 4))
        self._body_window = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: self._sync_scroll())
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(sequence, self._on_wheel)
        body.columnconfigure(0, weight=5, uniform="cols")
        body.columnconfigure(1, weight=6, uniform="cols")
        col_left = ttk.Frame(body)
        col_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        col_right = ttk.Frame(body)
        col_right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        # --- lewa kolumna: konto ---
        net_card = self._card(col_left, "Sieć")
        seg = ttk.Frame(net_card, style="Card.TFrame")
        seg.pack(anchor="w")
        ttk.Radiobutton(seg, text="Testnet", variable=self.chain_var, value="testnet",
                        style="Test.Seg.Toolbutton").pack(side="left")
        ttk.Radiobutton(seg, text="Mainnet", variable=self.chain_var, value="mainnet",
                        style="Main.Seg.Toolbutton").pack(side="left", padx=(2, 0))
        self.net_hint = ttk.Label(net_card, text="", style="Muted.Card.TLabel", wraplength=400, justify="left")
        self.net_hint.pack(anchor="w", pady=(8, 0))

        key_card = self._card(col_left, "Klucz prywatny (WIF)")
        entry_row = ttk.Frame(key_card, style="Card.TFrame")
        entry_row.pack(fill="x")
        entry_row.columnconfigure(0, weight=1)
        self.wif_entry = self._entry(entry_row, self.wif_var, mono=True, show="•")
        self.wif_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(entry_row, text="pokaż", style="Card.TCheckbutton", variable=self.show_wif_var,
                        command=lambda: self.wif_entry.config(show="" if self.show_wif_var.get() else "•")
                        ).grid(row=0, column=1, padx=(10, 0))
        row = ttk.Frame(key_card, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Zapisz klucz", command=self.on_save_key).pack(side="left")
        ttk.Button(row, text="Nowy klucz", style="Accent.TButton", command=self.on_generate_key).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Kopiuj", command=self.on_copy_key).pack(side="left", padx=(8, 0))
        ttk.Label(key_card, text="Klucz jest zapisywany jawnym tekstem w config.json - tylko do testów. "
                                 "Zrób kopię klucza: bez niego nie odzyskasz środków.",
                  style="Warn.Card.TLabel", wraplength=400, justify="left").pack(anchor="w", pady=(10, 0))

        addr_card = self._card(col_left, "Adres i saldo")
        self._entry(addr_card, self.address_var, mono=True, state="readonly").pack(fill="x")
        row = ttk.Frame(addr_card, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Kopiuj adres", command=self.on_copy_address).pack(side="left")
        ttk.Button(row, text="Odśwież saldo", command=self.refresh_balance).pack(side="left", padx=(8, 0))
        ttk.Label(addr_card, textvariable=self.balance_var, style="Card.TLabel", wraplength=400,
                  justify="left").pack(anchor="w", pady=(12, 0))
        self.faucet_btn = ttk.Button(addr_card, text="Kran testnet: darmowe monety", command=self.on_open_faucet)
        self.faucet_btn.pack(anchor="w", pady=(12, 0))

        # --- prawa kolumna: transakcja ---
        pay_card = self._card(col_right, "Płatność (opcjonalnie)")
        ttk.Checkbutton(pay_card, text="Wyślij też satoshi na adres w tej samej transakcji", style="Card.TCheckbutton",
                        variable=self.pay_enabled_var).pack(anchor="w")
        grid = ttk.Frame(pay_card, style="Card.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="Adres odbiorcy", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=3)
        self.pay_address_entry = self._entry(grid, self.pay_address_var, mono=True)
        self.pay_address_entry.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(grid, text="Kwota", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=3)
        amount_row = ttk.Frame(grid, style="Card.TFrame")
        amount_row.grid(row=1, column=1, sticky="w", pady=3)
        self.pay_amount_entry = self._entry(amount_row, self.pay_amount_var, width=14)
        self.pay_amount_entry.pack(side="left")
        ttk.Label(amount_row, text="satoshi", style="Muted.Card.TLabel").pack(side="left", padx=(8, 12))
        self.pay_reset_btn = ttk.Button(amount_row, text="Przywróć domyślne", style="Small.TButton",
                                        command=self.on_reset_payment)
        self.pay_reset_btn.pack(side="left")
        self.pay_msg_label = ttk.Label(pay_card, textvariable=self.pay_msg_var, style="Muted.Card.TLabel",
                                       wraplength=560, justify="left")
        self.pay_msg_label.pack(anchor="w", pady=(6, 0))

        msg_card = self._card(col_right, "Tekst do zapisania (publiczny i nieodwracalny)")
        self.text = tk.Text(msg_card, height=4, wrap="word", font=self.f["text"], bg="#ffffff", fg=TEXT,
                            insertbackground=TEXT, relief="flat", highlightthickness=1, highlightbackground=BORDER,
                            highlightcolor=ACCENT, padx=10, pady=8, undo=True)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", message)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.edit_modified(False)
        ttk.Label(msg_card, textvariable=self.size_var, style="Muted.Card.TLabel").pack(anchor="e", pady=(6, 0))

        cost_card = self._card(col_right, "Koszt, wysyłka i hash transakcji")
        table = ttk.Frame(cost_card, style="Card.TFrame")
        table.pack(fill="x")
        table.columnconfigure(1, weight=1)
        for i, name in enumerate(self.COST_ROWS):
            ttk.Label(table, text=name, style="Muted.Card.TLabel").grid(row=i, column=0, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(table, textvariable=self.cost_vars[name], style="Card.TLabel").grid(row=i, column=1, sticky="w", pady=1)
        self.cost_note_label = ttk.Label(cost_card, textvariable=self.cost_note_var, style="Warn.Card.TLabel",
                                         wraplength=560, justify="left")
        row = ttk.Frame(cost_card, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        self.cost_btn_row = row
        self.cost_note_var.trace_add("write", lambda *_: self._update_note_visibility())
        self.calc_btn = ttk.Button(row, text="Zapisz i oblicz koszt", command=self.on_calculate)
        self.calc_btn.pack(side="left")
        self.send_btn = ttk.Button(row, text="Wyślij", style="Success.TButton", command=self.on_send, state="disabled")
        self.send_btn.pack(side="left", padx=(10, 0))

        tx_head = ttk.Frame(cost_card, style="Card.TFrame")
        tx_head.pack(fill="x", pady=(12, 4))
        tx_head.columnconfigure(0, weight=1)
        ttk.Label(tx_head, text="Hash transakcji (TXID)", style="Muted.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(tx_head, text="Kopiuj", style="Small.TButton", command=self.on_copy_txid).grid(row=0, column=1, padx=(8, 0))
        self.open_btn = ttk.Button(tx_head, text="Eksplorator", style="Small.TButton", command=self.on_open_explorer,
                                   state="disabled")
        self.open_btn.grid(row=0, column=2, padx=(8, 0))
        ttk.Entry(cost_card, textvariable=self.txid_var, font=self.f["mono_small"], state="readonly").pack(fill="x")

        self._update_size_label()

    # --- przewijanie (awaryjne, gdy okno jest niższe niż zawartość) ---
    def _on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self._body_window, width=event.width)
        self._sync_scroll()

    def _sync_scroll(self) -> None:
        self.update_idletasks()
        content = self.canvas.bbox("all")
        if not content:
            return
        self.canvas.configure(scrollregion=(0, 0, content[2], content[3]))
        needs_scroll = content[3] > self.canvas.winfo_height() > 1
        if needs_scroll and not self._vsb_shown:
            self.vsb.pack(side="right", fill="y", before=self.canvas)
            self._vsb_shown = True
        elif not needs_scroll and self._vsb_shown:
            self.vsb.pack_forget()
            self._vsb_shown = False
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event) -> None:
        if not self._vsb_shown or isinstance(event.widget, tk.Text):
            return
        if getattr(event, "num", 0) == 4:
            step = -1
        elif getattr(event, "num", 0) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step * 2, "units")

    # --- pomocnicze ---
    def net(self) -> dict:
        return get_network(self.chain_var.get())

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _update_note_visibility(self) -> None:
        """Linia uwag pod tabelą kosztu jest widoczna tylko wtedy, gdy ma treść."""
        if self.cost_note_var.get():
            self.cost_note_label.pack(anchor="w", pady=(8, 0), before=self.cost_btn_row)
        else:
            self.cost_note_label.pack_forget()

    def invalidate(self) -> None:
        """Zmiana sieci, klucza, płatności lub tekstu unieważnia obliczoną transakcję."""
        self.prepared = None
        self.send_btn.config(state="disabled")
        for var in self.cost_vars.values():
            var.set("—")
        self.cost_note_var.set("")

    def _update_payment_validity(self) -> None:
        if not self.pay_enabled_var.get():
            self.pay_msg_var.set("Płatność wyłączona: zostanie zapisany tylko tekst.")
            self.pay_msg_label.config(style="Muted.Card.TLabel")
            state = "disabled"
        else:
            state = "normal"
            ok, message = check_recipient(self.pay_address_var.get(), self.net())
            try:
                parse_amount(self.pay_amount_var.get())
            except ChainError as e:
                if ok:
                    ok, message = False, str(e)
            self.pay_msg_var.set(("✓ " if ok else "✗ ") + message)
            self.pay_msg_label.config(style="Ok.Card.TLabel" if ok else "Bad.Card.TLabel")
        for widget in (self.pay_address_entry, self.pay_amount_entry, self.pay_reset_btn):
            widget.config(state=state)

    def _on_payment_changed(self) -> None:
        self.invalidate()
        self._update_payment_validity()

    def _on_network_or_key_changed(self) -> None:
        self.invalidate()
        net = self.net()
        if net["name"] == "testnet":
            self.badge.config(text="TESTNET  ·  monety bez wartości", bg=OK)
            self.net_hint.config(text="Bezpieczny tryb do prób. Monety testowe weźmiesz z kranu.")
            self.faucet_btn.pack(anchor="w", pady=(12, 0))
        else:
            self.badge.config(text="MAINNET  ·  prawdziwe pieniądze", bg=DANGER)
            self.net_hint.config(text="Uwaga: transakcji nie da się cofnąć. Wysyłka wymaga przepisania frazy potwierdzającej.")
            self.faucet_btn.pack_forget()
        try:
            self.address_var.set(address_for(parse_wif(self.wif_var.get()), net))
        except ChainError as e:
            self.address_var.set("" if not self.wif_var.get().strip() else f"({e})")
        self._update_payment_validity()
        # saldo odświeżamy z opóźnieniem, żeby nie odpytywać sieci przy każdej wpisanej literze klucza
        if self._balance_job is not None:
            self.after_cancel(self._balance_job)
        self.balance_var.set("Saldo: —")
        self._balance_job = self.after(700, self.refresh_balance)

    def _update_size_label(self) -> None:
        size = len(self.text.get("1.0", "end-1c").encode("utf-8"))
        self.size_var.set(f"{size} B z {MAX_PAYLOAD_BYTES} B")

    def _on_text_modified(self, _event) -> None:
        if self.text.edit_modified():
            self.invalidate()
            self._update_size_label()
            self.text.edit_modified(False)

    def current_config(self) -> dict:
        try:
            amount = int(self.pay_amount_var.get().strip())
        except ValueError:
            amount = DEFAULT_PAY_AMOUNT
        return {"chain": self.chain_var.get(), "wif": self.wif_var.get().strip(),
                "message": self.text.get("1.0", "end-1c"), "pay_enabled": self.pay_enabled_var.get(),
                "pay_address": self.pay_address_var.get().strip(), "pay_amount": amount,
                "fee_sat_per_kb": self.fee_sat_per_kb}

    def run_in_background(self, work, on_done) -> None:
        """Operacje sieciowe w wątku, żeby okno się nie zawieszało. Wynik wraca przez kolejkę
        do głównego wątku (tkinter nie lubi wywołań z innych wątków)."""
        self.busy = True
        self.calc_btn.config(state="disabled")
        self.send_btn.config(state="disabled")

        def target():
            result = error = None
            try:
                result = work()
            except ChainError as e:
                error = str(e)
            except Exception as e:  # nieprzewidziany błąd pokazujemy w oknie zamiast zamykać program
                error = f"{type(e).__name__}: {e}"
            self.results.put((on_done, result, error, True))

        threading.Thread(target=target, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            while True:
                on_done, result, error, tracked = self.results.get_nowait()
                if tracked:  # operacja główna (koszt / wysyłka) - odblokuj przyciski
                    self.busy = False
                    self.calc_btn.config(state="normal")
                on_done(result, error)
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    # --- saldo ---
    def refresh_balance(self) -> None:
        self._balance_job = None
        try:
            key = parse_wif(self.wif_var.get())
            net = self.net()
            address = address_for(key, net)
        except ChainError:
            self.balance_var.set("Saldo: —")
            return
        self._balance_seq += 1
        seq = self._balance_seq
        self.balance_var.set("Saldo: sprawdzam...")

        def work():
            try:
                return (fetch_balance(address, net), fetch_utxo_report(address, net)), None
            except ChainError as e:
                return None, str(e)
            except Exception as e:
                return None, f"{type(e).__name__}: {e}"

        def done(result, _error):
            if seq != self._balance_seq:  # przyszła nowsza prośba (zmiana sieci/klucza) - ta odpowiedź jest nieaktualna
                return
            balances, error = result
            if error:
                self.balance_var.set(f"Saldo: błąd ({error})")
            else:
                self.balance_var.set(format_balance(*balances))

        threading.Thread(target=lambda: self.results.put((done, work(), None, False)), daemon=True).start()

    # --- schowek, linki ---
    def _copy(self, text: str, status: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.set_status(status)

    def on_copy_address(self) -> None:
        address = self.address_var.get()
        if address and not address.startswith("("):
            self._copy(address, "Skopiowano adres do schowka.")

    def on_copy_key(self) -> None:
        wif = self.wif_var.get().strip()
        if wif:
            self._copy(wif, "Skopiowano klucz do schowka. Schowaj go w bezpiecznym miejscu.")

    def on_copy_txid(self) -> None:
        if self.txid_var.get():
            self._copy(self.txid_var.get(), "Skopiowano TXID do schowka.")

    def on_open_faucet(self) -> None:
        webbrowser.open(FAUCET_URL)
        self.set_status("Wklej na kranie swój adres testnetowy (przycisk „Kopiuj adres”), a potem odśwież saldo.")

    def on_open_explorer(self) -> None:
        if self.txid_var.get():
            webbrowser.open(self._last_explorer_url or explorer_tx_url(self.txid_var.get(), self.net()))

    # --- akcje ---
    def on_save_key(self) -> None:
        try:
            parse_wif(self.wif_var.get())
            save_config(self.current_config())
        except ChainError as e:
            messagebox.showerror("Błąd", str(e))
            return
        self.set_status(f"Zapisano do {CONFIG_PATH.name}.")

    def on_generate_key(self) -> None:
        net = self.net()
        if self.wif_var.get().strip():
            if not messagebox.askyesno(
                    "Nowy klucz",
                    "W polu jest już klucz. Nowy klucz go zastąpi, także w config.json.\n\n"
                    "Jeśli na adresie starego klucza są środki, a nie masz jego kopii, stracisz do nich dostęp.\n\n"
                    "Wygenerować nowy klucz?", icon="warning"):
                return
        self.wif_var.set(generate_key(net).wif())
        self.show_wif_var.set(True)
        self.wif_entry.config(show="")
        try:
            save_config(self.current_config())
        except ChainError as e:
            self.set_status(f"Wygenerowano klucz ({net['name']}), ale zapis się nie udał: {e}")
            return
        self.set_status(f"Wygenerowano i zapisano nowy klucz ({net['name']}). Zrób jego kopię (przycisk „Kopiuj”).")

    def on_reset_payment(self) -> None:
        self.pay_address_var.set(DEFAULT_PAY_ADDRESS)
        self.pay_amount_var.set(str(DEFAULT_PAY_AMOUNT))

    def on_calculate(self) -> None:
        try:
            key = parse_wif(self.wif_var.get())
            net = self.net()
            pay_address, pay_amount = None, 0
            if self.pay_enabled_var.get():
                pay_address = self.pay_address_var.get().strip()
                ok, message = check_recipient(pay_address, net)
                if not ok:
                    raise ChainError(f"Adres odbiorcy: {message}")
                pay_amount = parse_amount(self.pay_amount_var.get())
            save_config(self.current_config())  # "zapisz" - klucz, sieć, płatność i tekst
        except ChainError as e:
            messagebox.showerror("Błąd", str(e))
            return
        text = self.text.get("1.0", "end-1c")
        self.cost_note_var.set("Liczę koszt (pobieram UTXO z WhatsOnChain)...")
        self.set_status("")

        def done(prepared, error):
            if error:
                self.cost_note_var.set("")
                self.set_status(f"Błąd: {error}")
                messagebox.showerror("Nie udało się obliczyć kosztu", error)
                return
            self.prepared = prepared
            v = self.cost_vars
            v["Płatność"].set(f"{prepared.pay_amount} sat  →  {prepared.pay_address}" if prepared.pay_address else "brak (tylko tekst)")
            v["Opłata"].set(f"{prepared.fee} sat  ({prepared.tx_size} B, stawka {self.fee_sat_per_kb} sat/kB)")
            v["Łącznie z konta"].set(f"{prepared.fee + prepared.pay_amount} sat")
            v["Użyte UTXO"].set(f"{prepared.inputs_used} z {prepared.inputs_total}  (razem {prepared.inputs_value} sat)")
            v["Reszta"].set(f"{prepared.change} sat  wraca na Twój adres")
            self.cost_note_var.set("UWAGA: reszta była za mała i przepada dla górników." if prepared.change_dropped else "")
            self.send_btn.config(state="normal")
            self.set_status("Gotowe do wysłania. Zmiana sieci, klucza, płatności lub tekstu wymaga ponownego obliczenia.")

        self.run_in_background(
            lambda: prepare_transaction(text, key, net, self.fee_sat_per_kb, pay_address, pay_amount), done)

    def on_send(self) -> None:
        prepared = self.prepared
        if prepared is None or self.busy:
            return
        net = get_network(prepared.chain)
        lines = [f"Wysłać na {net['name']}?", ""]
        if prepared.pay_address:
            lines.append(f"Płatność: {prepared.pay_amount} sat na {prepared.pay_address}")
        lines += [f"Opłata: {prepared.fee} sat", f"Tekst: {prepared.payload_bytes} B (publiczny i nieodwracalny)"]
        question = "\n".join(lines)
        if net["name"] == "mainnet":
            answer = simpledialog.askstring("Mainnet - prawdziwe satoshi",
                                            f"{question}\n\nAby wysłać, wpisz dokładnie:\n{MAINNET_PHRASE}", parent=self)
            if answer is None or answer.strip() != MAINNET_PHRASE:
                self.set_status("Anulowano, nic nie wysłano.")
                return
        elif not messagebox.askyesno("Wyślij", question):
            self.set_status("Anulowano, nic nie wysłano.")
            return

        self.cost_note_var.set("Wysyłam...")

        def done(result, error):
            self.prepared = None  # tej transakcji nie wysyłamy drugi raz
            self.invalidate()
            if error:
                self.set_status(f"Błąd wysyłania: {error}")
                return
            if not result.ok:
                self.set_status(f"Węzeł odrzucił transakcję ({result.status}): {result.message}")
                return
            self.txid_var.set(result.txid)
            self.open_btn.config(state="normal")
            self._last_explorer_url = result.explorer_url
            self.set_status(f"Wysłano na {net['name']}. {result.message or ''}".strip())
            self.refresh_balance()

        self.run_in_background(lambda: run_async(broadcast_async(prepared.tx, net, dry_run=False)), done)


def main() -> None:
    if sys.platform == "win32":  # ostrzejszy tekst na ekranach o wysokiej rozdzielczości
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
