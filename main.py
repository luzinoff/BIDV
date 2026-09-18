"""Собирает новые уведомления BIDV из Яндекс Почты и сохраняет JSON в репозиторий."""

from __future__ import annotations

import email
from email.header import decode_header, make_header
from email.message import Message
import imaplib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Iterator

from parser import Transaction, parse_bidv_notification

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOGGER = logging.getLogger(__name__)

IMAP_HOST = "imap.yandex.ru"
STATE_FILE_NAME = "processed_uids.json"
TRANSACTIONS_DIR = "transactions"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задан обязательный секрет или параметр: {name}")
    return value


def configured_senders() -> set[str]:
    """Возвращает список разрешённых отправителей из MAIL_SENDER.

    Для совместимости принимается как один адрес, так и несколько адресов
    через запятую или пробел.
    """
    raw = required_env("MAIL_SENDER")
    senders = {item.casefold() for item in re.split(r"[,;\s]+", raw) if item}
    if not senders:
        raise RuntimeError("Не задан ни один отправитель в MAIL_SENDER")
    return senders


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def text_from_message(message: Message) -> str:
    """Получает text/plain; HTML — только как безопасный запасной вариант."""
    parts: Iterator[Message]
    if message.is_multipart():
        parts = (part for part in message.walk() if not part.is_multipart())
    else:
        parts = iter((message,))

    fallback_html: str | None = None
    for part in parts:
        disposition = part.get_content_disposition()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(charset, errors="replace")
        except LookupError:
            content = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            return content
        fallback_html = content

    if fallback_html:
        # Уведомления BIDV табличные; разделители превращаем в переводы строк.
        text = re.sub(r"<(?:br|/p|/div|/tr|/li)\b[^>]*>", "\n", fallback_html, flags=re.I)
        return re.sub(r"<[^>]+>", "", text)
    raise ValueError("В письме нет текстовой части")


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError("ожидался список UID")
        return set(raw)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать журнал обработанных писем: {error}") from error


def save_state(path: Path, uids: set[str]) -> None:
    path.write_text(json.dumps(sorted(uids), ensure_ascii=False), encoding="utf-8")


def save_transaction(transactions_dir: Path, transaction: Transaction) -> str:
    """Сохраняет транзакцию в JSON-файл."""
    transactions_dir.mkdir(exist_ok=True)
    filename = f"bidv-{transaction.reference_number}.json"
    file_path = transactions_dir / filename

    if file_path.exists():
        return "already_exists"

    file_path.write_text(
        json.dumps(transaction.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return "saved"


def _imap_response_text(data: list[object]) -> str:
    """Делает диагностический ответ IMAP пригодным для лога."""
    parts: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", errors="replace"))
        elif item is not None:
            parts.append(str(item))
    return " ".join(parts).strip() or "пустой ответ сервера"


def search_sender_uids(mail: imaplib.IMAP4_SSL, senders: set[str]) -> list[bytes]:
    """Ищет UID всех разрешённых отправителей в открытой папке.

    Если сервер отвергает SEARCH FROM, запасной поиск ALL остаётся ограничен
    выбранной папкой. Точная проверка заголовка From выполняется перед разбором.
    """
    found: set[bytes] = set()
    failed: list[str] = []
    for sender in sorted(senders):
        result, data = mail.uid("search", None, "FROM", f'"{sender}"')
        if result == "OK":
            if data and data[0]:
                found.update(data[0].split())
        else:
            failed.append(sender)
            LOGGER.warning(
                "IMAP-поиск по отправителю %s не выполнен (%s: %s).",
                sender,
                result,
                _imap_response_text(data),
            )

    if not failed:
        return sorted(found, key=lambda value: int(value))

    LOGGER.warning(
        "Перехожу к поиску всех писем только в открытой папке для проверки "
        "отправителей: %s.",
        ", ".join(failed),
    )
    result, data = mail.uid("search", None, "ALL")
    if result != "OK":
        raise RuntimeError(
            "Не удалось получить список писем из открытой папки "
            f"({result}: {_imap_response_text(data)})"
        )
    return data[0].split() if data and data[0] else []


def sender_list_text(senders: set[str]) -> str:
    return ", ".join(sorted(senders))


def parse_sender_addresses(from_header: str) -> list[str]:
    return re.findall(r"[\w.+-]+@[\w.-]+", from_header.casefold())


def sender_matches(from_header: str, senders: set[str]) -> bool:
    return bool(set(parse_sender_addresses(from_header)).intersection(senders))


def main() -> int:
    LOGGER.info("Запуск сборщика...")
    senders = configured_senders()
    password = required_env("YANDEX_APP_PASSWORD")
    mail_folder = os.getenv("YANDEX_MAIL_FOLDER", "INBOX").strip() or "INBOX"
    state_path = Path(os.getenv("STATE_FILE", STATE_FILE_NAME))
    transactions_dir = Path(os.getenv("TRANSACTIONS_DIR", TRANSACTIONS_DIR))
    processed_uids = load_state(state_path)

    LOGGER.info("Подключение к %s...", IMAP_HOST)
    mail = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
    newly_processed: set[str] = set()
    try:
        LOGGER.info("Авторизация в почте...")
        mail.login(required_env("YANDEX_EMAIL"), password)
        LOGGER.info("Авторизация успешна")
        LOGGER.info("Открытие папки '%s'...", mail_folder)
        result, _ = mail.select(f'"{mail_folder}"', readonly=True)
        if result != "OK":
            raise RuntimeError(f"Не удалось открыть папку почты: {mail_folder}")
        LOGGER.info("Папка открыта")
        LOGGER.info("Поиск писем от %s...", sender_list_text(senders))
        all_uids = search_sender_uids(mail, senders)
        LOGGER.info("Найдено писем в выбранной папке: %d, уже обработано: %d", len(all_uids), len(processed_uids))

        for raw_uid in all_uids:
            uid = raw_uid.decode("ascii")
            if uid in processed_uids:
                continue
            LOGGER.info("Чтение письма UID %s...", uid)
            result, payload = mail.uid("fetch", uid, "(RFC822)")
            if result != "OK" or not payload or not isinstance(payload[0], tuple):
                LOGGER.warning("Не удалось прочитать письмо UID %s", uid)
                continue
            message = email.message_from_bytes(payload[0][1])
            from_header = decode_mime_header(message.get("From"))
            # Сравнение адреса, а не произвольной отображаемой строки.
            addresses = parse_sender_addresses(from_header)
            if not sender_matches(from_header, senders):
                LOGGER.info(
                    "Письмо UID %s от %s — пропущено (ждём %s)",
                    uid,
                    addresses,
                    sender_list_text(senders),
                )
                continue
            matched_senders = sorted(set(addresses).intersection(senders))
            LOGGER.info(
                "Письмо UID %s от нужного отправителя (%s), парсинг...",
                uid,
                ", ".join(matched_senders),
            )
            try:
                mail_text = text_from_message(message)
                transaction = parse_bidv_notification(mail_text)
                LOGGER.info("Распознано: %s на %d VND", transaction.transaction_type, transaction.amount_vnd)
                outcome = save_transaction(transactions_dir, transaction)
            except ValueError as error:
                preview = mail_text[:500] if 'mail_text' in locals() else "(не удалось извлечь текст)"
                LOGGER.warning("Письмо UID %s не выгружено: %s", uid, error)
                LOGGER.warning("Начало письма: %s", preview.replace('\n', ' | '))
                continue
            newly_processed.add(uid)
            LOGGER.info("UID %s: %s (%s)", uid, outcome, transaction.reference_number)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    if newly_processed:
        save_state(state_path, processed_uids | newly_processed)
    LOGGER.info("Обработано новых писем: %d", len(newly_processed))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        LOGGER.error("Сбор остановлен: %s", error)
        raise SystemExit(1)
