import datetime
import decimal
import io
import unittest

import transfer_mysql_to_postgresql as migration


class FakeCursor:
    def __init__(self, rows):
        self.rows = iter(rows)

    def fetchone(self):
        return next(self.rows, None)


class TransferTests(unittest.TestCase):
    def test_decimal_does_not_round_under_context_precision(self):
        original = "1234567890123456789012345678901234567890.123400"
        self.assertEqual(migration.number_text(original), original[:-2])
        self.assertEqual(migration.number_text("-0.000"), "0")

    def test_json_numeric_and_object_order_canonicalization(self):
        first = migration.normalized('{"b":1.00,"a":[true,null,"1"]}', "jsonb")
        second = migration.normalized('{"a": [true, null, "1"], "b": 1}', "jsonb")
        self.assertEqual(first[1], second[1])
        self.assertNotEqual(migration.normalized('"1"', "jsonb")[1],
                            migration.normalized('1', "jsonb")[1])
        self.assertNotEqual(migration.normalized('null', "jsonb")[1],
                            migration.normalized(None, "jsonb")[1])

    def test_datetime_is_utc_with_all_microseconds(self):
        naive = datetime.datetime(2026, 10, 13, 10, 20, 30, 123456)
        aware = naive.replace(tzinfo=migration.UTC)
        self.assertEqual(migration.normalized(naive, "timestamptz"),
                         migration.normalized(aware, "timestamptz"))
        moscow = datetime.timezone(datetime.timedelta(hours=3))
        self.assertEqual(migration.normalized(aware.astimezone(moscow), "timestamptz"),
                         migration.normalized(naive, "timestamptz"))

    def test_binary_is_exact_and_text_nul_rejected(self):
        value = b"\x00\xff\x80\t\n\\"
        self.assertEqual(migration.normalized(memoryview(value), "bytea"),
                         ("\\x00ff80090a5c", value))
        with self.assertRaises(ValueError):
            migration.normalized("hello\x00world", "text")

    def test_copy_escape_distinguishes_null_empty_and_literal_null(self):
        self.assertEqual(migration.escape_copy(None), "\\N")
        self.assertEqual(migration.escape_copy(""), "")
        self.assertEqual(migration.escape_copy("\\N"), "\\\\N")
        self.assertEqual(migration.escape_copy("\t\n\r\b\f\v\\"), "\\t\\n\\r\\b\\f\\v\\\\")

    def test_identifiers_escape_both_dialects(self):
        self.assertEqual(migration.mysql_identifier("a`b"), "`a``b`")
        self.assertEqual(migration.pg_identifier('a"b'), '"a""b"')

    def test_uuid_and_interval_are_lossless(self):
        self.assertEqual(migration.normalized("4f4471f8e5ab44e498e24a09e526f085", "uuid")[0],
                         "4f4471f8-e5ab-44e4-98e2-4a09e526f085")
        value = datetime.timedelta(days=-1, seconds=1, microseconds=3)
        microseconds = -86398999997
        self.assertEqual(migration.normalized(value, "interval"),
                         migration.normalized(microseconds, "interval"))

    def test_invalid_boolean_integer_datetime_fail_closed(self):
        for value, kind in ((2, "bool"), ("1.1", "int4"), ("0000-00-00", "date"),
                            ("0000-00-00 00:00:00", "timestamptz"), ("NaN", "jsonb")):
            with self.subTest(kind=kind):
                with self.assertRaises((ValueError, decimal.InvalidOperation)):
                    migration.normalized(value, kind)

    def test_copy_stream_tiny_reads_handles_large_rows_and_utf8(self):
        columns = [{"name": "id", "type": "int8"}, {"name": "text", "type": "text"}]
        rows = [(1, "Привет\nмир\t\\N"), (2, "x" * 100000), (3, None)]
        digest = migration.RowsDigest(columns)
        stream = migration.CopyStream(FakeCursor(rows), digest)
        result = io.BytesIO()
        while True:
            chunk = stream.read(7)
            if not chunk:
                break
            self.assertLessEqual(len(chunk), 7)
            result.write(chunk)
        expected = ("1\tПривет\\nмир\\t\\\\N\n2\t" + "x" * 100000 + "\n3\t\\N\n").encode("utf-8")
        self.assertEqual(result.getvalue(), expected)
        self.assertEqual(digest.count, 3)
        self.assertTrue(stream.exhausted)

    def test_hash_distinguishes_field_boundaries_null_and_empty(self):
        columns = [{"name": "a", "type": "text"}, {"name": "b", "type": "text"}]
        hashes = []
        for row in (("ab", "c"), ("a", "bc"), (None, ""), ("", None), ("", "")):
            digest = migration.RowsDigest(columns)
            digest.add(row)
            hashes.append(digest.report()["sha256"])
        self.assertEqual(len(set(hashes)), len(hashes))

    def test_ghost_text_codec_hashes_decoded_values(self):
        columns = [{"name": "text", "type": "text", "ghost_text_codec": True}]
        for text in ("normal", "nul\x00value", migration.GHOST_TEXT_PREFIX + "user literal", "Привет"):
            source = migration.RowsDigest(columns)
            encoded, = source.add((text,))
            self.assertNotIn("\x00", encoded)
            target = migration.RowsDigest(columns, from_postgresql=True)
            target.add((encoded,))
            self.assertEqual(source.report(), target.report())
            if "\x00" in text or text.startswith(migration.GHOST_TEXT_PREFIX):
                self.assertNotEqual(text, encoded)

    def test_ordering_binary_text_and_canonical_uuid(self):
        definition = {"columns": [{"name": "id", "type": "uuid"}], "pk": ["id"]}
        self.assertIn("LOWER(REPLACE(`id`, '-', ''))", migration.source_query("example", definition))
        definition = {"columns": [{"name": "id", "type": "varchar"}], "pk": ["id"]}
        self.assertIn("ORDER BY BINARY `id`", migration.source_query("example", definition))
        self.assertIn('ORDER BY "id" COLLATE "C"', migration.target_query("public", "example", definition))


if __name__ == "__main__":
    unittest.main()
