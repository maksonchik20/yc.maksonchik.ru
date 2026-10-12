import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import GhostAccessToken, GhostSession, GhostTextMessage


@override_settings(GHOST_NOTE_BOT_TOKEN='', GHOST_NOTE_ADMIN_CHAT_ID='')
class TextDeliveryTests(TestCase):
    def setUp(self):
        self.token = GhostAccessToken.objects.create(token_type='test', starts_at=timezone.now()-timedelta(minutes=1), expires_at=timezone.now()+timedelta(hours=1))
        self.session = GhostSession.objects.create(session_id=str(uuid.uuid4()), access_token=self.token)

    def send(self, text):
        response = self.client.post('/ghost/text/', data=json.dumps({'session_id':self.session.session_id,'text':text}), content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)

    def poll(self, token=None):
        return self.client.get('/ghost/text/poll/', {'session_id':self.session.session_id}, HTTP_X_ACCESS_TOKEN=token or self.token.token)

    def test_brackets_unicode_and_text_after_bracket_survive_full_delivery(self):
        messages = [
            'Массив [1, 2] и текст после скобки',
            '] в начале, [ в конце',
            '[] [[a] [b]] хвост строки',
            'arr[0] = value; result[1] = 42;',
            'Скобки в кавычках: "[пример]" и дальше текст',
            'Обратная косая \\] и \\"[] конец',
            r'Буквальный текст \u005d и ] настоящий символ',
            '😀 [Привет] 你好 [مرحبا] конец',
            'Первая [строка]\nВторая ] строка\r\nТретья\tстрока',
            'Обычная строка без скобок',
            'После проблемного сообщения это тоже должно прийти',
        ]
        for text in messages:self.send(text)
        self.assertEqual(list(GhostTextMessage.objects.order_by('created_at','pk').values_list('text',flat=True)),messages)
        response=self.poll()
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['messages'],messages)
        wire=response.content.decode()
        self.assertEqual(wire.count('['),1)
        self.assertEqual(wire.count(']'),1)
        self.assertIn('\\u005b',wire)
        self.assertIn('\\u005d',wire)
        self.assertFalse(GhostTextMessage.objects.filter(delivered=False).exists())
        self.assertEqual(self.poll().json(),{'messages':[]})
        output=os.environ.get('GHOST_BRACKET_FIXTURES_DIR')
        if output:
            Path(output,'polled_wire.json').write_bytes(response.content)
            Path(output,'expected_messages.json').write_text(json.dumps(messages,ensure_ascii=False))

    def test_batch_limit_and_session_isolation(self):
        other=GhostSession.objects.create(session_id=str(uuid.uuid4()),access_token=self.token)
        untouched=GhostTextMessage.objects.create(session=other,text='Другой чат [не трогать]')
        for n in range(51):self.send('Сообщение [{}] хвост'.format(n))
        first=self.poll().json()['messages'];second=self.poll().json()['messages']
        self.assertEqual(first,['Сообщение [{}] хвост'.format(n) for n in range(50)])
        self.assertEqual(second,['Сообщение [50] хвост'])
        untouched.refresh_from_db();self.assertFalse(untouched.delivered)

    def test_invalid_token_does_not_consume_text(self):
        self.send('[текст] остаётся в очереди')
        self.assertEqual(self.poll('invalid-test-token').status_code,401)
        self.assertFalse(GhostTextMessage.objects.get().delivered)
        self.assertEqual(self.poll().json()['messages'],['[текст] остаётся в очереди'])

    def test_empty_poll_and_literal_escape_are_valid_json(self):
        self.assertEqual(self.poll().json(),{'messages':[]})
        text=r'\\u005b [x] \\u005d'
        self.send(text)
        self.assertEqual(self.poll().json()['messages'],[text])
