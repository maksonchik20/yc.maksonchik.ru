from unittest.mock import patch

from django.test import SimpleTestCase

from .who_update_payment_bridge import forward_who_update_payment


class WhoUpdatePaymentBridgeTests(SimpleTestCase):
    def test_ignores_ghost_note_payment(self):
        self.assertIsNone(forward_who_update_payment({"object": {"metadata": {"order_id": "1"}}}))

    @patch("ghost_note.who_update_payment_bridge._bridge_token", return_value="secret")
    @patch("ghost_note.who_update_payment_bridge.requests.post")
    def test_forwards_who_update_payment_with_secret(self, post, bridge_token):
        post.return_value.status_code = 200
        payload = {"object": {"metadata": {"service": "who_update", "order_id": "1"}}}
        self.assertEqual(forward_who_update_payment(payload), 200)
        self.assertEqual(post.call_args.kwargs["headers"]["X-Who-Update-Payment-Token"], "secret")
