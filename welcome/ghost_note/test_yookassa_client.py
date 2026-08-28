from django.test import SimpleTestCase

from .yookassa_client import is_yookassa_webhook_ip


class YooKassaWebhookIpTest(SimpleTestCase):
    def test_accepts_network_and_single_host_entries(self):
        self.assertTrue(is_yookassa_webhook_ip('185.71.76.1'))
        self.assertTrue(is_yookassa_webhook_ip('77.75.156.11'))
        self.assertTrue(is_yookassa_webhook_ip('77.75.156.35'))

    def test_rejects_unknown_or_invalid_address(self):
        self.assertFalse(is_yookassa_webhook_ip('203.0.113.10'))
        self.assertFalse(is_yookassa_webhook_ip('not-an-ip'))
        self.assertFalse(is_yookassa_webhook_ip(''))
