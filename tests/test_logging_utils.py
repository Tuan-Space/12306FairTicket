import unittest

from ticket_app.logging_utils import redact_text


class LoggingUtilsTests(unittest.TestCase):
    def test_redacts_tokens_identifiers_and_phone_numbers(self):
        text = (
            "REPEAT_SUBMIT_TOKEN=abc123 secretStr:'secret-value' "
            "身份证 11010519491231002X 手机 13812345678"
        )
        redacted = redact_text(text)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("11010519491231002X", redacted)
        self.assertNotIn("13812345678", redacted)
        self.assertIn("138****5678", redacted)

    def test_redacts_json_tokens_and_whole_cookie_header(self):
        redacted = redact_text(
            '\"secretStr\": \"unsafe\", Cookie: JSESSIONID=first; route=second\n'
            '订单号: E123456789 orderId=E987654321'
        )

        self.assertNotIn("unsafe", redacted)
        self.assertNotIn("first", redacted)
        self.assertNotIn("second", redacted)
        self.assertNotIn("E123456789", redacted)
        self.assertNotIn("E987654321", redacted)

    def test_redacts_configured_passenger_names(self):
        redacted = redact_text("已选择乘车人: 张三、李四", ["张三", "李四"])

        self.assertNotIn("张三", redacted)
        self.assertNotIn("李四", redacted)


if __name__ == "__main__":
    unittest.main()
