"""Preserve legacy message payloads PostgreSQL TEXT cannot store verbatim."""

import base64
import binascii

from django.db import models


TEXT_ENCODING_PREFIX = '~ghostnote:text:b64:v1~'


def encode_postgresql_text(value):
    if isinstance(value, str) and ('\x00' in value or value.startswith(TEXT_ENCODING_PREFIX)):
        return TEXT_ENCODING_PREFIX + base64.b64encode(value.encode('utf-8')).decode('ascii')
    return value


def decode_postgresql_text(value):
    if isinstance(value, str) and value.startswith(TEXT_ENCODING_PREFIX):
        try:
            return base64.b64decode(value[len(TEXT_ENCODING_PREFIX):], validate=True).decode('utf-8')
        except (ValueError, binascii.Error, UnicodeDecodeError):
            # Keep an unencoded pre-migration value intact rather than losing it.
            return value
    return value


class NullSafeTextField(models.TextField):
    def get_db_prep_value(self, value, connection, prepared=False):
        value = super().get_db_prep_value(value, connection, prepared=prepared)
        return encode_postgresql_text(value) if connection.vendor == 'postgresql' else value

    def from_db_value(self, value, expression, connection):
        return decode_postgresql_text(value) if connection.vendor == 'postgresql' else value
