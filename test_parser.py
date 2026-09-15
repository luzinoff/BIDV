"""Офлайн-проверка трёх предоставленных шаблонов BIDV."""

from parser import parse_bidv_notification


INTERBANK = """Thông báo giao dịch thành công!
Notice of successful transaction
Kính gửi quý khách: LUZINOV OLEG
Dear Valued Customer: LUZINOV OLEG

Loại giao dịch:
Transaction type:	Chuyển tiền ngoài BIDV
Interbank transfer
Thời gian giao dịch:
Transaction time:	14/09/2026 17:02:55
Số tham chiếu:
Reference number:	020097048809141702542026f3rw434028
Tài khoản nguồn:
Debit account:	8842784999
Số tiền giao dịch:
Transaction amount:	99,000 VND
Phí giao dịch:
Transaction fee:	Miễn phí
Tên người thụ hưởng:
Beneficiary name:	LE THI THUY ANH
Số tài khoản/Số thẻ thụ hưởng:
Beneficiary account/ Card number:	9980179868888
Tên ngân hàng thụ hưởng:
Beneficiary bank:	NHTMCP Quân Đội
Số tiền ghi có:
Credit amount:	99,000 VND
Nội dung giao dịch:
Transaction remark:	LUZINOV OLEG Transfer
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


WITHIN_BIDV_MULTILINE_AMOUNT = """Thông báo giao dịch thành công!
Notice of successful transaction
Kính gửi quý khách: LUZINOV OLEG
Dear Valued Customer: LUZINOV OLEG

Loại giao dịch:
Transaction type:	Chuyển tiền nội bộ BIDV
Within BIDV transfer
Thời gian giao dịch:
Transaction time:	14/09/2026 22:15:56
Số tham chiếu:
Reference number:	0552Pavc-8C7N48oZT
Tài khoản nguồn:
Debit account:	8842784999
Số tiền giao dịch:
Transaction amount:
Số tiền thực tế KH nhập chưa tính thuế phí
(The actual amount customer enters, not include fee and tax)	25,000 VND
Phí giao dịch:
Transaction fee:	Miễn phí
Tên người thụ hưởng:
Beneficiary name:	HO KINH DOANH SINH TO VAN
Số tài khoản thụ hưởng:
Beneficiary account:	MBF62D865D050C01BID/8841697487
Tên ngân hàng thụ hưởng:
Beneficiary bank:	BIDV
Số tiền ghi có:
Credit amount:
Số tiền thực tế người thụ hưởng nhận được:
(The amount Beneficiary received)	25,000 VND
Nội dung giao dịch:
Transaction remark:	LUZINOV OLEG Transfer
Kênh thực hiện giao dịch:
Channel:	MB
Hệ điều hành:
Operating System:	ANDROID
IP:	171.254.175.131
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
            "amount_vnd": 99000,
            "recipient_type": "beneficiary",
            "recipient_name": "LE THI THUY ANH",
            "transaction_at": "2026-09-14T17:02:55",
            "reference_number": "020097048809141702542026f3rw434028",
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
    check(
        WITHIN_BIDV_MULTILINE_AMOUNT,
        {
            "transaction_type": "within_bidv_transfer",
            "amount_vnd": 25000,
            "recipient_type": "beneficiary",
            "recipient_name": "HO KINH DOANH SINH TO VAN",
            "transaction_at": "2026-09-14T22:15:56",
            "reference_number": "0552Pavc-8C7N48oZT",
        },
    )
    print("OK: четыре шаблона разобраны, чувствительные поля отсутствуют")


if __name__ == "__main__":
    main()
