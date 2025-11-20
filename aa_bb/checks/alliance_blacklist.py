"""
alliance blacklist helper utilities. All rendering happens elsewhere, but
collecting the character names in one helper makes templating easier.
"""

from allianceauth.authentication.models import CharacterOwnership

def get_user_character_names_alliance(user_id):
    """
    Given an Alliance Auth User ID, returns a comma-separated string
    of all character names linked to that user.
    """
    characters = CharacterOwnership.objects.filter(user__id=user_id)
    names =[]
    for char in characters:
        char_name = str(char.character)
        names.append(char_name)
    return "<br>".join(names)

