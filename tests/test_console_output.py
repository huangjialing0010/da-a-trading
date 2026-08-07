import io
import unittest

from main import _configure_console_output


class ConsoleOutputTest(unittest.TestCase):
    def test_cp936_replaces_emoji_without_losing_chinese(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp936", errors="strict")

        self.assertTrue(_configure_console_output(stream))
        stream.write("日更完成 ⚠️")
        stream.flush()

        text = raw.getvalue().decode("cp936")
        self.assertIn("日更完成", text)
        self.assertNotIn("⚠", text)

    def test_utf8_keeps_full_output(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")

        self.assertTrue(_configure_console_output(stream))
        stream.write("日更完成 ⚠️")
        stream.flush()

        self.assertEqual(raw.getvalue().decode("utf-8"), "日更完成 ⚠️")


if __name__ == "__main__":
    unittest.main()
