"""
bsv_common.py - wspólne pomocniki do programów na bsv-sdk (sprawdzone na 2.4.0).

Kopiuj ten plik do projektu obok skryptu i importuj z niego, zamiast wklejać
te same bloki kodu w każdym skrypcie. Identyfikatory są po angielsku,
komunikaty dla użytkownika po polsku.

Zasady, które ten moduł wymusza:
  * sieć wybiera się w jednym miejscu (CHAIN), domyślnie testnet,
  * klucz prywatny pochodzi wyłącznie ze zmiennej środowiskowej i nigdy
    nie trafia do komunikatów błędów,
  * nie ma ślepego wyboru pierwszego UTXO (filtr mempoolu, ordinali i kwoty),
  * po tx.fee() sprawdzamy, czy środków naprawdę wystarczyło,
  * domyślnie DRY_RUN=True, a mainnet wymaga osobnego, świadomego potwierdzenia.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# KONFIGURACJA - zmieniaj tylko tutaj
# ---------------------------------------------------------------------------

CHAIN = "testnet"            # "testnet" (domyślnie) albo "mainnet" (prawdziwe satoshi)
DRY_RUN = True               # True = zbuduj i podpisz, ale NIE rozgłaszaj
KEY_ENV_VAR = "BSV_WIF"      # NAZWA zmiennej środowiskowej z kluczem WIF
ORDINAL_MAX_VALUE = 1        # UTXO o tej wartości (lub mniejszej) może nieść ordinal / token
DEFAULT_SAT_PER_KB = 100     # tylko do szacowania rezerwy przy wyborze UTXO (patrz known-gaps.md)

# Ta sama sieć nazywa się inaczej w SDK, w WhatsOnChain i w adresie (prefiks base58).
NETWORKS = {
    "mainnet": {"name": "mainnet", "sdk": "mainnet", "woc": "main",
                "explorer": "https://whatsonchain.com", "address_prefix": 0x00},
    "testnet": {"name": "testnet", "sdk": "testnet", "woc": "test",
                "explorer": "https://test.whatsonchain.com", "address_prefix": 0x6F},
}
_ALIASES = {"main": "mainnet", "test": "testnet"}

# Szacunkowe rozmiary (bajty) zwykłej transakcji P2PKH - tylko do szacowania rezerwy.
_TX_OVERHEAD = 10
_P2PKH_INPUT = 148
_P2PKH_OUTPUT = 34


class ChainError(Exception):
    """Błąd, który pokazujemy użytkownikowi (bez stacktrace). Nigdy nie zawiera klucza."""


class InsufficientFunds(ChainError):
    """Wybrane UTXO nie pokrywają kwoty i opłaty (można spróbować dobrać kolejne)."""


# ---------------------------------------------------------------------------
# SIEĆ I KLUCZ
# ---------------------------------------------------------------------------

def get_network(chain: str | None = None) -> dict:
    """Zwraca ustawienia sieci. Zła nazwa = czytelny błąd, a nie cichy mainnet."""
    name = str(CHAIN if chain is None else chain).strip().lower()
    name = _ALIASES.get(name, name)
    if name not in NETWORKS:
        raise ChainError(f'Nieznana sieć "{chain or CHAIN}". Dozwolone: "testnet" albo "mainnet".')
    return NETWORKS[name]


def read_private_key(net: dict | None = None, env_var: str | None = None, allow_cross_network: bool = False):
    """
    Wczytuje klucz WIF ze zmiennej środowiskowej. Klucz nie jest nigdzie wypisywany.
    Klucz z innej sieci (np. mainnetowy WIF przy CHAIN="testnet") jest odrzucany,
    żeby nie pomylić, na jakiej sieci działamy.
    """
    net = net or get_network()
    env_var = env_var or KEY_ENV_VAR
    try:
        from bsv.keys import PrivateKey
    except ImportError as e:
        raise ChainError("Brak biblioteki bsv-sdk. Zainstaluj: pip install bsv-sdk") from e

    wif = os.environ.get(env_var, "").strip()
    if not wif:
        raise ChainError(f"Brak klucza prywatnego. Ustaw zmienną środowiskową {env_var} "
                         f"(np. w Windows: set {env_var}=twoj_klucz_wif) i uruchom program ponownie.")
    try:
        key = PrivateKey(wif)
    except Exception:
        # celowo bez szczegółów wyjątku - mógłby zawierać fragment klucza
        raise ChainError("Klucz prywatny w zmiennej środowiskowej jest niepoprawny "
                         "(sprawdź, czy skopiowałeś go w całości).") from None

    key_net = getattr(key.network, "value", str(key.network))
    if key_net != net["sdk"] and not allow_cross_network:
        raise ChainError(f"Klucz WIF jest z sieci {key_net}, a program działa na {net['name']}. "
                         f"Użyj klucza właściwej sieci albo zmień CHAIN.")
    return key


def validate_address(address: str, net: dict | None = None) -> bool:
    """Sprawdza długość, sumę kontrolną i prefiks adresu P2PKH dla wybranej sieci."""
    net = net or get_network()
    try:
        from bsv.base58 import base58check_decode
        decoded = base58check_decode(address)
        return len(decoded) == 21 and decoded[0] == net["address_prefix"]
    except Exception:
        return False


def explorer_tx_url(txid: str, net: dict | None = None) -> str:
    return f'{(net or get_network())["explorer"]}/tx/{txid}'


# ---------------------------------------------------------------------------
# WHATSONCHAIN
# ---------------------------------------------------------------------------

def woc_url(path: str, net: dict | None = None) -> str:
    return f'https://api.whatsonchain.com/v1/bsv/{(net or get_network())["woc"]}/{path.lstrip("/")}'


def http_get(url: str, as_text: bool = False, timeout: int = 20):
    """GET z limitem czasu i sprawdzeniem kodu odpowiedzi; błędy zamieniamy na ChainError."""
    try:
        import requests
    except ImportError as e:
        raise ChainError("Brak biblioteki requests. Zainstaluj: pip install requests") from e
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text.strip() if as_text else response.json()
    except requests.RequestException as e:
        raise ChainError(f"Błąd połączenia z WhatsOnChain: {e}") from e


@dataclass(frozen=True)
class Utxo:
    txid: str
    vout: int
    value: int
    height: int | None = None


def fetch_balance(address: str, net: dict | None = None) -> int:
    """Saldo POTWIERDZONE w satoshi (endpoint confirmed/balance)."""
    return int(http_get(woc_url(f"address/{address}/confirmed/balance", net))["confirmed"])


def fetch_utxos(address: str, net: dict | None = None, include_small: bool = False) -> list[Utxo]:
    """
    Niewydane UTXO adresu z endpointu unspent/all (potwierdzone i niepotwierdzone).
    Odrzuca: wpłaty już wydane w mempoolu oraz (domyślnie) UTXO o wartości
    <= ORDINAL_MAX_VALUE, bo na tym samym adresie mogą leżeć NFT i tokeny 1Sat,
    których nie wolno przypadkiem wydać jako zwykłych środków.
    """
    result = http_get(woc_url(f"address/{address}/unspent/all", net)).get("result", [])
    utxos = []
    for item in result:
        if item.get("isSpentInMempoolTx"):
            continue
        value = int(item["value"])
        if not include_small and value <= ORDINAL_MAX_VALUE:
            continue
        utxos.append(Utxo(item["tx_hash"], int(item["tx_pos"]), value, item.get("height")))
    return utxos


def fetch_tx_hex(txid: str, net: dict | None = None) -> str:
    return http_get(woc_url(f"tx/{txid}/hex", net), as_text=True)


# ---------------------------------------------------------------------------
# WYBÓR UTXO I BUDOWA WEJŚĆ
# ---------------------------------------------------------------------------

def estimate_size(n_inputs: int, n_outputs: int, extra_bytes: int = 0) -> int:
    return _TX_OVERHEAD + _P2PKH_INPUT * n_inputs + _P2PKH_OUTPUT * n_outputs + extra_bytes


def select_utxos(utxos: list[Utxo], amount: int, n_outputs: int = 2, extra_bytes: int = 0,
                 sat_per_kb: int = DEFAULT_SAT_PER_KB) -> list[Utxo]:
    """
    Wybiera UTXO od największego, aż suma pokryje kwotę oraz szacowaną opłatę.
    To tylko podpowiedź - ostateczną kontrolę robi finalize_transaction() po tx.fee().
    extra_bytes: dodatkowy rozmiar wyjść danych (np. długość skryptu OP_RETURN).
    """
    if amount < 0:
        raise ChainError("Kwota nie może być ujemna.")
    selected, total = [], 0
    for utxo in sorted(utxos, key=lambda u: u.value, reverse=True):
        selected.append(utxo)
        total += utxo.value
        reserve = math.ceil(estimate_size(len(selected), n_outputs, extra_bytes) * sat_per_kb / 1000) + 5
        if total >= amount + reserve:
            return selected
    needed = f"{amount} sat plus opłata" if amount else "środków na opłatę"
    raise ChainError(f"Za mało środków: do dyspozycji {total} sat, a potrzeba {needed}. "
                     f"(UTXO po {ORDINAL_MAX_VALUE} sat lub mniej oraz wydane w mempoolu są pomijane.)")


def make_inputs(utxos: list[Utxo], private_key, net: dict | None = None) -> list:
    """Buduje TransactionInput dla każdego UTXO (pobiera hex każdej transakcji źródłowej raz)."""
    from bsv.script.type import P2PKH
    from bsv.transaction import Transaction
    from bsv.transaction_input import TransactionInput

    cache: dict[str, Transaction] = {}
    inputs = []
    for utxo in utxos:
        if utxo.txid not in cache:
            source = Transaction.from_hex(fetch_tx_hex(utxo.txid, net))
            if source is None:
                raise ChainError(f"Nie udało się odczytać transakcji źródłowej {utxo.txid} (zły hex).")
            cache[utxo.txid] = source
        source = cache[utxo.txid]
        if utxo.vout >= len(source.outputs) or source.outputs[utxo.vout].satoshis != utxo.value:
            raise ChainError(f"Dane UTXO {utxo.txid}:{utxo.vout} nie zgadzają się z transakcją źródłową.")
        inputs.append(TransactionInput(
            source_transaction=source,
            source_output_index=utxo.vout,
            unlocking_script_template=P2PKH().unlock(private_key)))
    return inputs


# ---------------------------------------------------------------------------
# OPŁATA, PODPIS, ROZGŁOSZENIE
# ---------------------------------------------------------------------------

@dataclass
class FeeReport:
    required: int          # opłata policzona przez model
    actual: int            # wejścia minus wyjścia (to faktycznie zapłacisz)
    change_dropped: bool   # reszta była za mała i SDK ją usunął
    extra_to_miners: int   # ile ponad wymaganą opłatę przepada dla górników


def finalize_transaction(tx, fee_model=None, max_fee: int | None = None) -> FeeReport:
    """
    tx.fee() + kontrola środków + tx.sign().
    fee_model: None (domyślnie LivePolicy z SDK), liczba całkowita (stała opłata w satoshi)
    albo np. SatoshisPerKilobyte(n). Przy zbyt małym UTXO SDK nie rzuca błędu
    - dopiero ta kontrola wyłapuje brak środków przed rozgłoszeniem.
    """
    had_change = any(out.change for out in tx.outputs)
    try:
        required = tx.fee(fee_model)
    except ValueError as e:
        raise ChainError(f"Nie można policzyć opłaty: {e}") from e
    actual = tx.get_fee()
    if actual < required:
        raise InsufficientFunds(f"Za mało środków na pokrycie kwoty i opłaty (brakuje {required - actual} sat).")
    if max_fee is not None and actual > max_fee:
        raise ChainError(f"Opłata {actual} sat przekracza ustawiony limit {max_fee} sat.")
    tx.sign()
    dropped = had_change and not any(out.change for out in tx.outputs)
    return FeeReport(required, actual, dropped, actual - required)


@dataclass
class BroadcastResult:
    ok: bool
    status: str
    txid: str | None
    message: str | None
    dry_run: bool = False
    explorer_url: str | None = None


async def broadcast_async(tx, net: dict | None = None, dry_run: bool | None = None, broadcaster=None) -> BroadcastResult:
    """
    Rozgłasza transakcję (albo tylko ją pokazuje przy dry_run).
    BroadcastResponse (sukces) ma pole message, BroadcastFailure (porażka) ma description,
    więc sprawdzamy status, a dopiero potem czytamy właściwe pole.
    """
    net = net or get_network()
    dry_run = DRY_RUN if dry_run is None else dry_run
    txid = tx.txid()
    if dry_run:
        return BroadcastResult(True, "dry-run", txid, "Tryb testowy - nic nie wysłano.", True)
    if broadcaster is None:
        try:
            from bsv.broadcasters import WhatsOnChainBroadcaster
        except ImportError as e:
            raise ChainError("Brak biblioteki bsv-sdk. Zainstaluj: pip install bsv-sdk") from e
        broadcaster = WhatsOnChainBroadcaster(net["woc"])
    response = await tx.broadcast(broadcaster)
    status = str(getattr(response, "status", "")).lower()
    ok = status == "success"
    text = getattr(response, "message", None) if ok else getattr(response, "description", None)
    return BroadcastResult(ok, status, txid, text, False, explorer_tx_url(txid, net) if ok else None)


def run_async(coro):
    """
    Uruchamia korutynę w osobnym wątku z własną pętlą zdarzeń. Działa w zwykłym skrypcie,
    w IDLE/Jupyterze i w aplikacji webowej (np. Django), bez nest_asyncio.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------------------
# POTWIERDZENIE PRZED ZAPISEM NA ŁAŃCUCH
# ---------------------------------------------------------------------------

MAINNET_PHRASE = "WYSLIJ NA MAINNET"


def print_summary(summary_lines: list[str], net: dict | None = None) -> None:
    """Wypisuje podsumowanie tego, co ma pójść na łańcuch."""
    net = net or get_network()
    print(f"\n--- PODSUMOWANIE ({net['name']}) ---")
    for line in summary_lines:
        print(f"  {line}")


def confirm_action(summary_lines: list[str], net: dict | None = None) -> bool:
    """
    Pokazuje podsumowanie i prosi o potwierdzenie. Testnet: wpisz "ok".
    Mainnet: trzeba przepisać całą frazę, żeby nie dało się zatwierdzić odruchowo.
    """
    net = net or get_network()
    print_summary(summary_lines, net)
    if net["name"] == "mainnet":
        answer = input(f'\nTo prawdziwe satoshi. Aby wysłać, wpisz dokładnie "{MAINNET_PHRASE}": ').strip()
        return answer == MAINNET_PHRASE
    return input('\nWpisz "ok", aby wysłać: ').strip().lower() == "ok"
