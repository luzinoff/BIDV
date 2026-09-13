"""Офлайн-проверка трёх предоставленных шаблонов BIDV."""

from parser import parse_bidv_notification


INTERBANK = """Loại giao dịch:
Transaction type:	Chuyển tiền ngoài BIDV
Interbank transfer
Thời gian giao dịch:
Transaction time:	13/09/2026 15:27:33
Số tham chiếu:
Reference number:	6256BIDVE26FT6ZU
Số tiền giao dịch:
Transaction amount:	38,000 VND
Tên người thụ hưởng:
Beneficiary name:	DANG THI TUONG AN
"""

WITHIN_BIDV = """Loại giao dịch:
Transaction type:	Chuyển tiền nội bộ BIDV
Within BIDV transfer
Thời gian giao dịch:
Transaction time:	13/09/2026 18:27:51
Số tham chiếu:
Reference number:	0552qXXO-8C5borQac
Số tiền giao dịch:
Transaction amount:
Số tiền thực tế KH nhập chưa tính thuế phí
(The actual amount customer enters, not include fee and tax)	25,000 VND
Tên người thụ hưởng:
Beneficiary name:	HO KINH DOANH SINH TO VAN
"""

QR_PAY = """Loại giao dịch:
Transaction type:	Thanh toán QR
QR Pay
Thời gian giao dịch:
Transaction time:	13/09/2026 11:00:14
Số tham chiếu:
Reference number:	0551bXq4-8C58VQ1DA
Số tiền:
Amount:	135,000 VND
Merchant Name:	VIETTEL TELECOM
"""


def check(text: str, expected: dict) -> None:
    actual = parse_bidv_notification(text).as_dict()
    assert actual == expected, f"\nПолучено: {actual}\nОжидалось: {expected}"
    allowed = {
        "transaction_type",
        "amount_vnd",
        "recipient_type",
        "recipient_name",
        "transaction_at",
        "reference_number",
    }
    assert set(actual) == allowed, actual
    forbidden_fragments = ("account", "card", "ip", "operating", "remark", "os")
    assert not any(
        fragment in key.casefold() for key in actual for fragment in forbidden_fragments
    ), actual


def main() -> None:
    check(
        INTERBANK,
        {
            "transaction_type": "interbank_transfer",
            "amount_vnd": 38000,
            "recipient_type": "beneficiary",
            "recipient_name": "DANG THI TUONG AN",
            "transaction_at": "2026-09-13T15:27:33",
            "reference_number": "6256BIDVE26FT6ZU",
        },
    )
    check(
        WITHIN_BIDV,
        {
            "transaction_type": "within_bidv_transfer",
            "amount_vnd": 25000,
            "recipient_type": "beneficiary",
            "recipient_name": "HO KINH DOANH SINH TO VAN",
            "transaction_at": "2026-09-13T18:27:51",
            "reference_number": "0552qXXO-8C5borQac",
        },
    )
    check(
        QR_PAY,
        {
            "transaction_type": "qr_pay",
            "amount_vnd": 135000,
            "recipient_type": "merchant",
            "recipient_name": "VIETTEL TELECOM",
            "transaction_at": "2026-09-13T11:00:14",
            "reference_number": "0551bXq4-8C58VQ1DA",
        },
    )
    print("OK: три шаблона разобраны, чувствительные поля отсутствуют")


if __name__ == "__main__":
    main()
