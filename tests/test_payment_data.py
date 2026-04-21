import unittest

from app.payment_data import parse_check_gov_payment, parse_privat_receipt_pdf_text


class PaymentDataTests(unittest.TestCase):
    def test_parse_check_gov_payment(self) -> None:
        data = {
            "payments": [
                {
                    "sender": "Радченко Олександр Вікторович",
                    "recipient": "Вікторія В., 444111******1722",
                    "amount": 30000,
                    "date": "2026-04-20T09:02:35Z",
                    "description": "Переказ особистих коштів",
                    "currencyCode": 980,
                }
            ]
        }
        payment = parse_check_gov_payment(data)
        self.assertEqual(payment["recipient_card"], "444111******1722")
        self.assertEqual(payment["amount"], "300")
        self.assertEqual(payment["currency_code"], 980)

    def test_parse_privat_receipt_pdf_text(self) -> None:
        text = """
        Квитанція
        Рахунок отримувача
        5355571306568825
        Сума переказу: 8 916,30 грн
        """
        payment = parse_privat_receipt_pdf_text(text)
        self.assertEqual(payment["recipient_card"], "5355571306568825")
        self.assertEqual(payment["amount"], "8916.3")
        self.assertEqual(payment["currency"], "UAH")


if __name__ == "__main__":
    unittest.main()
