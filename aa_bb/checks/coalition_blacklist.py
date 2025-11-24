"""
Helpers for constructing links against Imperium's shared blacklist tools.
"""

from allianceauth.authentication.models import CharacterOwnership
import urllib.parse

# def generate_blacklist_links(user_id, base_url="https://gice.goonfleet.com/Blacklist", max_url_length=2000):
#     """
#     Batch character names into multiple links to avoid exceeding the
#     URL length limit enforced by the external blacklist service.
#     """
#     characters = CharacterOwnership.objects.filter(user__id=user_id)
#     names = [str(char.character) for char in characters]
#
#     links = []
#     current_names = []
#
#     for name in names:
#         test_list = current_names + [name]
#         query_string = urllib.parse.quote(",".join(test_list))  # URL-encode
#         url = f"{base_url}?q={query_string}"
#
#         if len(url) >= max_url_length:  # Push current batch if adding name would exceed service limit.
#             links.append(current_names)
#             current_names = [name]
#         else:
#             current_names = test_list
#
#     if current_names:  # Append final batch after loop.
#         links.append(current_names)
#
#     formatted_links = []
#     for i, chunk in enumerate(links):
#         link_text = "Click here" if i == 0 else "and here"
#         query = urllib.parse.quote(",".join(chunk))  # URL-encode
#         formatted_links.append(f"<a href='{base_url}?q={query}'>{link_text}</a>")
#
#     return formatted_links

def generate_blacklist_links():
    return ""
