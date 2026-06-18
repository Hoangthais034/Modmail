from __future__ import annotations

from typing import Iterable, Optional

import discord
from discord import app_commands


async def send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """Send an ephemeral error or hint to the slash invoker."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def send_ephemeral_embed(interaction: discord.Interaction, embed: discord.Embed) -> None:
    """Send an ephemeral embed to the slash invoker."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def validate_action(
    action: Optional[str],
    allowed: Iterable[str],
    *,
    command_name: str = "command",
) -> Optional[str]:
    """Return an ephemeral error message when action is missing or invalid."""
    allowed_list = list(allowed)
    if not action:
        choices = ", ".join(f"`{name}`" for name in allowed_list)
        return f"Missing required `action`. Choose one of: {choices}."
    if action not in allowed_list:
        choices = ", ".join(f"`{name}`" for name in allowed_list)
        return f"Unknown action `{action}` for `/{command_name}`. Choose one of: {choices}."
    return None


def missing_param(name: str, *, for_action: Optional[str] = None) -> str:
    """Build a standard missing-parameter message for slash dispatch."""
    if for_action:
        return f"Missing required parameter `{name}` for action `{for_action}`."
    return f"Missing required parameter `{name}`."


async def reject_action(
    interaction: discord.Interaction,
    action: Optional[str],
    allowed: Iterable[str],
    *,
    command_name: str = "command",
) -> bool:
    """Validate action and send ephemeral error when invalid. Returns True if rejected."""
    error = validate_action(action, allowed, command_name=command_name)
    if error:
        await send_ephemeral(interaction, error)
        return True
    return False


async def reject_missing(
    interaction: discord.Interaction,
    value,
    param_name: str,
    *,
    for_action: Optional[str] = None,
) -> bool:
    """Send ephemeral error when a required slash parameter is absent."""
    if value is not None and value != "":
        return False
    await send_ephemeral(interaction, missing_param(param_name, for_action=for_action))
    return True


def static_action_autocomplete(choices: list[str]):
    """Return an autocomplete callback for a fixed list of action names (max 25 per Discord API)."""

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = (current or "").casefold()
        filtered = choices
        if current_lower:
            filtered = [name for name in choices if current_lower in name.casefold()]
        return [app_commands.Choice(name=name[:100], value=name) for name in filtered[:25]]

    return autocomplete
