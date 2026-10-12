import json
import warnings

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.paginator import UnorderedObjectListWarning
from django.test import RequestFactory, TestCase

from .models import GhostUser


class GhostUserAutocompleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_superuser(
            username='admin-test', password='test-only', email='admin@example.test'
        )
        # Deliberately interleave names, with enough equal names to cross pages.
        for index in range(47):
            GhostUser.objects.create(
                name='Same name' if index % 3 else 'Another name',
                telegram_username='person_{}'.format(index),
            )

    def autocomplete(self, page, term='', model='ghostuser', field='referred_by'):
        request = RequestFactory().get('/admin/autocomplete/', {
            'app_label': 'ghost_note', 'model_name': model,
            'field_name': field, 'term': term, 'page': page,
        })
        request.user = self.operator
        response = admin.site.autocomplete_view(request)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def assert_pages_match(self, expected, term='', model='ghostuser', field='referred_by'):
        found = []
        with warnings.catch_warnings():
            warnings.simplefilter('error', UnorderedObjectListWarning)
            page = 1
            while True:
                self.assertLessEqual(page, 4)
                data = self.autocomplete(page, term, model, field)
                found.extend(int(result['id']) for result in data['results'])
                if not data['pagination']['more']:
                    break
                page += 1
        self.assertEqual(found, expected)
        self.assertEqual(len(found), len(set(found)))

    def test_inviter_pages_are_stable_with_duplicate_names(self):
        expected = list(GhostUser.objects.order_by('name', 'pk').values_list('pk', flat=True))
        self.assert_pages_match(expected)

    def test_filtered_inviter_pages_do_not_skip_equal_names(self):
        expected = list(GhostUser.objects.filter(name='Same name').order_by('name', 'pk').values_list('pk', flat=True))
        self.assert_pages_match(expected, term='Same name')

    def test_token_owner_uses_same_stable_user_pages(self):
        expected = list(GhostUser.objects.order_by('name', 'pk').values_list('pk', flat=True))
        self.assert_pages_match(expected, model='ghostaccesstoken', field='user')

    def test_selected_inviter_can_be_saved_in_admin_form(self):
        users = list(GhostUser.objects.order_by('pk')[:2])
        invitee, inviter = users
        request = RequestFactory().get('/admin/ghost_note/ghostuser/{}/change/'.format(invitee.pk))
        request.user = self.operator
        form_class = admin.site._registry[GhostUser].get_form(request, invitee)
        form = form_class(data={
            'name': invitee.name, 'telegram_username': invitee.telegram_username,
            'referred_by': inviter.pk, 'notes': '',
        }, instance=invitee)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        form.save()
        invitee.refresh_from_db()
        self.assertEqual(invitee.referred_by_id, inviter.pk)
