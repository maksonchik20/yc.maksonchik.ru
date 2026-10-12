import builtins
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import time
import types
from unittest import TestCase
from unittest.mock import Mock, patch


class LazyYandexClientTests(TestCase):
    def load_module(self):
        env = types.ModuleType('env')
        env.YANDEX_CLOUD_API_KEY = 'test-api-key'
        env.YANDEX_CLOUD_FOLDER = 'test-folder'
        env.YANDEX_CLOUD_MODEL = 'test-model'
        specification = importlib.util.spec_from_file_location('isolated_yandex_client',
            Path(__file__).with_name('yandex_client.py'))
        module = importlib.util.module_from_spec(specification)
        with patch.dict(sys.modules, {'env': env}):
            specification.loader.exec_module(module)
        return module

    def test_module_import_does_not_import_openai_or_construct_client(self):
        original_import = builtins.__import__

        def reject_openai(name, *args, **kwargs):
            if name == 'openai' or name.startswith('openai.'):
                raise AssertionError('SDK imported eagerly')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=reject_openai):
            module = self.load_module()
            self.assertEqual(module.parse_image_base64(None), (None, None))
        self.assertIsNone(module.client)

    def test_first_use_initializes_once_with_original_configuration(self):
        module = self.load_module()
        sdk = types.ModuleType('openai')
        sdk.OpenAI = Mock()
        with patch.dict(sys.modules, {'openai': sdk}):
            first = module._get_client()
            self.assertIs(module._get_client(), first)
        sdk.OpenAI.assert_called_once_with(api_key='test-api-key',
            base_url='https://ai.api.cloud.yandex.net/v1', project='test-folder')

    def test_concurrent_first_calls_share_one_client(self):
        module = self.load_module()
        expected = Mock()

        def construct(**fieldToValue):
            time.sleep(0.02)
            return expected

        sdk = types.ModuleType('openai')
        sdk.OpenAI = Mock(side_effect=construct)
        with patch.dict(sys.modules, {'openai': sdk}):
            with ThreadPoolExecutor(max_workers=8) as pool:
                clients = list(pool.map(lambda _: module._get_client(), range(8)))
        self.assertTrue(all(client is expected for client in clients))
        sdk.OpenAI.assert_called_once()

    def test_initialization_failure_can_be_retried(self):
        module = self.load_module()
        expected = Mock()
        sdk = types.ModuleType('openai')
        sdk.OpenAI = Mock(side_effect=[RuntimeError('temporary initialization error'), expected])
        with patch.dict(sys.modules, {'openai': sdk}):
            with self.assertRaises(RuntimeError):
                module._get_client()
            self.assertIsNone(module.client)
            self.assertIs(module._get_client(), expected)

    def test_create_response_preserves_payload_and_result(self):
        module = self.load_module()
        client = Mock()
        client.responses.create.return_value = types.SimpleNamespace(
            error=None, status='completed', output_text=' response ', id='response-id')
        with patch.object(module, '_get_client', return_value=client):
            self.assertEqual(module.create_response(' prompt ', previous_response_id='previous',
                instructions='test instruction', reasoning_effort='none'), ('response', 'response-id'))
        client.responses.create.assert_called_once_with(model='gpt://test-folder/test-model',
            temperature=0.3, instructions='test instruction', input=' prompt ', max_output_tokens=10000,
            previous_response_id='previous', reasoning={'effort': 'none'})

    def test_empty_prompt_does_not_initialize_client(self):
        module = self.load_module()
        with patch.object(module, '_get_client') as client:
            with self.assertRaises(ValueError):
                module.create_response(' ')
        client.assert_not_called()
