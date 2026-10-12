import hashlib
import os
import tempfile
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from .staticfiles import AdminVersionedStaticFilesStorage


class AdminStaticFilesTests(SimpleTestCase):
    def test_deployed_content_change_invalidates_cached_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'admin/js/autocomplete.js'
            path.parent.mkdir(parents=True)
            path.write_text('old-content')
            storage = AdminVersionedStaticFilesStorage(location=directory, base_url='/static/')
            before = storage.url('admin/js/autocomplete.js')
            self.assertEqual(storage.url('admin/js/autocomplete.js'), before)
            modified = path.stat().st_mtime_ns
            path.write_text('new-content')  # Same length, different contents.
            os.utime(path, ns=(modified + 1000000000, modified + 1000000000))
            after = storage.url('admin/js/autocomplete.js')
            self.assertNotEqual(after, before)
            self.assertTrue(after.endswith('?v=' + hashlib.sha256(b'new-content').hexdigest()[:12]))

    def test_template_static_tag_uses_versioned_admin_and_theme_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            for relative in ('admin/js/autocomplete.js', 'jazzmin/js/main.js'):
                path = Path(directory) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('source')
            with override_settings(
                STATIC_ROOT=directory,
                STATIC_URL='/static/',
                STATICFILES_STORAGE='ghost_note.staticfiles.AdminVersionedStaticFilesStorage',
            ):
                expected = hashlib.sha256(b'source').hexdigest()[:12]
                for relative in ('admin/js/autocomplete.js', 'jazzmin/js/main.js'):
                    html = Template('{% load static %}{% static asset %}').render(Context({'asset': relative}))
                    self.assertEqual(html, '/static/{}?v={}'.format(relative, expected))

    def test_other_assets_and_missing_files_keep_working(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = AdminVersionedStaticFilesStorage(location=directory, base_url='/static/')
            self.assertEqual(storage.url('ghost_note/admin/referrals.css'), '/static/ghost_note/admin/referrals.css')
            self.assertEqual(storage.url('admin/js/not-collected.js'), '/static/admin/js/not-collected.js')
