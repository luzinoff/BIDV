"""Собирает подтверждённые уведомления BIDV из Яндекс Почты в JSON-файлы."""

from __future__ import annotations

import email
from email.header import decode_header, make_header
from email.message import Message
import html
import imaplib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Iterator

from bybit_parser import P2POrder, parse_bybit_p2p, side_from_subject
from parser import Transaction, parse_bidv_notification

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOGGER = logging.getLogger(__name__)

IMAP_HOST = "imap.yandex.ru"
STATE_FILE_NAME = "processed_uids.json"
TRANSACTIONS_DIR = "transactions"
BYBIT_STATE_FILE_NAME = "processed_uids_bybit.json"
BYBIT_DIR = "bybit"
BYBIT_DEFAULT_FOLDER = "Bybit"
BYBIT_DEFAULT_SENDER = "noreply@email-service.bybit.com"
CONNECT_ATTEMPTS = 3
CONNECT_PAUSE_SECONDS = 15


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задан обязательный секрет или параметр: {name}")
    return value


def configured_senders() -> set[str]:
    """Читает один или несколько адресов из MAIL_SENDER."""
    raw = required_env("MAIL_SENDER")
    senders = {item.casefold() for item in re.split(r"[,;\s]+", raw) if item}
    if not senders:
        raise RuntimeError("Не задан ни один отправитель в MAIL_SENDER")
    return senders


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _html_to_text(source: str) -> str:
    """Убирает разметку HTML, сохраняя строки и ячейки таблицы."""
    # Технический CSS не должен попадать в извлечённый текст письма.
    text = re.sub(
        r"<(?:style|script)\b[^>]*>.*?</(?:style|script)>\s*",
        "\n",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<(?:br|p|/p|div|/div|tr|/tr|li|/li|td|/td|th|/th)\b[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ")


def text_from_message(message: Message) -> str:
    """Извлекает тело письма, предпочитая содержательную HTML-версию."""
    parts: Iterator[Message]
    if message.is_multipart():
        parts = (part for part in message.walk() if not part.is_multipart())
    else:
        parts = iter((message,))

    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in parts:
        if part.get_content_disposition() == "attachment":
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
        if content_type == "text/html":
            html_parts.append(content)
        elif content.strip():
            plain_parts.append(content)

    # Уведомления BIDV нередко имеют пустую text/plain часть и полноценную
    # таблицу HTML. Поэтому HTML проверяется первым.
    for source in html_parts:
        content = _html_to_text(source)
        if content.strip():
            return content
    if plain_parts:
        return max(plain_parts, key=len)
    raise ValueError("В письме нет непустой текстовой части")


def _body_preview(body: str, limit: int = 500) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    return compact[:limit] or "(пустое содержимое)"


def message_subject(message: Message) -> str:
    return decode_mime_header(message.get("Subject"))


def is_known_transaction_format(message: Message, body: str) -> bool:
    """Определяет, похоже ли письмо на ожидаемое подтверждение операции."""
    subject = message_subject(message).casefold()
    body_folded = body.casefold()
    return any(
        marker in body_folded or marker in subject
        for marker in (
            "transaction type",
            "original amount",
            "approval code",
            "transaction amount",
            "merchant name",
            "successful transaction",
            "giao dịch thẻ thành công",
        )
    )


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
    """Сохраняет операцию; не перезаписывает уже существующий JSON."""
    transactions_dir.mkdir(exist_ok=True)
    file_path = transactions_dir / f"bidv-{transaction.reference_number}.json"
    if file_path.exists():
        return "already_exists"
    file_path.write_text(
        json.dumps(transaction.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "saved"


def _imap_response_text(data: list[object]) -> str:
    parts: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", errors="replace"))
        elif item is not None:
            parts.append(str(item))
    return " ".join(parts).strip() or "пустой ответ сервера"


def search_sender_uids(mail: imaplib.IMAP4_SSL, senders: set[str]) -> list[bytes]:
    """Получает все UID только из выбранной папки.

    Поиск FROM у Yandex иногда отвечает OK, но возвращает неполный результат.
    Поэтому папка BIDV сначала ограничивает область поиска, а точная проверка
    адреса From выполняется после чтения каждого письма.
    """
    LOGGER.info(
        "Получаю все письма из выбранной папки и затем проверяю отправителей: %s",
        ", ".join(sorted(senders)),
    )
    result, data = mail.uid("search", None, "ALL")
    if result != "OK":
        raise RuntimeError(
            "Не удалось получить список писем из открытой папки "
            f"({result}: {_imap_response_text(data)})"
        )
    return data[0].split() if data and data[0] else []


def _sorted_uids(uids: list[bytes]) -> list[bytes]:
    """Сортирует UID, не падая из-за неожиданного ответа IMAP."""
    return sorted(uids, key=lambda value: int(value) if value.isdigit() else value)


def parse_sender_addresses(from_header: str) -> list[str]:
    return re.findall(r"[\w.+-]+@[\w.-]+", from_header.casefold())


def sender_matches(from_header: str, senders: set[str]) -> bool:
    return bool(set(parse_sender_addresses(from_header)).intersection(senders))


def sender_list_text(senders: set[str]) -> str:
    return ", ".join(sorted(senders))


def read_message(mail: imaplib.IMAP4_SSL, uid: str) -> Message | None:
    result, payload = mail.uid("fetch", uid, "(RFC822)")
    if result != "OK" or not payload:
        return None
    for item in payload:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return email.message_from_bytes(item[1])
    return None


def save_bybit_order(orders_dir: Path, order: P2POrder) -> str:
    """Сохраняет P2P-ордер Bybit; не перезаписывает уже существующий JSON."""
    orders_dir.mkdir(exist_ok=True)
    file_path = orders_dir / f"bybit-{order.file_id}.json"
    if file_path.exists():
        return "already_exists"
    file_path.write_text(
        json.dumps(order.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "saved"


def connect(email_address: str, password: str) -> imaplib.IMAP4_SSL:
    """Подключается к Яндексу, повторяя попытку при обрыве соединения.

    Яндекс иногда рвёт соединение на CAPABILITY или LOGIN; через несколько
    секунд вход обычно проходит. Неверный пароль повторять бессмысленно.
    """
    last_error: Exception | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        mail: imaplib.IMAP4_SSL | None = None
        try:
            LOGGER.info("Подключение к %s (попытка %d)...", IMAP_HOST, attempt)
            mail = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
            LOGGER.info("Авторизация в почте...")
            mail.login(email_address, password)
            LOGGER.info("Авторизация успешна")
            return mail
        except imaplib.IMAP4.error as error:
            if "auth" in str(error).casefold() or "invalid" in str(error).casefold():
                raise
            last_error = error
        except (imaplib.IMAP4.abort, OSError) as error:
            last_error = error
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
        LOGGER.warning("Соединение с почтой не удалось: %s", last_error)
        if attempt < CONNECT_ATTEMPTS:
            time.sleep(CONNECT_PAUSE_SECONDS)
    raise RuntimeError(f"Не удалось подключиться к почте: {last_error}")


def select_folder(mail: imaplib.IMAP4_SSL, folder: str) -> bool:
    LOGGER.info("Открытие папки '%s'...", folder)
    result, _ = mail.select(f'"{folder}"', readonly=True)
    return result == "OK"


def collect_bidv(mail: imaplib.IMAP4_SSL) -> None:
    senders = configured_senders()
    mail_folder = os.getenv("YANDEX_MAIL_FOLDER", "INBOX").strip() or "INBOX"
    state_path = Path(os.getenv("STATE_FILE", STATE_FILE_NAME))
    transactions_dir = Path(os.getenv("TRANSACTIONS_DIR", TRANSACTIONS_DIR))
    processed_uids = load_state(state_path)
    newly_processed: set[str] = set()

    LOGGER.info("=== BIDV ===")
    if not select_folder(mail, mail_folder):
        raise RuntimeError(f"Не удалось открыть папку почты: {mail_folder}")
    LOGGER.info("Папка открыта")
    LOGGER.info("Поиск писем от %s...", sender_list_text(senders))
    all_uids = search_sender_uids(mail, senders)
    LOGGER.info(
        "Найдено писем в выбранной папке: %d, уже обработано: %d",
        len(all_uids),
        len(processed_uids),
    )

    try:
        for raw_uid in all_uids:
            uid = raw_uid.decode("ascii")
            if uid in processed_uids:
                continue
            LOGGER.info("Чтение письма UID %s...", uid)
            try:
                message = read_message(mail, uid)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as error:
                # Сохраняем уже созданные JSON и продолжим с этим UID на следующем
                # запуске, вместо потери всей партии из-за краткого сбоя Яндекса.
                LOGGER.warning("Соединение IMAP прервано на UID %s: %s", uid, error)
                LOGGER.warning("Остальные письма будут обработаны при следующем запуске.")
                raise
            if message is None:
                LOGGER.warning("Не удалось прочитать письмо UID %s", uid)
                continue

            from_header = decode_mime_header(message.get("From"))
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
            mail_text = ""
            try:
                mail_text = text_from_message(message)
                transaction = parse_bidv_notification(mail_text)
                LOGGER.info(
                    "Распознано: %s на %d VND",
                    transaction.transaction_type,
                    transaction.amount_vnd,
                )
                outcome = save_transaction(transactions_dir, transaction)
            except ValueError as error:
                LOGGER.warning("Письмо UID %s не выгружено: %s", uid, error)
                LOGGER.warning("Начало письма: %s", _body_preview(mail_text))
                # Ошибку разбора никогда не записываем в processed_uids.json.
                # Формат письма может стать поддерживаемым после обновления
                # парсера, и такой UID должен повторно проверяться.
                LOGGER.info(
                    "UID %s не добавлен в журнал: письмо будет повторно "
                    "проверено в следующем запуске.",
                    uid,
                )
                continue

            newly_processed.add(uid)
            LOGGER.info("UID %s: %s (%s)", uid, outcome, transaction.reference_number)
    finally:
        if newly_processed:
            save_state(state_path, processed_uids | newly_processed)
        LOGGER.info("BIDV: обработано новых писем: %d", len(newly_processed))


def fetch_headers(mail: imaplib.IMAP4_SSL, uids: list[str]) -> dict[str, Message]:
    """Читает только From и Subject пачкой, не скачивая письма целиком."""
    headers: dict[str, Message] = {}
    for start in range(0, len(uids), 200):
        chunk = uids[start : start + 200]
        result, payload = mail.uid(
            "fetch", ",".join(chunk), "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
        )
        if result != "OK":
            # Яндекс отказывает всей пачке, если одно письмо не читается.
            # Тогда ниже каждое письмо пачки дочитывается по отдельности.
            LOGGER.warning("Заголовки пачки не прочитались (%s), читаю по одному", result)
            continue
        for item in payload or []:
            if not (isinstance(item, tuple) and len(item) > 1):
                continue
            meta = item[0].decode("ascii", errors="replace") if isinstance(item[0], bytes) else str(item[0])
            match = re.search(r"UID (\d+)", meta)
            if match and isinstance(item[1], bytes):
                headers[match.group(1)] = email.message_from_bytes(item[1])
    # Яндекс иногда пишет UID после тела ответа, и в пачке письмо не опознать.
    # Такие письма дочитываем по одному: там UID известен заранее.
    for uid in uids:
        if uid in headers:
            continue
        result, payload = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        for item in (payload or []) if result == "OK" else []:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                headers[uid] = email.message_from_bytes(item[1])
                break
        else:
            LOGGER.warning(
                "Письмо UID %s в папке Bybit не читается (%s %s) — пропущено, "
                "будет проверено в следующем запуске",
                uid,
                result,
                _imap_response_text(payload or []),
            )
    return headers


def collect_bybit(mail: imaplib.IMAP4_SSL) -> None:
    """Выгружает выполненные P2P-ордера Bybit из отдельной папки.

    UID у каждой папки свои, поэтому журнал отдельный. Письма с другими темами
    или от других отправителей сразу записываются в журнал: они никогда не
    станут ордерами, и скачивать их каждый час незачем.
    """
    folder = os.getenv("BYBIT_MAIL_FOLDER", "").strip() or BYBIT_DEFAULT_FOLDER
    raw_sender = os.getenv("BYBIT_SENDER", "").strip() or BYBIT_DEFAULT_SENDER
    senders = {item.casefold() for item in re.split(r"[,;\s]+", raw_sender) if item}
    state_path = Path(os.getenv("BYBIT_STATE_FILE", BYBIT_STATE_FILE_NAME))
    orders_dir = Path(os.getenv("BYBIT_DIR", BYBIT_DIR))
    processed_uids = load_state(state_path)
    newly_processed: set[str] = set()
    saved = 0

    LOGGER.info("=== Bybit ===")
    if not select_folder(mail, folder):
        LOGGER.warning("Папка '%s' не открылась — Bybit пропущен.", folder)
        return
    result, data = mail.uid("search", None, "ALL")
    if result != "OK":
        raise RuntimeError(f"Не удалось получить список писем Bybit ({result})")
    all_uids = [uid.decode("ascii") for uid in (data[0].split() if data and data[0] else [])]
    pending = [uid for uid in all_uids if uid not in processed_uids]
    LOGGER.info("Писем в папке: %d, новых: %d", len(all_uids), len(pending))

    try:
        for uid, header in fetch_headers(mail, pending).items():
            subject = decode_mime_header(header.get("Subject"))
            from_header = decode_mime_header(header.get("From"))
            if not sender_matches(from_header, senders) or side_from_subject(subject) is None:
                newly_processed.add(uid)
                continue

            message = read_message(mail, uid)
            if message is None:
                LOGGER.warning("Не удалось прочитать письмо Bybit UID %s", uid)
                continue
            mail_text = ""
            try:
                mail_text = text_from_message(message)
                order = parse_bybit_p2p(subject, mail_text)
            except ValueError as error:
                LOGGER.warning("Письмо Bybit UID %s не выгружено: %s", uid, error)
                LOGGER.warning("Начало письма: %s", _body_preview(mail_text))
                continue
            outcome = save_bybit_order(orders_dir, order)
            saved += outcome == "saved"
            newly_processed.add(uid)
            LOGGER.info(
                "UID %s: %s %s %s за %s %s (%s)",
                uid,
                order.side,
                order.crypto_amount,
                order.crypto,
                order.as_dict()["fiat_amount"],
                order.fiat_currency,
                outcome,
            )
    finally:
        if newly_processed:
            save_state(state_path, processed_uids | newly_processed)
        LOGGER.info(
            "Bybit: новых ордеров: %d, просмотрено писем: %d", saved, len(newly_processed)
        )


def main() -> int:
    LOGGER.info("Запуск сборщика...")
    configured_senders()  # проверить настройку до подключения к почте
    mail = connect(required_env("YANDEX_EMAIL"), required_env("YANDEX_APP_PASSWORD"))
    failed = False
    try:
        for collect in (collect_bidv, collect_bybit):
            try:
                collect(mail)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as error:
                # Обрыв на одной папке не должен терять то, что уже собрано.
                LOGGER.warning("Сбор прерван обрывом соединения: %s", error)
                LOGGER.warning("Остальное будет обработано при следующем запуске.")
                break
            except RuntimeError as error:
                LOGGER.error("%s", error)
                failed = True
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        LOGGER.error("Сбор остановлен: %s", error)
        raise SystemExit(1)
