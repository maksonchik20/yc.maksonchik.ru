import json
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from . import ai_provider


@override_settings(GHOST_AI_CONFIG={'api_key': 'fixture-provider-key', 'folder': 'fixturefolder123456',
                                  'model': ai_provider.DEFAULT_MODEL})
class QwenProviderTests(SimpleTestCase):
    def messages(self):
        return [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Задание'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,/9j/2Q=='}},
        ]}, {'role': 'assistant', 'content': 'Предыдущий ответ'},
            {'role': 'user', 'content': 'Продолжение'}]

    def response(self, status=200, body=None):
        result = MagicMock()
        result.status_code = status
        result.__enter__.return_value = result
        result.iter_content.return_value = [json.dumps(body or {'status': 'completed', 'output': [
            {'type': 'reasoning', 'text': 'Do not return this'},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Б — 42'}]},
        ]}).encode()]
        return result

    def test_request_uses_godmode_responses_contract_and_private_history(self):
        with patch.object(requests, 'Session') as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = self.response()
            self.assertEqual(ai_provider.complete(self.messages()), 'Б — 42')
        self.assertFalse(client.trust_env)
        args, kwargs = client.post.call_args
        self.assertEqual(args, (ai_provider.ENDPOINT,))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (5, 55))
        fieldToBody = json.loads(kwargs['data'])
        self.assertFalse(fieldToBody['store'])
        self.assertFalse(fieldToBody['stream'])
        self.assertEqual(fieldToBody['reasoning'], {'effort': 'none'})
        self.assertIn('/qwen3.6-35b-a3b/latest', fieldToBody['model'])
        self.assertEqual(fieldToBody['instructions'], ai_provider.SYSTEM_PROMPT)
        self.assertEqual(fieldToBody['input'][0]['content'][1]['type'], 'input_image')
        self.assertEqual(fieldToBody['input'][1]['content'], 'Предыдущий ответ')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer fixture-provider-key')
        self.assertEqual(kwargs['headers']['x-data-logging-enabled'], 'false')

    def test_rejects_external_image_urls_without_network(self):
        messages = self.messages()
        messages[0]['content'][1]['image_url']['url'] = 'https://example.com/private-image'
        with patch.object(requests, 'Session') as factory:
            with self.assertRaises(ai_provider.AiProviderError):
                ai_provider.complete(messages)
            factory.assert_not_called()

    def test_bad_config_has_no_network_and_no_secret_in_error(self):
        with override_settings(GHOST_AI_CONFIG={'api_key': 'fixture-key\r\nInjected: yes',
                'folder': 'fixturefolder123456', 'model': ai_provider.DEFAULT_MODEL}):
            with patch.object(requests, 'Session') as factory:
                with self.assertRaises(ai_provider.AiProviderError) as caught:
                    ai_provider.complete(self.messages())
                self.assertEqual(str(caught.exception), 'ai_unavailable')
                factory.assert_not_called()

    def test_upstream_statuses_are_safe_and_never_follow_redirects(self):
        for status, code in [(302, 'ai_provider_error'), (401, 'ai_unavailable'),
                             (403, 'ai_unavailable'), (429, 'ai_rate_limited'), (500, 'ai_provider_error')]:
            with self.subTest(status=status), patch.object(requests, 'Session') as factory:
                factory.return_value.__enter__.return_value.post.return_value = self.response(status)
                with self.assertRaises(ai_provider.AiProviderError) as caught:
                    ai_provider.complete(self.messages())
                self.assertEqual(str(caught.exception), code)

    def test_timeout_is_not_retried_automatically(self):
        with patch.object(requests, 'Session') as factory:
            client = factory.return_value.__enter__.return_value
            client.post.side_effect = requests.Timeout('sensitive provider details')
            with self.assertRaises(ai_provider.AiProviderError) as caught:
                ai_provider.complete(self.messages())
            self.assertEqual(str(caught.exception), 'ai_timeout')
            self.assertEqual(client.post.call_count, 1)

    def test_incomplete_or_empty_answers_are_not_presented_as_success(self):
        for body in [{'status': 'incomplete', 'output': []}, {'status': 'completed', 'output': []},
                     {'status': 'completed', 'error': {'message': 'private-details'}}]:
            with self.subTest(body=body):
                with self.assertRaises(ai_provider.AiProviderError) as caught:
                    ai_provider.extract_answer(body)
                self.assertEqual(str(caught.exception), 'ai_provider_error')

    def test_large_provider_response_is_bounded(self):
        with patch.object(requests, 'Session') as factory:
            response = self.response()
            response.iter_content.return_value = [b'x' * (ai_provider.MAX_RESPONSE_BYTES + 1)]
            factory.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaises(ai_provider.AiProviderError):
                ai_provider.complete(self.messages())
