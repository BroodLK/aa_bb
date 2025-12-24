"""
Helpers for constructing links against shared blacklist tools.
"""

from ..models import BigBrotherConfig
from django.utils.html import format_html

def get_external_blacklist_link():
    """
    Returns an HTML link to the configured External Blacklist URL.
    """
    cfg = BigBrotherConfig.get_solo()
    url = cfg.external_blacklist_url
    if not url:
        return "External Blacklist URL not configured."
    return format_html(
        '<a href="{}" target="_blank" class="btn btn-primary btn-block">Go to External Blacklist</a>',
        url
    )
