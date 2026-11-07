"""Разбор писем Bybit о выполненных P2P-ордерах (покупка и продажа)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

# Темы писем, которые нужно выгружать. Сравниваются без учёта регистра:
# у Bybit «выполнен» в покупке и «Выполнено» в продаже.
SUBJECT_SIDES = (
    ("p2p-ордер на покупку: выполнен", "buy"),
    ("p2p-ордер на продажу: выполнено", "sell"),
)


@dataclass(frozen=True)
class P2POrder:
    side: str  # buy — купили крипту за фиат, sell — продали крипту за фиат
    fiat_amount: float
    fiat_currency: str
    crypto_amount: float
    crypto: str
    created_at: str  # ISO 8601 в UTC, с суффиксом Z
    order_tail: str  # видимые последние цифры ID ордера

    @property
    def file_id(self) -> str:
        stamp = self.created_at.replace("-", "").replace(":", "").rstrip("Z")
        return f"{stamp}-{self.order_tail}"

    def as_dict(self) -> dict[str, object]:
        fiat: float | int = self.fiat_amount
        if float(fiat).is_integer():
            fiat = int(fiat)
        return {
            "side": self.side,
            "fiat_amount": fiat,
            "fiat_currency": self.fiat_currency,
            "crypto_amount": self.crypto_amount,
            "crypto": self.crypto,
            "created_at": self.created_at,
            "order_tail": self.order_tail,
        }


def side_from_subject(subject: str) -> str | None:
    """Возвращает buy/sell для нужных тем, иначе None."""
    folded = re.sub(r"\s+", " ", subject).strip().casefold()
    for marker, side in SUBJECT_SIDES:
        if marker in folded:
            return side
    return None


def _number(raw: str) -> float:
    """Понимает 35150, 9,000,000, 9 000 000, 411.1591 и 411,1591."""
    value = re.sub(r"[\s  ]", "", raw)
    if "," in value and "." in value:
        value = value.replace(",", "")
    elif "," in value:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", value):
            value = value.replace(",", "")
        else:
            value = value.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", value):
        raise ValueError(f"не число: {raw!r}")
    return float(value)


def _field(text: str, label: str) -> str:
    match = re.search(rf"{label}\s*:\s*([^\n]+)", text, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"нет поля «{label}»")
    return match.group(1).strip()


def _amount_with_code(text: str, label: str) -> tuple[float, str]:
    raw = _field(text, label)
    match = re.fullmatch(r"([\d\s  .,]+?)\s*([A-Za-z]{2,10})", raw)
    if not match:
        raise ValueError(f"не удалось разобрать «{label}»: {raw!r}")
    return _number(match.group(1)), match.group(2).upper()


def parse_bybit_p2p(subject: str, body: str) -> P2POrder:
    """Разбирает письмо; бросает ValueError, если формат незнакомый."""
    side = side_from_subject(subject)
    if side is None:
        raise ValueError(f"тема не из списка выгружаемых: {subject!r}")

    text = body.replace("\r", "")
    body_folded = re.sub(r"\s+", " ", text).casefold()
    expected = "на покупку выполнен" if side == "buy" else "на продажу выполнен"
    if expected not in body_folded:
        raise ValueError("текст письма не подтверждает тип ордера из темы")

    order_raw = _field(text, "ID ордера")
    tail = re.search(r"(\d{3,})\s*$", order_raw)
    if not tail:
        raise ValueError(f"не удалось взять цифры ID ордера: {order_raw!r}")

    created_raw = _field(text, "Время создания ордера")
    created = re.match(
        r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*\(UTC\s*\+\s*0\)",
        created_raw,
        flags=re.IGNORECASE,
    )
    if not created:
        raise ValueError(f"незнакомый формат времени: {created_raw!r}")
    created_at = datetime.fromisoformat(f"{created.group(1)}T{created.group(2)}")

    fiat_amount, fiat_currency = _amount_with_code(text, "Сумма в фиате")
    crypto_amount, crypto = _amount_with_code(text, "Сумма криптовалюты")
    if fiat_amount <= 0 or crypto_amount <= 0:
        raise ValueError("сумма ордера должна быть больше нуля")

    return P2POrder(
        side=side,
        fiat_amount=fiat_amount,
        fiat_currency=fiat_currency,
        crypto_amount=crypto_amount,
        crypto=crypto,
        created_at=created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        order_tail=tail.group(1),
    )
