import hashlib
import os
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from django.contrib.staticfiles.storage import StaticFilesStorage


@lru_cache(maxsize=256)
def _content_version(path, modified_ns, size):
    # File metadata invalidates the cached digest after collectstatic/deployment.
    with open(path, 'rb') as source:
        return hashlib.sha256(source.read()).hexdigest()[:12]


class AdminVersionedStaticFilesStorage(StaticFilesStorage):
    """Change admin asset URLs whenever their deployed contents change."""

    def url(self, name):
        url = super().url(name)
        if not str(name).startswith(('admin/', 'jazzmin/')):
            return url
        try:
            path = self.path(name)
            metadata = os.stat(path)
            version = _content_version(path, metadata.st_mtime_ns, metadata.st_size)
        except OSError:
            # Keep normal URL generation working before collectstatic has run.
            return url
        parts = urlsplit(url)
        query = '{}{}v={}'.format(parts.query, '&' if parts.query else '', version)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
