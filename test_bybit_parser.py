"""Офлайн-проверка писем Bybit о выполненных P2P-ордерах."""

from bybit_parser import parse_bybit_p2p, side_from_subject
from main import _html_to_text

BUY_SUBJECT = "P2P-ордер на покупку: выполнен"
SELL_SUBJECT = "P2P-ордер на продажу: Выполнено"

BUY = """Уважаемый трейдер!
Ваш P2P-ордер на покупку выполнен, и монеты зачислены на аккаунт финансирования. Подробности можно посмотреть на странице ордера. При возникновении каких-либо вопросов свяжитесь с продавцом в чате.
Данные ордера:
- ID ордера: ******6592
- Время создания ордера: 2026-11-06 09:19:53 (UTC+0)
- Сумма в фиате: 35150 RUB
- Сумма криптовалюты: 411.1591 USDT
Если у вас остались вопросы, обратитесь к разделу «Часто задаваемые вопросы о P2P». Благодарим за поддержку!
С уважением,
Команда Bybit
"""

SELL_HTML = """<html><head><style>p{margin:0}</style></head><body>
<p>Уважаемый трейдер!</p>
<p>Ваш P2P-ордер на продажу
выполнен, и монеты зачислены на аккаунт покупателя. Перейдите на страницу P2P-ордера, чтобы проверить данные выполненной транзакции.</p>
<p>Данные ордера:<br>- ID ордера: ******2800<br>- Время создания ордера: 2026-11-06&nbsp;09:53:45 (UTC+0)<br>
- Сумма в фиате: <b>9,000,000</b> VND<br>- Сумма криптовалюты: 347.0883 USDT</p>
<p>С уважением,<br>Команда Bybit</p></body></html>
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    buy = parse_bybit_p2p(BUY_SUBJECT, BUY).as_dict()
    check(
        buy
        == {
            "side": "buy",
            "fiat_amount": 35150,
            "fiat_currency": "RUB",
            "crypto_amount": 411.1591,
            "crypto": "USDT",
            "created_at": "2026-11-06T09:19:53Z",
            "order_tail": "6592",
        },
        f"покупка: {buy}",
    )
    check(
        parse_bybit_p2p(BUY_SUBJECT, BUY).file_id == "20261106T091953-6592",
        "имя файла покупки",
    )

    sell = parse_bybit_p2p(SELL_SUBJECT, _html_to_text(SELL_HTML)).as_dict()
    check(sell["side"] == "sell", f"продажа: {sell}")
    check(sell["fiat_amount"] == 9000000 and sell["fiat_currency"] == "VND", f"фиат: {sell}")
    check(sell["crypto_amount"] == 347.0883, f"крипта: {sell}")
    check(sell["created_at"] == "2026-11-06T09:53:45Z", f"время: {sell}")
    check(sell["order_tail"] == "2800", f"ID: {sell}")

    check(side_from_subject("P2P-ордер на покупку: выполнен") == "buy", "тема покупки")
    check(side_from_subject("P2P-ордер на продажу: Выполнено") == "sell", "тема продажи")
    check(side_from_subject("P2P-ордер на покупку: отменён") is None, "чужая тема")
    check(side_from_subject("Вход в аккаунт") is None, "посторонняя тема")

    try:
        parse_bybit_p2p(SELL_SUBJECT, BUY)
    except ValueError:
        pass
    else:
        raise AssertionError("тема продажи с текстом покупки должна отклоняться")

    try:
        parse_bybit_p2p(BUY_SUBJECT, BUY.replace("35150 RUB", "много RUB"))
    except ValueError:
        pass
    else:
        raise AssertionError("неразборчивая сумма должна отклоняться")

    print("Bybit: все проверки пройдены")


if __name__ == "__main__":
    main()
