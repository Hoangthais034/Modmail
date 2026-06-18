from __future__ import annotations

from difflib import get_close_matches

import discord
from discord import app_commands

from core.slash_dispatch import static_action_autocomplete

REPLY_MODES = [
    "normal",
    "anonymous",
    "plain",
    "plain_anonymous",
    "format",
    "format_anonymous",
    "format_plain",
    "format_plain_anonymous",
]

SNIPPET_ACTIONS = ["list", "view", "raw", "add", "remove", "edit"]
LOGS_ACTIONS = ["view", "closed_by", "key", "delete", "responded", "search"]
NOTE_ACTIONS = ["normal", "persistent"]
BLOCKED_ACTIONS = ["list", "whitelist", "block", "unblock"]
CONTROL_ACTIONS = ["enable", "disable_new", "disable_all", "status"]
SNOOZE_ACTIONS = ["snooze", "unsnooze", "list", "clear"]
DEBUG_ACTIONS = ["help", "hastebin", "clear"]
CONFIG_ACTIONS = ["help", "options", "get", "set", "remove"]
ALIAS_ACTIONS = ["view", "raw", "add", "remove", "edit"]
PERMISSIONS_ACTIONS = ["help", "override", "add", "remove", "get"]
OAUTH_ACTIONS = ["help", "whitelist", "show"]
AUTOTRIGGER_ACTIONS = ["list", "add", "edit", "remove", "test"]
PLUGINS_ACTIONS = [
    "help",
    "add",
    "remove",
    "update",
    "reset",
    "loaded",
    "registry",
    "registry_compact",
]
THREADMENU_ACTIONS = [
    "toggle",
    "show",
    "option_show",
    "option_remove",
    "option_edit",
    "dump_config",
    "reset",
    "load_config",
]

SNOOZE_DURATION_PRESETS = ["1h", "6h", "12h", "1d", "2d", "7d"]

reply_mode_autocomplete = static_action_autocomplete(REPLY_MODES)
snippet_action_autocomplete = static_action_autocomplete(SNIPPET_ACTIONS)
logs_action_autocomplete = static_action_autocomplete(LOGS_ACTIONS)
note_action_autocomplete = static_action_autocomplete(NOTE_ACTIONS)
blocked_action_autocomplete = static_action_autocomplete(BLOCKED_ACTIONS)
control_action_autocomplete = static_action_autocomplete(CONTROL_ACTIONS)
snooze_action_autocomplete = static_action_autocomplete(SNOOZE_ACTIONS)
debug_action_autocomplete = static_action_autocomplete(DEBUG_ACTIONS)
config_action_autocomplete = static_action_autocomplete(CONFIG_ACTIONS)
alias_action_autocomplete = static_action_autocomplete(ALIAS_ACTIONS)
permissions_action_autocomplete = static_action_autocomplete(PERMISSIONS_ACTIONS)
oauth_action_autocomplete = static_action_autocomplete(OAUTH_ACTIONS)
autotrigger_action_autocomplete = static_action_autocomplete(AUTOTRIGGER_ACTIONS)
plugins_action_autocomplete = static_action_autocomplete(PLUGINS_ACTIONS)
threadmenu_action_autocomplete = static_action_autocomplete(THREADMENU_ACTIONS)


def _format_recipient_choice(recipient: dict) -> tuple[str, str]:
    """Build display label and snowflake value for a log recipient document."""
    name = recipient.get("name", "Unknown")
    discriminator = recipient.get("discriminator", "0")
    if discriminator and discriminator != "0":
        name = f"{name}#{discriminator}"
    recipient_id = str(recipient.get("id", ""))
    return f"{name} ({recipient_id})", recipient_id


async def log_recipient_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest log recipients from MongoDB, newest closed threads first."""
    bot = interaction.client
    recipients = await bot.api.search_log_recipients(current, limit=25)
    choices = []
    for recipient in recipients[:25]:
        label, value = _format_recipient_choice(recipient)
        if value:
            choices.append(app_commands.Choice(name=label[:100], value=value))
    return choices


async def guild_member_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest guild members from the Modmail staff guild."""
    bot = interaction.client
    guild = bot.modmail_guild
    if guild is None:
        return []

    members = list(guild.members)
    current_lower = current.casefold()
    if current_lower:
        members = [
            member
            for member in members
            if current_lower in member.display_name.casefold() or current_lower in member.name.casefold()
        ]
    else:
        members = members[:10]

    choices = []
    for member in members[:25]:
        choices.append(
            app_commands.Choice(
                name=f"{member.display_name} ({member.id})"[:100],
                value=str(member.id),
            )
        )
    return choices


async def guild_role_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest guild roles from the Modmail staff guild."""
    bot = interaction.client
    guild = bot.modmail_guild
    if guild is None:
        return []

    roles = [role for role in guild.roles if role.name != "@everyone"]
    roles.sort(key=lambda role: role.position, reverse=True)
    current_lower = current.casefold()
    if current_lower:
        roles = [role for role in roles if current_lower in role.name.casefold()]

    choices = []
    for role in roles[:25]:
        choices.append(app_commands.Choice(name=role.name[:100], value=str(role.id)))
    return choices


async def category_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest Modmail guild categories with fuzzy name matching."""
    bot = interaction.client
    guild = bot.modmail_guild
    if guild is None:
        return []

    categories = {category.name.casefold(): category for category in guild.categories}
    if not current:
        selected = list(categories.values())
    else:
        matches = get_close_matches(current.casefold(), categories.keys(), n=25, cutoff=0.5)
        selected = [categories[match] for match in matches]

    choices = []
    for category in selected[:25]:
        choices.append(app_commands.Choice(name=category.name[:100], value=str(category.id)))
    return choices


async def snippet_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest snippet names configured on the bot."""
    bot = interaction.client
    names = sorted(bot.snippets.keys())
    current_lower = current.casefold()
    if current_lower:
        names = [name for name in names if name.casefold().startswith(current_lower)]

    choices = []
    for name in names[:25]:
        choices.append(app_commands.Choice(name=name[:100], value=name))
    return choices


async def alias_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest command alias names configured on the bot."""
    bot = interaction.client
    names = sorted(bot.aliases.keys())
    current_lower = current.casefold()
    if current_lower:
        names = [name for name in names if name.casefold().startswith(current_lower)]

    choices = []
    for name in names[:25]:
        choices.append(app_commands.Choice(name=name[:100], value=name))
    return choices


async def config_key_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest configuration keys from config help metadata."""
    bot = interaction.client
    keys = list(bot.config.config_help.keys())
    if not current:
        keys = keys[:10]
    else:
        keys = get_close_matches(current, keys, n=25, cutoff=0.5)

    choices = []
    for key in keys[:25]:
        choices.append(app_commands.Choice(name=key[:100], value=key))
    return choices


async def command_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest registered bot command qualified names."""
    bot = interaction.client
    names = sorted({command.qualified_name for command in bot.walk_commands() if not command.hidden})
    current_lower = current.casefold()
    if current_lower:
        names = [name for name in names if current_lower in name.casefold()]

    choices = []
    for name in names[:25]:
        choices.append(app_commands.Choice(name=name[:100], value=name))
    return choices


async def log_key_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest recent Modmail log keys from MongoDB."""
    bot = interaction.client
    keys = await bot.api.search_log_keys(current, limit=25)
    choices = []
    for key in keys[:25]:
        choices.append(app_commands.Choice(name=key[:100], value=key))
    return choices


async def plugin_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest plugin names from the registry and installed plugins."""
    bot = interaction.client
    names = set(bot.config.get("plugins") or [])
    plugins_cog = bot.get_cog("Plugins")
    if plugins_cog is not None:
        names.update(getattr(plugins_cog, "registry", {}).keys())

    names = sorted(names)
    current_lower = current.casefold()
    if current_lower:
        names = [name for name in names if current_lower in name.casefold()]

    choices = []
    for name in names[:25]:
        choices.append(app_commands.Choice(name=name[:100], value=name))
    return choices


async def snooze_duration_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest snooze durations with the configured default first."""
    bot = interaction.client
    default = bot.config.get("snooze_default_duration") or "1d"
    presets = []
    for value in [default, *SNOOZE_DURATION_PRESETS]:
        if value not in presets:
            presets.append(value)

    current_lower = (current or "").casefold()
    if current_lower:
        presets = [value for value in presets if current_lower in value.casefold()]

    return [app_commands.Choice(name=value[:100], value=value) for value in presets[:25]]


async def threadmenu_label_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest thread-creation menu option labels from core config."""
    bot = interaction.client
    labels = []
    options = bot.config.get("thread_creation_menu_options") or {}
    for key, option in options.items():
        label = option.get("label") or key
        labels.append((label, key))

    submenus = bot.config.get("thread_creation_menu_submenus") or {}
    for submenu_name, submenu_options in submenus.items():
        for key, option in submenu_options.items():
            label = option.get("label") or key
            labels.append((f"{submenu_name}: {label}", key))

    current_lower = current.casefold()
    if current_lower:
        labels = [(label, value) for label, value in labels if current_lower in label.casefold()]

    choices = []
    for label, value in labels[:25]:
        choices.append(app_commands.Choice(name=label[:100], value=value))
    return choices
