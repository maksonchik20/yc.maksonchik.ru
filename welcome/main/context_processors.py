from .site_info import SITE, TARIFFS


def site_info(request):
    return {
        'site': SITE,
        'tariffs': TARIFFS,
    }
