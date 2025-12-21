"""
alliance blacklist helper utilities. All rendering happens elsewhere, but
collecting the character names in one helper makes templating easier.
"""

from ..models import BigBrotherConfig
from django.utils.html import format_html

def get_alliance_blacklist_link():
    """
    Returns an HTML link to the configured Alliance Blacklist URL.
    """
    cfg = BigBrotherConfig.get_solo()
    url = cfg.alliance_blacklist_url
    if not url:
        return "Alliance Blacklist URL not configured."
    return format_html(
        '<a href="{}" target="_blank" class="btn btn-primary btn-block">Go to Alliance Blacklist</a>',
        url
    )

