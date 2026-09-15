"""Строгий разбор уведомлений BIDV о расходных операциях."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


@dataclass(frozen=True)
class Transaction:
    """Только поля, разрешённые для выгрузки."""

    transaction_type: str
    amount_vnd: int
    recipient_type: str
    recipient_name: str
    transaction_at: str
    reference_number: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "transaction_type": self.transaction_type,
            "amount_vnd": self.amount_vnd,
            "recipient_type": self.recipient_type,
            "recipient_name": self.recipient_name,
            "transaction_at": self.transaction_at,
            "reference_number": self.reference_number,
        }


_FIELD_END = r"\s*(?:\r?\n|$)"


def _value_after_label(text: str, *labels: str) -> str | None:
    """Возвращает первую непустую строку после двуязычной метки."""
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*:\s*(.*?){_FIELD_END}",
            text,
            flags=re.IGNORECASE,
        )
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _amount_after_label(text: str, *labels: str) -> int | None:
    """Извлекает VND после метки, допуская пояснительные строки между ними."""
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*:\s*(?:[^\n]*\n){{0,5}}?[^\n]*?([\d,.]+)\s*VND\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            digits = re.sub(r"[^0-9]", "", match.group(1))
            return int(digits)
    return None


def _first_label_value(text: str, *labels: str) -> str | None:
    """Берёт значение после англоязычной метки, включая следующую строку."""
    value = _value_after_label(text, *labels)
    if value:
        return value
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*:\s*\n\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _field_value(text: str, vietnamese_label: str, english_label: str) -> str | None:
    """Сначала ищет английскую метку, затем вьетнамскую — у писем оба варианта."""
    return _first_label_value(text, english_label) or _first_label_value(text, vietnamese_label)


def _field_amount(text: str, vietnamese_label: str, english_label: str) -> int | None:
    """Сначала ищет английскую метку, затем вьетнамскую."""
    return _amount_after_label(text, english_label) or _amount_after_label(text, vietnamese_label)


def _normalized_type(text: str) -> str | None:
    """Название операции бывает на строке после метки на вьетнамском и английском."""
    english = _first_label_value(text, "Transaction type")
    vietnamese = _first_label_value(text, "Loại giao dịch")
    if english and english.casefold() not in {"transaction type"}:
        return english
    return vietnamese


def _dual_label_value(text: str, vietnamese_label: str, english_label: str) -> str | None:
    return _field_value(text, vietnamese_label, english_label)


def _dual_label_amount(text: str, vietnamese_label: str, english_label: str) -> int | None:
    return _field_amount(text, vietnamese_label, english_label)


def _read_transaction_type(text: str) -> str | None:
    """Возвращает вторую строку для формы «Loại ...:\nTransaction type: QR Pay»."""
    # Ищем двуязычную пару: вьетнамская метка + английская метка + значение
    match = re.search(
        r"Loại giao dịch\s*:\s*\n\s*Transaction type\s*:\s*([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return _normalized_type(text)


def _read_dual_field(text: str, vietnamese_label: str, english_label: str) -> str | None:
    match = re.search(
        rf"{re.escape(vietnamese_label)}\s*:\s*\n\s*{re.escape(english_label)}\s*:\s*([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return _dual_label_value(text, vietnamese_label, english_label)


def _read_dual_amount(text: str, vietnamese_label: str, english_label: str) -> int | None:
    match = re.search(
        rf"{re.escape(vietnamese_label)}\s*:\s*\n\s*{re.escape(english_label)}\s*:\s*(?:[^\n]*\n){{0,5}}?[^\n]*?([\d,.]+)\s*VND\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return int(re.sub(r"[^0-9]", "", match.group(1)))
    return _dual_label_amount(text, vietnamese_label, english_label)


def _read_time(text: str) -> str | None:
    return _read_dual_field(text, "Thời gian giao dịch", "Transaction time")


def _read_reference(text: str) -> str | None:
    return _read_dual_field(text, "Số tham chiếu", "Reference number")


def _read_beneficiary(text: str) -> str | None:
    return _read_dual_field(text, "Tên người thụ hưởng", "Beneficiary name")


def _read_merchant(text: str) -> str | None:
    return _first_label_value(text, "Merchant Name")


def _read_transfer_amount(text: str) -> int | None:
    return _read_dual_amount(text, "Số tiền giao dịch", "Transaction amount")


def _read_qr_amount(text: str) -> int | None:
    return _dual_label_amount(text, "Số tiền", "Amount")


def _require(value: str | int | None, field: str) -> str | int:
    if value is None or value == "":
        raise ValueError(f"Не найдено обязательное поле: {field}")
    return value


def _parse_datetime(value: str) -> str:
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M:%S").isoformat()
    except ValueError as error:
        raise ValueError(f"Неизвестный формат даты операции: {value!r}") from error


def parse_bidv_notification(text: str) -> Transaction:
    """Разбирает только три подтверждённых типа письма BIDV.

    Не возвращает счета, номера карт, IP-адреса, ОС или полный текст письма.
    Неугаданные шаблоны намеренно завершаются ошибкой и не выгружаются.
    """
    normalized = text.replace(" ", " ").replace("\r\n", "\n")
    transaction_type = _require(_read_transaction_type(normalized), "Transaction type")
    transaction_time = _require(_read_time(normalized), "Transaction time")
    reference_number = _require(_read_reference(normalized), "Reference number")

    type_text = str(transaction_type).casefold()
    is_qr = "qr" in type_text or "thanh toán" in type_text
    is_transfer = "transfer" in type_text or "chuyển tiền" in type_text
    is_within_bidv = "within" in type_text or "nội bộ" in type_text
    if is_qr:
        amount = _require(_read_qr_amount(normalized), "Amount")
        recipient_name = _require(_read_merchant(normalized), "Merchant Name")
        parsed_type = "qr_pay"
        recipient_type = "merchant"
    elif is_transfer:
        amount = _require(_read_transfer_amount(normalized), "Transaction amount")
        recipient_name = _require(_read_beneficiary(normalized), "Beneficiary name")
        parsed_type = "within_bidv_transfer" if is_within_bidv else "interbank_transfer"
        recipient_type = "beneficiary"
    else:
        raise ValueError(f"Неподдерживаемый тип операции: {transaction_type!r}")

    return Transaction(
        transaction_type=parsed_type,
        amount_vnd=int(amount),
        recipient_type=recipient_type,
        recipient_name=str(recipient_name),
        transaction_at=_parse_datetime(str(transaction_time)),
        reference_number=str(reference_number),
    )
