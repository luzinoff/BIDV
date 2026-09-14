"""Собирает новые уведомления BIDV из Яндекс Почты и выгружает JSON на Google Drive."""

from __future__ import annotations

import base64
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

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from parser import Transaction, parse_bidv_notification

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOGGER = logging.getLogger(__name__)

IMAP_HOST = "imap.yandex.ru"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
STATE_FILE_NAME = "processed_uids.json"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задан обязательный секрет или параметр: {name}")
    return value


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


def drive_service():
    raw = required_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        # GitHub Secret иногда передают в base64, чтобы избежать искажения JSON.
        try:
            info = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as error:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не является JSON или base64(JSON)") from error
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=[GOOGLE_DRIVE_SCOPE]
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def remote_file_exists(service, folder_id: str, filename: str) -> bool:
    escaped_name = filename.replace("'", "\\'")
    query = (
        f"'{folder_id}' in parents and name = '{escaped_name}' "
        "and trashed = false"
    )
    result = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    return bool(result.get("files"))


def upload_transaction(service, folder_id: str, transaction: Transaction) -> str:
    from googleapiclient.http import MediaInMemoryUpload

    # Reference number делает имя идемпотентным, даже если Actions перезапустится.
    filename = f"bidv-{transaction.reference_number}.json"
    if remote_file_exists(service, folder_id, filename):
        return "already_exists"

    body = json.dumps(transaction.as_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaInMemoryUpload(body, mimetype="application/json", resumable=False)
    service.files().create(
        body={"name": filename, "parents": [folder_id], "mimeType": "application/json"},
        media_body=media,
        fields="id",
    ).execute()
    return "uploaded"


def main() -> int:
    LOGGER.info("Запуск сборщика...")
    sender = required_env("MAIL_SENDER").casefold()
    password = required_env("YANDEX_APP_PASSWORD")
    folder_id = required_env("GOOGLE_DRIVE_FOLDER_ID")
    mail_folder = os.getenv("YANDEX_MAIL_FOLDER", "INBOX").strip() or "INBOX"
    state_path = Path(os.getenv("STATE_FILE", STATE_FILE_NAME))
    processed_uids = load_state(state_path)

    LOGGER.info("Подключение к Google Drive...")
    service = drive_service()
    LOGGER.info("Google Drive подключён")

    LOGGER.info("Подключение к %s...", IMAP_HOST)
    mail = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
    newly_processed: set[str] = set()
    try:
        LOGGER.info("Авторизация в почте...")
        mail.login(required_env("YANDEX_EMAIL"), password)
        LOGGER.info("Авторизация успешна")
        result, _ = mail.select(f'"{mail_folder}"', readonly=True)
        if result != "OK":
            raise RuntimeError(f"Не удалось открыть папку почты: {mail_folder}")
        result, data = mail.uid("search", None, "ALL")
        if result != "OK":
            raise RuntimeError("Не удалось получить список писем")

        for raw_uid in data[0].split():
            uid = raw_uid.decode("ascii")
            if uid in processed_uids:
                continue
            result, payload = mail.uid("fetch", uid, "(RFC822)")
            if result != "OK" or not payload or not isinstance(payload[0], tuple):
                LOGGER.warning("Не удалось прочитать письмо UID %s", uid)
                continue
            message = email.message_from_bytes(payload[0][1])
            from_header = decode_mime_header(message.get("From")).casefold()
            # Сравнение адреса, а не произвольной отображаемой строки.
            addresses = re.findall(r"[\w.+-]+@[\w.-]+", from_header)
            if sender not in addresses:
                continue
            try:
                transaction = parse_bidv_notification(text_from_message(message))
                outcome = upload_transaction(service, folder_id, transaction)
            except (ValueError, HttpError) as error:
                LOGGER.warning("Письмо UID %s не выгружено: %s", uid, error)
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
