"""
Summaries covering corporation roles and ESI token coverage.

The views reuse these helpers to highlight characters that are missing
required scopes or that still have elevated roles without valid tokens.
"""

from allianceauth.authentication.models import CharacterOwnership
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from aa_bb.models import BigBrotherConfig

import logging

logger = logging.getLogger(__name__)

try:
    from corptools.models import CharacterRoles, CharacterAudit
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")

def get_user_roles(user_id):
    """
    Return a mapping of character name -> key roles held inside their corp.

    Pulling the CharacterRoles info once here keeps the template logic simple
    and avoids doing additional DB hits while rendering a table per user.
    """
    characters = CharacterOwnership.objects.filter(user__id=user_id).select_related("character")

    roles_dict = {}

    for ownership in characters:
        eve_char = ownership.character  # EveCharacter instance
        char_name = eve_char.character_name

        try:
            audit = CharacterAudit.objects.get(character=eve_char)
            char_roles = CharacterRoles.objects.get(character=audit)

            roles_dict[char_name] = {
                "director": char_roles.director,
                "accountant": char_roles.accountant,
                "station_manager": char_roles.station_manager,
                "personnel_manager": char_roles.personnel_manager,
                # you could add "titles": list(char_roles.titles.values_list("name", flat=True)) if needed
            }
        except (CharacterAudit.DoesNotExist, CharacterRoles.DoesNotExist):
            # no audit/roles available for this character
            roles_dict[char_name] = {
                "director": False,
                "accountant": False,
                "station_manager": False,
                "personnel_manager": False,
            }

    return roles_dict

def get_user_tokens(user_id):
    """
    Inspect which ESI scopes the user has granted for each character.

    We track both character audit availability and the presence of the
    configured corporation scopes so staff can quickly spot gaps.
    """
    from esi.models import Token, Scope

    CHARACTER_SCOPES = BigBrotherConfig.get_solo().character_scopes.split(",")

    CORPORATION_SCOPES = BigBrotherConfig.get_solo().corporation_scopes.split(",")

    characters = CharacterOwnership.objects.filter(user__id=user_id).select_related("character")
    tokens_dict = {}

    for ownership in characters:
        eve_char = ownership.character
        char_name = eve_char.character_name

        # Get all tokens for this character
        all_tokens = Token.objects.filter(character_id=eve_char.character_id, user_id=user_id)

        char_scopes_owned = set()
        corp_scopes_owned = set()

        for token in all_tokens:
            token_scopes = set(token.scopes.values_list("name", flat=True))
            # intersect with the sets of character/corp scopes to avoid unrelated scopes
            char_scopes_owned.update(token_scopes & set(CHARACTER_SCOPES))
            corp_scopes_owned.update(token_scopes & set(CORPORATION_SCOPES))

        missing_corporation_scopes = set(CORPORATION_SCOPES) - corp_scopes_owned

        has_corp_token = len(missing_corporation_scopes) == 0
        # If there is no CharacterAudit for this character, treat as non-compliant
        try:
            char_audit = CharacterAudit.objects.get(character=eve_char).active
        except CharacterAudit.DoesNotExist:
            char_audit = False

        tokens_dict[char_name] = {
            "character_token": char_audit,
            "corporation_token": has_corp_token,
            "missing_corporation_scopes": ", ".join(sorted(missing_corporation_scopes)),
        }

    return tokens_dict

def get_user_roles_and_tokens(user_id):
    """
    Merge the separate role/token dictionaries so a single lookup provides
    everything needed for both highlighting and CSV exports.
    """
    roles = get_user_roles(user_id)
    tokens = get_user_tokens(user_id)

    combined = {}

    # union of all characters in roles or tokens
    for char_name in set(roles.keys()) | set(tokens.keys()):
        combined[char_name] = {}
        if char_name in roles:  # Copy role flags when available.
            combined[char_name].update(roles[char_name])
        if char_name in tokens:  # Merge token coverage metadata.
            combined[char_name].update(tokens[char_name])

    return combined


def render_user_roles_tokens_html(user_id: int) -> str:
    """
    Returns an HTML snippet showing, for each of the user's characters:
      - director / accountant / station_manager / personnel_manager (True/False)
      - whether they have a character_token
      - whether they have a corporation_token
    """
    data = get_user_roles_and_tokens(user_id)
    html = ""

    for char_name, info in data.items():
        # header
        html += format_html("<h3>{}</h3>", char_name)

        # table start
        html += """
        <table class="table table-responsive compliance">
          <thead>
            <tr>
              <th>Attribute</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
        """

        # roles
        has_roles = False
        for key, label in (
            ("director", "Director"),
            ("accountant", "Accountant"),
            ("station_manager", "Station Manager"),
            ("personnel_manager", "Personnel Manager"),
        ):
            val = info.get(key, False)
            # highlight True roles in red if corporation_token is False
            if val:  # Highlight key roles even if tokens missing.
                val_txt = mark_safe('<span class="text-success">True</span>')
                has_roles = True
            else:
                val_txt = "False"
            html += format_html(
                "<tr><td>{}</td><td>{}</td></tr>", label, val_txt
            )

        # tokens
        for key, label in (
            ("character_token", "Character Token"),
            ("corporation_token", "Corporation Token"),
        ):
            val = info.get(key, False)
            # if character_token is False → make it red
            if key == "character_token" and not val:  # Character audit missing entirely.
                val_txt = mark_safe('<span class="text-danger">False</span> has no audit record.')
            elif key == "corporation_token" and not val:  # No corp token; severity depends on corp roles.
                if has_roles:  # Elevated corp roles without corp token is especially risky.
                    val_txt = mark_safe('<span class="text-danger">False</span> has elevated roles. A corporation token is expected.')
                else:
                    val_txt = mark_safe('False')
            else:
                val_txt = mark_safe('<span class="text-success">True</span>')
            html += format_html(
                "<tr><td>{}</td><td>{}</td></tr>", label, val_txt
            )

        # scopes
        for key, label in (
            ("missing_corporation_scopes", "Missing Corporation Scopes"),
        ):
            val = info.get(key, "")
            if val:  # only add row if non-empty
                html += format_html(
                    "<tr><td>{}</td><td colspan='2'>{}</td></tr>",
                    label,
                    mark_safe(val.replace(",", "<br>"))  # comma-separated string of missing scopes
                )

        # table end
        html += "</tbody></table>"

    return format_html(html)
