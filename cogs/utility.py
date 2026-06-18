from core.utils import trigger_typing, truncate
import asyncio
import inspect
import os
import random
import re
import traceback
from contextlib import redirect_stdout
from difflib import get_close_matches
from io import BytesIO, StringIO
from itertools import takewhile, zip_longest
from json import JSONDecodeError, loads
from subprocess import PIPE
from textwrap import indent
from typing import Optional, Union
import typing

import discord
from discord import app_commands
from discord.enums import ActivityType, Status
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView

from aiohttp import ClientResponseError
from packaging.version import Version

from core import checks, slash_dispatch, utils
from core.autocomplete import (
    ALIAS_ACTIONS,
    AUTOTRIGGER_ACTIONS,
    CONFIG_ACTIONS,
    DEBUG_ACTIONS,
    OAUTH_ACTIONS,
    PERMISSIONS_ACTIONS,
    alias_action_autocomplete,
    alias_autocomplete,
    autotrigger_action_autocomplete,
    command_name_autocomplete,
    config_action_autocomplete,
    config_key_autocomplete,
    debug_action_autocomplete,
    guild_member_autocomplete,
    guild_role_autocomplete,
    oauth_action_autocomplete,
    permissions_action_autocomplete,
)
from core.changelog import Changelog
from core.models import (
    HostingMethod,
    InvalidConfigError,
    PermissionLevel,
    UnseenFormatter,
    getLogger,
)
from core.utils import DummyParam
from core.paginator import EmbedPaginatorSession, MessagePaginatorSession


logger = getLogger(__name__)

_ACTIVITY_TYPES = ("playing", "streaming", "listening", "watching", "competing", "custom", "clear")
_STATUS_TYPES = (
    ("online", "online"),
    ("idle", "idle"),
    ("dnd", "dnd"),
    ("do not disturb", "dnd"),
    ("invisible", "invisible"),
    ("offline", "offline"),
    ("clear", "clear"),
)
_PERMISSION_LEVELS = ("OWNER", "ADMINISTRATOR", "MODERATOR", "SUPPORTER", "REGULAR", "1", "2", "3", "4", "5")
_PERMISSION_TYPES = ("command", "level", "override")


async def member_role_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest guild members and roles for slash parameters."""
    members = await guild_member_autocomplete(interaction, current)
    roles = await guild_role_autocomplete(interaction, current)
    return (members + roles)[:25]


async def changelog_version_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest changelog version tags from the remote changelog data."""
    bot = interaction.client
    try:
        changelog = await Changelog.from_url(bot)
        versions = [version.version for version in changelog.versions]
    except Exception:
        return []

    current_lower = current.casefold().lstrip("v")
    if current_lower:
        versions = [version for version in versions if current_lower in version.casefold()]

    choices = []
    for version in versions[:25]:
        choices.append(app_commands.Choice(name=f"v{version}"[:100], value=version))
    return choices


async def activity_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest fixed bot activity types for slash parameters."""
    current_lower = current.casefold()
    choices = []
    for activity_type in _ACTIVITY_TYPES:
        if not current_lower or activity_type.startswith(current_lower):
            choices.append(app_commands.Choice(name=activity_type, value=activity_type))
    return choices[:25]


async def status_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest fixed bot status types for slash parameters."""
    current_lower = current.casefold()
    choices = []
    for display_name, value in _STATUS_TYPES:
        if not current_lower or display_name.startswith(current_lower) or value.startswith(current_lower):
            choices.append(app_commands.Choice(name=display_name, value=value))
    return choices[:25]


async def permission_level_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest permission level names and numeric shortcuts."""
    current_lower = current.casefold()
    choices = []
    for level_name in _PERMISSION_LEVELS:
        if not current_lower or current_lower in level_name.casefold():
            choices.append(app_commands.Choice(name=level_name, value=level_name))
    return choices[:25]


async def permission_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest permission subcommand target types."""
    current_lower = current.casefold()
    choices = []
    for type_name in _PERMISSION_TYPES:
        if not current_lower or type_name.startswith(current_lower):
            choices.append(app_commands.Choice(name=type_name, value=type_name))
    return choices[:25]


async def permissions_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest command names and permission levels for permissions add/remove/get."""
    commands = await command_name_autocomplete(interaction, current)
    levels = await permission_level_autocomplete(interaction, current)
    return (commands + levels)[:25]


async def autotrigger_keyword_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest configured autotrigger keywords."""
    bot = interaction.client
    keywords = sorted(bot.auto_triggers.keys())
    current_lower = current.casefold()
    if current_lower:
        keywords = [keyword for keyword in keywords if current_lower in keyword.casefold()]

    choices = []
    for keyword in keywords[:25]:
        choices.append(app_commands.Choice(name=keyword[:100], value=keyword))
    return choices


async def mention_target_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest members, roles, and mention preset keywords."""
    choices = []
    current_lower = (current or "").casefold()
    for keyword in ("disable", "reset", "everyone", "all"):
        if not current_lower or keyword.startswith(current_lower):
            choices.append(app_commands.Choice(name=keyword, value=keyword))
    choices.extend(await member_role_autocomplete(interaction, current))
    return choices[:25]


class ModmailHelpCommand(commands.HelpCommand):
    async def command_callback(self, ctx, *, command=None):
        """Overwrites original command_callback to ensure `help` without any arguments
        returns with checks, `help all` returns without checks"""
        if command is None:
            self.verify_checks = True
        else:
            self.verify_checks = False

        if command == "all":
            command = None

        return await super().command_callback(ctx, command=command)

    async def format_cog_help(self, cog, *, no_cog=False):
        bot = self.context.bot
        prefix = self.context.clean_prefix

        formats = [""]
        for cmd in await self.filter_commands(
            cog.get_commands() if not no_cog else cog,
            sort=True,
            key=lambda c: (bot.command_perm(c.qualified_name), c.qualified_name),
        ):
            perm_level = bot.command_perm(cmd.qualified_name)
            if perm_level is PermissionLevel.INVALID:
                format_ = f"`{prefix + cmd.qualified_name}` "
            else:
                format_ = f"`[{perm_level}] {prefix + cmd.qualified_name}` "

            format_ += f"- {cmd.short_doc}\n" if cmd.short_doc else "- *No description.*\n"
            if not format_.strip():
                continue
            if len(format_) + len(formats[-1]) >= 1024:
                formats.append(format_)
            else:
                formats[-1] += format_

        embeds = []
        for format_ in formats:
            description = (
                cog.description or "No description."
                if not no_cog
                else "Miscellaneous commands without a category."
            )
            embed = discord.Embed(description=f"*{description}*", color=bot.main_color)

            if not format_:
                continue

            embed.add_field(name="Commands", value=format_ or "No commands.")

            name = cog.qualified_name + " - Help" if not no_cog else "Miscellaneous Commands"
            embed.set_author(
                name=name, icon_url=bot.user.display_avatar.url if bot.user.display_avatar else None
            )

            embed.set_footer(
                text=f'Type "{prefix}{self.command_attrs["name"]} command" '
                "for more info on a specific command."
            )
            embeds.append(embed)

        if len(embeds) > 1:
            for n, em in enumerate(embeds):
                em.set_author(name=f"{em.author.name} [{n + 1}]", icon_url=em.author.icon_url)

        return embeds

    def process_help_msg(self, help_: str):
        return help_.format(prefix=self.context.clean_prefix) if help_ else "No help message."

    async def send_bot_help(self, mapping):
        embeds = []
        no_cog_commands = sorted(mapping.pop(None), key=lambda c: c.qualified_name)
        cogs = sorted(mapping, key=lambda c: c.qualified_name)

        bot = self.context.bot

        # always come first
        default_cogs = [
            bot.get_cog("Modmail"),
            bot.get_cog("Utility"),
            bot.get_cog("Plugins"),
        ]

        default_cogs.extend(c for c in cogs if c not in default_cogs)

        for cog in default_cogs:
            embeds.extend(await self.format_cog_help(cog))
        if no_cog_commands:
            embeds.extend(await self.format_cog_help(no_cog_commands, no_cog=True))

        slash_lines = [
            "`/reply` `/snippet` `/logs` `/note` `/blocked` `/control` `/snooze`",
            "`/debug` `/config` `/alias` `/permissions` `/oauth` `/autotrigger`",
            "`/plugins` `/threadmenu`",
            "",
            "Pick an `action` from autocomplete on each command.",
        ]
        slash_embed = discord.Embed(
            title="Slash commands",
            description="\n".join(slash_lines),
            color=bot.main_color,
        )
        embeds.insert(0, slash_embed)

        session = EmbedPaginatorSession(self.context, *embeds, destination=self.get_destination())
        return await session.run()

    async def send_cog_help(self, cog):
        embeds = await self.format_cog_help(cog)
        session = EmbedPaginatorSession(self.context, *embeds, destination=self.get_destination())
        return await session.run()

    async def _get_help_embed(self, topic):
        if not await self.filter_commands([topic]):
            return
        perm_level = self.context.bot.command_perm(topic.qualified_name)
        if perm_level is not PermissionLevel.INVALID:
            perm_level = f"{perm_level.name} [{perm_level}]"
        else:
            perm_level = "NONE"

        embed = discord.Embed(
            title=f"`{self.get_command_signature(topic).strip()}`",
            color=self.context.bot.main_color,
            description=self.process_help_msg(topic.help),
        )
        return embed, perm_level

    async def send_command_help(self, command):
        topic = await self._get_help_embed(command)
        if topic is not None:
            topic[0].set_footer(text=f"Permission level: {topic[1]}")
            await self.get_destination().send(embed=topic[0])

    async def send_group_help(self, group):
        topic = await self._get_help_embed(group)
        if topic is None:
            return
        embed = topic[0]
        embed.add_field(name="Permission Level", value=topic[1], inline=False)

        format_ = ""
        length = len(group.commands)

        for i, command in enumerate(
            await self.filter_commands(group.commands, sort=True, key=lambda c: c.name)
        ):
            # BUG: fmt may run over the embed limit
            # TODO: paginate this
            if length == i + 1:  # last
                branch = "└─"
            else:
                branch = "├─"
            format_ += f"`{branch} {command.name}` - {command.short_doc}\n"

        embed.add_field(name="Sub Command(s)", value=format_[:1024], inline=False)
        embed.set_footer(
            text=f'Type "{self.context.clean_prefix}{self.command_attrs["name"]} command" '
            "for more info on a command."
        )

        await self.get_destination().send(embed=embed)

    async def send_error_message(self, error):
        command = self.context.kwargs.get("command")
        val = self.context.bot.snippets.get(command)
        if val is not None:
            embed = discord.Embed(title=f"{command} is a snippet.", color=self.context.bot.main_color)
            embed.add_field(name=f"`{command}` will send:", value=val, inline=False)

            snippet_aliases = []
            for alias in self.context.bot.aliases:
                if self.context.bot._resolve_snippet(alias) == command:
                    snippet_aliases.append(f"`{alias}`")

            if snippet_aliases:
                embed.add_field(
                    name="Aliases to this snippet:",
                    value=",".join(snippet_aliases),
                    inline=False,
                )

            return await self.get_destination().send(embed=embed)

        val = self.context.bot.aliases.get(command)
        if val is not None:
            values = utils.parse_alias(val)

            if not values:
                embed = discord.Embed(
                    title="Error",
                    color=self.context.bot.error_color,
                    description=f"Alias `{command}` is invalid, this alias will now be deleted."
                    "This alias will now be deleted.",
                )
                embed.add_field(name=f"{command}` used to be:", value=val)
                self.context.bot.aliases.pop(command)
                await self.context.bot.config.update()
            else:
                if len(values) == 1:
                    embed = discord.Embed(
                        title=f"{command} is an alias.",
                        color=self.context.bot.main_color,
                    )
                    embed.add_field(name=f"`{command}` points to:", value=values[0])
                else:
                    embed = discord.Embed(
                        title=f"{command} is an alias.",
                        color=self.context.bot.main_color,
                        description=f"**`{command}` points to the following steps:**",
                    )
                    for i, val in enumerate(values, start=1):
                        embed.add_field(name=f"Step {i}:", value=val)

            embed.set_footer(
                text=f'Type "{self.context.clean_prefix}{self.command_attrs["name"]} alias" '
                "for more details on aliases."
            )
            return await self.get_destination().send(embed=embed)

        logger.warning("CommandNotFound: %s", error)

        embed = discord.Embed(color=self.context.bot.error_color)
        embed.set_footer(text=f'Command/Category "{command}" not found.')

        choices = set()

        for cmd in self.context.bot.walk_commands():
            if not cmd.hidden:
                choices.add(cmd.qualified_name)

        closest = get_close_matches(command, choices)
        if closest:
            embed.add_field(name="Perhaps you meant:", value="\n".join(f"`{x}`" for x in closest))
        else:
            embed.title = "Cannot find command or category"
            embed.set_footer(
                text=f'Type "{self.context.clean_prefix}{self.command_attrs["name"]}" '
                "for a list of all available commands."
            )
        await self.get_destination().send(embed=embed)


class Utility(commands.Cog):
    """General commands that provide utility."""

    def __init__(self, bot):
        self.bot = bot
        self._original_help_command = bot.help_command
        self._custom_help = ModmailHelpCommand(
            command_attrs={
                "help": "Shows this help message.",
                "checks": [checks.has_permissions_predicate(PermissionLevel.REGULAR)],
            },
        )
        self._custom_help.cog = self
        self.bot.help_command = None
        if not self.bot.config.get("enable_eval"):
            self.eval_.enabled = False
            logger.info("Eval disabled. enable_eval=False")

    def _guilds(self) -> list[discord.Object]:
        """Return guild scopes for slash command registration."""
        guilds = []
        if self.bot.guild_id:
            guilds.append(discord.Object(id=self.bot.guild_id))
        if self.bot.using_multiple_server_setup:
            modmail_guild_id = self.bot.config.get("modmail_guild_id")
            if modmail_guild_id is not None:
                try:
                    guilds.append(discord.Object(id=int(modmail_guild_id)))
                except (TypeError, ValueError):
                    pass
        return guilds

    def _collect_users(self, *users) -> list:
        """Gather optional slash user parameters into a list."""
        return [user for user in users if user is not None]

    def _get_prefix_rest(self, ctx) -> str:
        """Return the unparsed argument string from a prefix command message."""
        message = ctx.message.content
        prefix = ctx.prefix
        if isinstance(prefix, list):
            prefix = next((p for p in prefix if message.startswith(p)), "")
        if prefix and message.startswith(prefix):
            rest = message[len(prefix) :].lstrip()
        else:
            rest = message
        for part in ctx.command.qualified_name.split():
            if rest.lower().startswith(part.lower()):
                rest = rest[len(part) :].lstrip()
        return rest

    async def _parse_mention_targets_from_rest(self, ctx) -> list:
        """Parse greedy mention targets from prefix command rest."""
        from discord.ext.commands.view import StringView

        rest = self._get_prefix_rest(ctx)
        if not rest:
            return []

        view = StringView(rest)
        result = []
        while not view.eof:
            view.skip_ws()
            if view.eof:
                break
            word = view.get_quoted_word()
            if word.lower() in {"disable", "reset", "everyone", "all"}:
                result.append(word.lower())
                continue
            try:
                result.append(await commands.MemberConverter().convert(ctx, word))
            except commands.BadArgument:
                try:
                    result.append(await commands.RoleConverter().convert(ctx, word))
                except commands.BadArgument:
                    result.append(word)
        return result

    async def _resolve_member_role(self, ctx, target):
        """Resolve a member or role from slash autocomplete snowflakes."""
        if target is None:
            return None
        if isinstance(target, (discord.Member, discord.Role)):
            return target
        if isinstance(target, utils.User):
            guild = ctx.guild or self.bot.modmail_guild
            if guild is not None:
                member = guild.get_member(target.id)
                if member is not None:
                    return member
            return target
        if isinstance(target, str):
            lowered = target.casefold()
            if lowered in ("everyone", "all"):
                return lowered
            guild = ctx.guild or self.bot.modmail_guild
            if guild is not None and target.isdigit():
                member = guild.get_member(int(target))
                if member is not None:
                    return member
                role = guild.get_role(int(target))
                if role is not None:
                    return role
            try:
                return await commands.MemberConverter().convert(ctx, target)
            except commands.BadArgument:
                pass
            try:
                return await commands.RoleConverter().convert(ctx, target)
            except commands.BadArgument:
                pass
            try:
                return await utils.User().convert(ctx, target)
            except commands.BadArgument:
                pass
        return target

    async def _resolve_mention_target(self, ctx, target):
        """Resolve mention command targets from slash autocomplete values."""
        if target is None:
            return None
        if isinstance(target, (discord.Member, discord.Role)):
            return target
        if isinstance(target, str):
            lowered = target.casefold()
            if lowered in ("disable", "reset", "everyone", "all"):
                return lowered
            guild = ctx.guild or self.bot.modmail_guild
            if guild is not None and target.isdigit():
                member = guild.get_member(int(target))
                if member is not None:
                    return member
                role = guild.get_role(int(target))
                if role is not None:
                    return role
        return target

    async def cog_load(self):
        guilds = self._guilds()
        if guilds:
            for command in self.walk_app_commands():
                command.guilds = guilds
        self.loop_presence.start()  # pylint: disable=no-member

    def cog_unload(self):
        self.bot.help_command = self._original_help_command

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.REGULAR)
    @utils.trigger_typing
    @app_commands.describe(command="Command or category to get help for")
    @app_commands.autocomplete(command=command_name_autocomplete)
    async def help(self, ctx, *, command: str = None):
        """Shows this help message."""
        await self._custom_help.command_callback(ctx, command=command)

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.REGULAR)
    @utils.trigger_typing
    @app_commands.describe(version="Changelog version tag to display")
    @app_commands.autocomplete(version=changelog_version_autocomplete)
    async def changelog(self, ctx, version: str.lower = ""):
        """Shows the changelog of the Modmail."""
        changelog = await Changelog.from_url(self.bot)
        version = version.lstrip("v") if version else changelog.latest_version.version

        try:
            index = [v.version for v in changelog.versions].index(version)
        except ValueError:
            return await ctx.send(
                embed=discord.Embed(
                    color=self.bot.error_color,
                    description=f"The specified version `{version}` could not be found.",
                )
            )

        paginator = EmbedPaginatorSession(ctx, *changelog.embeds)
        try:
            paginator.current = index
            await paginator.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await paginator.close()
            finally:
                logger.warning("Failed to display changelog.", exc_info=True)
                await ctx.send(
                    f"View the changelog here: {changelog.latest_version.changelog_url}#v{version[::2]}"
                )

    @commands.hybrid_command(aliases=["info"])
    @checks.has_permissions(PermissionLevel.REGULAR)
    @utils.trigger_typing
    async def about(self, ctx):
        """Shows information about this bot."""
        embed = discord.Embed(color=self.bot.main_color, timestamp=discord.utils.utcnow())
        embed.set_author(
            name="Modmail - About",
            icon_url=self.bot.user.display_avatar.url if self.bot.user.display_avatar else None,
            url="https://discord.gg/F34cRU8",
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url if self.bot.user.display_avatar else None)

        desc = "This is an open source Discord bot that serves as a means for "
        desc += "members to easily communicate with server administrators in "
        desc += "an organised manner."
        embed.description = desc

        embed.add_field(name="Uptime", value=self.bot.uptime)
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.2f} ms")
        embed.add_field(name="Version", value=f"`{self.bot.version}`")
        embed.add_field(name="Authors", value="`kyb3r`, `Taki`, `fourjr`")
        embed.add_field(name="Hosting Method", value=self.bot.hosting_method.name)

        changelog = await Changelog.from_url(self.bot)
        latest = changelog.latest_version

        if self.bot.version.is_prerelease:
            stable = next(filter(lambda v: not Version(v.version).is_prerelease, changelog.versions))
            footer = f"You are on the prerelease version • the latest version is v{stable.version}."
        elif self.bot.version < Version(latest.version):
            footer = f"A newer version is available v{latest.version}."
        else:
            footer = "You are up to date with the latest version."

        embed.add_field(
            name="Want Modmail in Your Server?",
            value="Follow the installation guide on [GitHub](https://github.com/modmail-dev/modmail/) "
            "and join our [Discord server](https://discord.gg/cnUpwrnpYb)!",
            inline=False,
        )

        embed.add_field(
            name="Support the Developers",
            value="This bot is completely free for everyone. We rely on kind individuals "
            "like you to support us on [`Buy Me A Coffee`](https://buymeacoffee.com/modmaildev) (perks included for memberships) "
            "to keep this bot free forever!",
            inline=False,
        )

        embed.add_field(
            name="Project Sponsors",
            value=f"Checkout the people who supported Modmail with command `{self.bot.prefix}sponsors`!",
            inline=False,
        )

        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["sponsor"])
    @checks.has_permissions(PermissionLevel.REGULAR)
    @utils.trigger_typing
    async def sponsors(self, ctx):
        """Shows the sponsors of this project."""

        async with self.bot.session.get(
            "https://raw.githubusercontent.com/modmail-dev/modmail/master/SPONSORS.json"
        ) as resp:
            data = loads(await resp.text())

        embeds = []

        for elem in data:
            embed = discord.Embed.from_dict(elem["embed"])
            embeds.append(embed)

        random.shuffle(embeds)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    async def _debug_logs(self, ctx):
        """Show recent application logs in paginated messages."""
        with open(self.bot.log_file_path, "r+", encoding="utf-8") as f:
            logs = f.read().strip()

        if not logs:
            embed = discord.Embed(
                color=self.bot.main_color,
                title="Debug Logs:",
                description="You don't have any logs at the moment.",
            )
            embed.set_footer(text="Go to your console to see your logs.")
            return await ctx.send(embed=embed)

        messages = []

        msg = "```Haskell\n"

        for line in logs.splitlines(keepends=True):
            if msg != "```Haskell\n":
                if len(line) + len(msg) + 3 > 2000:
                    msg += "```"
                    messages.append(msg)
                    msg = "```Haskell\n"
            msg += line
            if len(msg) + 3 > 2000:
                msg = msg[:1992] + "[...]```"
                messages.append(msg)
                msg = "```Haskell\n"

        if msg != "```Haskell\n":
            msg += "```"
            messages.append(msg)

        embed = discord.Embed(color=self.bot.main_color)
        embed.set_footer(text="Debug logs - Navigate using the reactions below.")

        session = MessagePaginatorSession(ctx, *messages, embed=embed)
        session.current = len(messages) - 1
        return await session.run()

    async def _debug_hastebin(self, ctx, attachment: Optional[discord.Attachment] = None):
        """Upload application logs or an attachment to Hastebin."""
        haste_url = os.environ.get("HASTE_URL", "https://hastebin.cc")

        if attachment is not None:
            logs = BytesIO(await attachment.read())
        else:
            with open(self.bot.log_file_path, "rb+") as f:
                logs = BytesIO(f.read().strip())

        try:
            async with self.bot.session.post(haste_url + "/documents", data=logs) as resp:
                data = await resp.json()
                try:
                    key = data["key"]
                except KeyError:
                    logger.error(data["message"])
                    raise
                embed = discord.Embed(
                    title="Debug Logs",
                    color=self.bot.main_color,
                    description=f"{haste_url}/" + key,
                )
        except (JSONDecodeError, ClientResponseError, IndexError, KeyError):
            embed = discord.Embed(
                title="Debug Logs",
                color=self.bot.main_color,
                description="Something's wrong. We're unable to upload your logs to hastebin.",
            )
            embed.set_footer(text="Go to your console to see your logs.")
        await ctx.send(embed=embed)

    async def _debug_clear(self, ctx):
        """Clear locally cached application logs."""
        with open(self.bot.log_file_path, "w"):
            pass
        await ctx.send(
            embed=discord.Embed(color=self.bot.main_color, description="Cached logs are now cleared.")
        )

    @app_commands.command(name="debug", description="View, upload, or clear application debug logs.")
    @app_commands.describe(
        action="Debug action to perform",
        attachment="Log file to upload for hastebin (defaults to bot log file)",
    )
    @app_commands.autocomplete(action=debug_action_autocomplete)
    @checks.slash_has_permissions(PermissionLevel.OWNER)
    async def debug_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        attachment: Optional[discord.Attachment] = None,
    ):
        """Dispatch merged slash debug actions."""
        ctx = await checks.InteractionContext.from_interaction(interaction)
        if await slash_dispatch.reject_action(interaction, action, DEBUG_ACTIONS, command_name="debug"):
            return

        await ctx.defer()

        if action == "help":
            return await self._debug_logs(ctx)
        if action == "hastebin":
            return await self._debug_hastebin(ctx, attachment)
        if action == "clear":
            return await self._debug_clear(ctx)

    @commands.hybrid_command(aliases=["presence"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @app_commands.describe(
        activity_type="Bot activity type",
        message="Activity message text",
    )
    @app_commands.autocomplete(activity_type=activity_type_autocomplete)
    async def activity(self, ctx, activity_type: str.lower, *, message: str = ""):
        """
        Set an activity status for the bot.

        Possible activity types:
            - `playing`
            - `streaming`
            - `listening`
            - `watching`
            - `competing`
            - `custom`

        When activity type is set to `listening`,
        it must be followed by a "to": "listening to..."

        When activity type is set to `competing`,
        it must be followed by a "in": "competing in..."

        When activity type is set to `streaming`, you can set
        the linked twitch page:
        - `{prefix}config set twitch_url https://www.twitch.tv/somechannel/`

        When activity type is set to `custom`, you can set
        any custom text as the activity message.

        To remove the current activity status:
        - `{prefix}activity clear`
        """
        if activity_type == "clear":
            self.bot.config.remove("activity_type")
            self.bot.config.remove("activity_message")
            await self.bot.config.update()
            await self.set_presence()
            embed = discord.Embed(title="Activity Removed", color=self.bot.main_color)
            return await ctx.send(embed=embed)

        if not message:
            raise commands.MissingRequiredArgument(DummyParam("message"))

        try:
            activity_type = ActivityType[activity_type]
        except KeyError:
            raise commands.MissingRequiredArgument(DummyParam("activity"))

        activity, _ = await self.set_presence(activity_type=activity_type, activity_message=message)

        self.bot.config["activity_type"] = activity.type.value
        self.bot.config["activity_message"] = activity.name
        await self.bot.config.update()

        msg = f"Activity set to: {activity.type.name.capitalize()} "
        if activity.type == ActivityType.listening:
            msg += f"to {activity.name}."
        elif activity.type == ActivityType.competing:
            msg += f"in {activity.name}."
        else:
            msg += f"{activity.name}."

        embed = discord.Embed(title="Activity Changed", description=msg, color=self.bot.main_color)
        return await ctx.send(embed=embed)

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @app_commands.describe(status_type="Bot status type")
    @app_commands.autocomplete(status_type=status_type_autocomplete)
    async def status(self, ctx, *, status_type: str.lower):
        """
        Set a status for the bot.

        Possible status types:
            - `online`
            - `idle`
            - `dnd` or `do not disturb`
            - `invisible` or `offline`

        To remove the current status:
        - `{prefix}status clear`
        """
        if status_type == "clear":
            self.bot.config.remove("status")
            await self.bot.config.update()
            await self.set_presence()
            embed = discord.Embed(title="Status Removed", color=self.bot.main_color)
            return await ctx.send(embed=embed)

        status_type = status_type.replace(" ", "_")
        try:
            status = Status[status_type]
        except KeyError:
            raise commands.MissingRequiredArgument(DummyParam("status"))

        _, status = await self.set_presence(status=status)

        self.bot.config["status"] = status.value
        await self.bot.config.update()

        msg = f"Status set to: {status.value}."
        embed = discord.Embed(title="Status Changed", description=msg, color=self.bot.main_color)
        return await ctx.send(embed=embed)

    async def set_presence(self, *, status=None, activity_type=None, activity_message=None):
        if status is None:
            status = self.bot.config.get("status")

        if activity_type is None:
            activity_type = self.bot.config.get("activity_type")

        url = None
        activity_message = (activity_message or self.bot.config["activity_message"]).strip()
        if activity_type is not None and not activity_message:
            logger.warning('No activity message found whilst activity is provided, defaults to "Modmail".')
            activity_message = "Modmail"

        if activity_type == ActivityType.listening:
            if activity_message.lower().startswith("to "):
                # The actual message is after listening to [...]
                # discord automatically add the "to"
                activity_message = activity_message[3:].strip()
        elif activity_type == ActivityType.competing:
            if activity_message.lower().startswith("in "):
                # The actual message is after listening to [...]
                # discord automatically add the "in"
                activity_message = activity_message[3:].strip()
        elif activity_type == ActivityType.streaming:
            url = self.bot.config["twitch_url"]

        if activity_type == ActivityType.custom:
            activity = discord.CustomActivity(name=activity_message)
        elif activity_type is not None:
            activity = discord.Activity(type=activity_type, name=activity_message, url=url)
        else:
            activity = None
        await self.bot.change_presence(activity=activity, status=status)

        return activity, status

    @tasks.loop(minutes=30)
    async def loop_presence(self):
        """Set presence to the configured value every 30 minutes."""
        logger.debug("Resetting presence.")
        await self.set_presence()

    @loop_presence.before_loop
    async def before_loop_presence(self):
        await self.bot.wait_for_connected()
        logger.line()
        activity, status = await self.set_presence()

        if activity is not None:
            msg = f"Activity set to: {activity.type.name.capitalize()} "
            if activity.type == ActivityType.listening:
                msg += f"to {activity.name}."
            else:
                msg += f"{activity.name}."
            logger.info(msg)
        else:
            logger.info("No activity has been set.")
        if status is not None:
            msg = f"Status set to: {status.value}."
            logger.info(msg)
        else:
            logger.info("No status has been set.")

        await asyncio.sleep(1800)
        logger.info("Starting presence loop.")

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @utils.trigger_typing
    async def ping(self, ctx):
        """Pong! Returns your websocket latency."""
        embed = discord.Embed(
            title="Pong! Websocket Latency:",
            description=f"{self.bot.ws.latency * 1000:.4f} ms",
            color=self.bot.main_color,
        )
        return await ctx.send(embed=embed)

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @app_commands.describe(
        user1="First user or role to mention",
        user2="Second user or role to mention",
        user3="Third user or role to mention",
        user4="Fourth user or role to mention",
        user5="Fifth user or role to mention",
    )
    @app_commands.autocomplete(
        user1=mention_target_autocomplete,
        user2=mention_target_autocomplete,
        user3=mention_target_autocomplete,
        user4=mention_target_autocomplete,
        user5=mention_target_autocomplete,
    )
    async def mention(
        self,
        ctx,
        user1: Optional[str] = None,
        user2: Optional[str] = None,
        user3: Optional[str] = None,
        user4: Optional[str] = None,
        user5: Optional[str] = None,
    ):
        """
        Change what the bot mentions at the start of each thread.

        `user_or_role` may be a user ID, mention, name, role ID, mention, or name.
        You can also set it to mention multiple users or roles, just separate the arguments with space.

        Examples:
        - `{prefix}mention @user`
        - `{prefix}mention @user @role`
        - `{prefix}mention 984301093849028 388218663326449`
        - `{prefix}mention everyone`

        Do not ping `@everyone` to set mention to everyone, use "everyone" or "all" instead.

        Notes:
        - Type only `{prefix}mention` to retrieve your current "mention" message.
        - `{prefix}mention disable` to disable mention.
        - `{prefix}mention reset` to reset it to default value, which is "@here".
        """
        if ctx.interaction is not None:
            raw_targets = self._collect_users(user1, user2, user3, user4, user5)
            user_or_role = []
            for target in raw_targets:
                resolved = await self._resolve_mention_target(ctx, target)
                if resolved is not None:
                    user_or_role.append(resolved)
        else:
            user_or_role = await self._parse_mention_targets_from_rest(ctx)

        current = self.bot.config["mention"]
        if not user_or_role:
            embed = discord.Embed(
                title="Current mention:",
                color=self.bot.main_color,
                description=str(current),
            )
        elif (
            len(user_or_role) == 1
            and isinstance(user_or_role[0], str)
            and user_or_role[0].lower() in ("disable", "reset")
        ):
            option = user_or_role[0].lower()
            if option == "disable":
                embed = discord.Embed(
                    description="Disabled mention on thread creation.",
                    color=self.bot.main_color,
                )
                self.bot.config["mention"] = None
            else:
                embed = discord.Embed(
                    description="`mention` is reset to default.",
                    color=self.bot.main_color,
                )
                self.bot.config.remove("mention")
            await self.bot.config.update()
        else:
            mention = []
            everyone = ("all", "everyone")
            for m in user_or_role:
                if not isinstance(m, (discord.Role, discord.Member)) and m not in everyone:
                    raise commands.BadArgument(f'Role or Member "{m}" not found.')
                elif m == ctx.guild.default_role or m in everyone:
                    mention.append("@everyone")
                    continue
                mention.append(m.mention)

            mention = " ".join(mention)
            embed = discord.Embed(
                title="Changed mention!",
                description=f'On thread creation the bot now says "{mention}".',
                color=self.bot.main_color,
            )
            self.bot.config["mention"] = mention
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @app_commands.describe(prefix="New bot command prefix")
    async def prefix(self, ctx, *, prefix=None):
        """
        Change the prefix of the bot.

        Type only `{prefix}prefix` to retrieve your current bot prefix.
        """

        current = self.bot.prefix
        embed = discord.Embed(title="Current prefix", color=self.bot.main_color, description=f"{current}")

        if prefix is None:
            await ctx.send(embed=embed)
        else:
            embed.title = "Changed prefix!"
            embed.description = f"Set prefix to `{prefix}`"
            self.bot.config["prefix"] = prefix
            await self.bot.config.update()
            await ctx.send(embed=embed)

    async def _config_options(self, ctx):
        """List valid public configuration keys."""
        embeds = []
        for names in zip_longest(*(iter(sorted(self.bot.config.public_keys)),) * 15):
            description = "\n".join(f"`{name}`" for name in takewhile(lambda x: x is not None, names))
            embed = discord.Embed(
                title="Available configuration keys:",
                color=self.bot.main_color,
                description=description,
            )
            embeds.append(embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    async def _config_set(self, ctx, key: str, value: str):
        """Set a configuration variable and its value."""
        key = key.lower()
        keys = self.bot.config.public_keys

        if key in keys:
            try:
                await self.bot.config.set(key, value)
                await self.bot.config.update()
                embed = discord.Embed(
                    title="Success",
                    color=self.bot.main_color,
                    description=f"Set `{key}` to `{self.bot.config[key]}`.",
                )
                if key == "snooze_behavior":
                    behavior = (
                        str(self.bot.config.get("snooze_behavior", convert=False)).strip().lower().strip('"')
                    )
                    if behavior == "move":
                        cat_id = self.bot.config.get("snoozed_category_id", convert=False)
                        valid = False
                        if cat_id:
                            try:
                                cat_obj = self.bot.modmail_guild.get_channel(int(str(cat_id)))
                                valid = isinstance(cat_obj, discord.CategoryChannel)
                            except Exception:
                                valid = False
                        if not valid:
                            example = f"`/config` with action `set`, key `snoozed_category_id`"
                            embed.add_field(
                                name="Action required",
                                value=(
                                    "You set `snooze_behavior` to `move`. Please set `snoozed_category_id` "
                                    "to the category where snoozed threads should be moved.\n"
                                    f"For example: {example}"
                                ),
                                inline=False,
                            )
            except InvalidConfigError as exc:
                embed = exc.embed
        else:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"{key} is an invalid key.",
            )
            valid_keys = [f"`{k}`" for k in sorted(keys)]
            embed.add_field(name="Valid keys", value=truncate(", ".join(valid_keys), 1024))

        return await ctx.send(embed=embed)

    async def _config_remove(self, ctx, key: str):
        """Delete a set configuration variable."""
        key = key.lower()
        keys = self.bot.config.public_keys
        if key in keys:
            self.bot.config.remove(key)
            await self.bot.config.update()
            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description=f"`{key}` had been reset to default.",
            )
        else:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"{key} is an invalid key.",
            )
            valid_keys = [f"`{k}`" for k in sorted(keys)]
            embed.add_field(name="Valid keys", value=", ".join(valid_keys))

        return await ctx.send(embed=embed)

    async def _config_get(self, ctx, key: str = None):
        """Show configuration variables that are currently set."""
        if key:
            key = key.lower()
        keys = self.bot.config.public_keys

        if key:
            if key in keys:
                desc = f"`{key}` is set to `{self.bot.config[key]}`"
                embed = discord.Embed(color=self.bot.main_color, description=desc)
                embed.set_author(
                    name="Config variable",
                    icon_url=self.bot.user.display_avatar.url if self.bot.user.display_avatar else None,
                )

            else:
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description=f"`{key}` is an invalid key.",
                )
                embed.set_footer(text='Use `/config` with action `options` for a list of config variables.')

        else:
            base_desc = "Here is a list of currently set configuration variable(s)."
            author_name = "Current config(s):"
            icon = self.bot.user.display_avatar.url if self.bot.user.display_avatar else None

            config = self.bot.config.filter_default(self.bot.config)
            items = [(name, value) for name, value in config.items() if name in self.bot.config.public_keys]

            embeds: list[discord.Embed] = []
            chunk: list[tuple[str, typing.Any]] = []
            for pair in items:
                chunk.append(pair)
                if len(chunk) == 15:
                    e = discord.Embed(color=self.bot.main_color, description=base_desc)
                    e.set_author(name=author_name, icon_url=icon)
                    for name, value in chunk:
                        e.add_field(name=name, value=f"`{value}`", inline=False)
                    embeds.append(e)
                    chunk = []

            if chunk:
                e = discord.Embed(color=self.bot.main_color, description=base_desc)
                e.set_author(name=author_name, icon_url=icon)
                for name, value in chunk:
                    e.add_field(name=name, value=f"`{value}`", inline=False)
                embeds.append(e)

        if key:
            return await ctx.send(embed=embed)
        if not embeds:
            return await ctx.send("No public configuration keys are set.")
        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()

    async def _config_help(self, ctx, key: str = None):
        """Show information on a specified configuration key."""
        if key is not None:
            key = key.lower()
        if key is not None and not (
            key in self.bot.config.public_keys or key in self.bot.config.protected_keys
        ):
            closest = get_close_matches(
                key, {**self.bot.config.public_keys, **self.bot.config.protected_keys}
            )
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"`{key}` is an invalid key.",
            )
            if closest:
                embed.add_field(
                    name="Perhaps you meant:",
                    value="\n".join(f"`{x}`" for x in closest),
                )
            return await ctx.send(embed=embed)

        config_help = self.bot.config.config_help

        if key is not None and key not in config_help:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"No help details found for `{key}`.",
            )
            return await ctx.send(embed=embed)

        def fmt(val):
            return UnseenFormatter().format(val, prefix=self.bot.prefix, bot=self.bot)

        index = 0
        embeds = []
        for i, (current_key, info) in enumerate(config_help.items()):
            if current_key == key:
                index = i
            embed = discord.Embed(title=f"{current_key}", color=self.bot.main_color)
            embed.add_field(name="Default:", value=fmt(info["default"]), inline=False)
            embed.add_field(name="Information:", value=fmt(info["description"]), inline=False)
            if info["examples"]:
                example_text = ""
                for example in info["examples"]:
                    example_text += f"- {fmt(example)}\n"
                embed.add_field(name="Example(s):", value=example_text, inline=False)

            note_text = ""
            for note in info.get("notes", []):
                note_text += f"- {fmt(note)}\n"
            if note_text:
                embed.add_field(name="Note(s):", value=note_text, inline=False)

            if info.get("image") is not None:
                embed.set_image(url=fmt(info["image"]))

            if info.get("thumbnail") is not None:
                embed.set_thumbnail(url=fmt(info["thumbnail"]))
            embeds += [embed]

        paginator = EmbedPaginatorSession(ctx, *embeds)
        paginator.current = index
        await paginator.run()

    @app_commands.command(name="config", description="View and change bot configuration variables.")
    @app_commands.describe(
        action="Config action to perform",
        key="Configuration key",
        value="Configuration value for set",
    )
    @app_commands.autocomplete(action=config_action_autocomplete, key=config_key_autocomplete)
    @checks.slash_has_permissions(PermissionLevel.OWNER)
    async def config_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        key: str = "",
        value: str = "",
    ):
        """Dispatch merged slash config actions."""
        ctx = await checks.InteractionContext.from_interaction(interaction)
        if await slash_dispatch.reject_action(interaction, action, CONFIG_ACTIONS, command_name="config"):
            return

        await ctx.defer()

        if action == "help":
            resolved_key = key.lower() if key else None
            return await self._config_help(ctx, resolved_key)
        if action == "options":
            return await self._config_options(ctx)
        if action == "get":
            resolved_key = key.lower() if key else None
            return await self._config_get(ctx, resolved_key)
        if action == "set":
            if await slash_dispatch.reject_missing(interaction, key, "key", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, value, "value", for_action=action):
                return
            return await self._config_set(ctx, key, value)
        if action == "remove":
            if await slash_dispatch.reject_missing(interaction, key, "key", for_action=action):
                return
            return await self._config_remove(ctx, key)

    async def _alias_view(self, ctx, name: str = None):
        """View a single alias or list all aliases."""
        if name is not None:
            name = name.lower()
            val = self.bot.aliases.get(name)
            if val is None:
                embed = utils.create_not_found_embed(name, self.bot.aliases.keys(), "Alias")
                return await ctx.send(embed=embed)

            values = utils.parse_alias(val)

            if not values:
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description=f"Alias `{name}` is invalid, this alias will now be deleted."
                    "This alias will now be deleted.",
                )
                embed.add_field(name=f"{name}` used to be:", value=utils.truncate(val, 1024))
                self.bot.aliases.pop(name)
                await self.bot.config.update()
                return await ctx.send(embed=embed)

            if len(values) == 1:
                embed = discord.Embed(
                    title=f'Alias - "{name}":',
                    description=values[0],
                    color=self.bot.main_color,
                )
                return await ctx.send(embed=embed)

            embeds = []
            for i, val in enumerate(values, start=1):
                embed = discord.Embed(
                    color=self.bot.main_color,
                    title=f'Alias - "{name}" - Step {i}:',
                    description=val,
                )
                embeds += [embed]
            session = EmbedPaginatorSession(ctx, *embeds)
            return await session.run()

        if not self.bot.aliases:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="You dont have any aliases at the moment.",
            )
            embed.set_footer(text='Use `/alias` with action `add` for more commands.')
            embed.set_author(
                name="Aliases",
                icon_url=self.bot.get_guild_icon(guild=ctx.guild, size=128),
            )
            return await ctx.send(embed=embed)

        embeds = []

        for i, names in enumerate(zip_longest(*(iter(sorted(self.bot.aliases)),) * 15)):
            description = utils.format_description(i, names)
            embed = discord.Embed(color=self.bot.main_color, description=description)
            embed.set_author(
                name="Command Aliases",
                icon_url=self.bot.get_guild_icon(guild=ctx.guild, size=128),
            )
            embeds.append(embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    async def _alias_raw(self, ctx, name: str):
        """View the raw content of an alias."""
        name = name.lower()
        val = self.bot.aliases.get(name)
        if val is None:
            embed = utils.create_not_found_embed(name, self.bot.aliases.keys(), "Alias")
            return await ctx.send(embed=embed)

        val = utils.truncate(utils.escape_code_block(val), 2048 - 7)
        embed = discord.Embed(
            title=f'Raw alias - "{name}":',
            description=f"```\n{val}```",
            color=self.bot.main_color,
        )

        return await ctx.send(embed=embed)

    async def make_alias(self, name, value, action):
        values = utils.parse_alias(value)
        if not values:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description="Invalid multi-step alias, try wrapping each steps in quotes.",
            )
            embed.set_footer(text=f'See "{self.bot.prefix}alias add" for more details.')
            return embed

        if len(values) > 25:
            embed = discord.Embed(
                title="Error",
                description="Too many steps, max=25.",
                color=self.bot.error_color,
            )
            return embed

        save_aliases = []

        multiple_alias = len(values) > 1

        embed = discord.Embed(title=f"{action} alias", color=self.bot.main_color)

        if not multiple_alias:
            embed.add_field(name=f"`{name}` points to:", value=utils.truncate(values[0], 1024))
        else:
            embed.description = f"`{name}` now points to the following steps:"

        for i, val in enumerate(values, start=1):
            view = StringView(val)
            linked_command = view.get_word().lower()
            message = view.read_rest()

            is_snippet = val in self.bot.snippets

            if not self.bot.get_command(linked_command) and not is_snippet:
                alias_command = self.bot.aliases.get(linked_command)
                if alias_command is not None:
                    save_aliases.extend(utils.normalize_alias(alias_command, message))
                else:
                    embed = discord.Embed(title="Error", color=self.bot.error_color)

                    if multiple_alias:
                        embed.description = (
                            "The command you are attempting to point "
                            f"to does not exist: `{linked_command}`."
                        )
                    else:
                        embed.description = (
                            "The command you are attempting to point "
                            f"to on step {i} does not exist: `{linked_command}`."
                        )

                    return embed
            else:
                save_aliases.append(val)
            if multiple_alias:
                embed.add_field(name=f"Step {i}:", value=utils.truncate(val, 1024))

        self.bot.aliases[name] = " && ".join(f'"{a}"' for a in save_aliases)
        await self.bot.config.update()
        return embed

    async def _alias_add(self, ctx, name: str, value: str):
        """Add a new alias."""
        name = name.lower()
        embed = None
        if self.bot.get_command(name):
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"A command with the same name already exists: `{name}`.",
            )

        elif name in self.bot.aliases:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"Another alias with the same name already exists: `{name}`.",
            )

        elif name in self.bot.snippets:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"A snippet with the same name already exists: `{name}`.",
            )

        elif len(name) > 120:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description="Alias names cannot be longer than 120 characters.",
            )

        if embed is None:
            embed = await self.make_alias(name, value, "Added")
        return await ctx.send(embed=embed)

    async def _alias_remove(self, ctx, name: str):
        """Remove an alias."""
        name = name.lower()
        if name in self.bot.aliases:
            self.bot.aliases.pop(name)
            await self.bot.config.update()

            embed = discord.Embed(
                title="Removed alias",
                color=self.bot.main_color,
                description=f"Successfully deleted `{name}`.",
            )
        else:
            embed = utils.create_not_found_embed(name, self.bot.aliases.keys(), "Alias")

        return await ctx.send(embed=embed)

    async def _alias_edit(self, ctx, name: str, value: str):
        """Edit an existing alias."""
        name = name.lower()
        if name not in self.bot.aliases:
            embed = utils.create_not_found_embed(name, self.bot.aliases.keys(), "Alias")
            return await ctx.send(embed=embed)

        embed = await self.make_alias(name, value, "Edited")
        return await ctx.send(embed=embed)

    @app_commands.command(name="alias", description="Create and manage command aliases.")
    @app_commands.describe(
        action="Alias action to perform",
        name="Alias name",
        value="Command or snippet target for add or edit",
    )
    @app_commands.autocomplete(action=alias_action_autocomplete, name=alias_autocomplete)
    @checks.slash_has_permissions(PermissionLevel.MODERATOR)
    async def alias_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        name: str = "",
        value: str = "",
    ):
        """Dispatch merged slash alias actions."""
        ctx = await checks.InteractionContext.from_interaction(interaction)
        if await slash_dispatch.reject_action(interaction, action, ALIAS_ACTIONS, command_name="alias"):
            return

        await ctx.defer()

        if action == "view":
            resolved_name = name.lower() if name else None
            return await self._alias_view(ctx, resolved_name)
        if action == "raw":
            if await slash_dispatch.reject_missing(interaction, name, "name", for_action=action):
                return
            return await self._alias_raw(ctx, name)
        if action == "add":
            if await slash_dispatch.reject_missing(interaction, name, "name", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, value, "value", for_action=action):
                return
            return await self._alias_add(ctx, name, value)
        if action == "remove":
            if await slash_dispatch.reject_missing(interaction, name, "name", for_action=action):
                return
            return await self._alias_remove(ctx, name)
        if action == "edit":
            if await slash_dispatch.reject_missing(interaction, name, "name", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, value, "value", for_action=action):
                return
            return await self._alias_edit(ctx, name, value)

    @staticmethod
    def _verify_user_or_role(user_or_role):
        if isinstance(user_or_role, discord.Role):
            if user_or_role.is_default():
                return -1
        elif user_or_role in {"everyone", "all"}:
            return -1
        if hasattr(user_or_role, "id"):
            return user_or_role.id
        raise commands.BadArgument(f'User or Role "{user_or_role}" not found')

    @staticmethod
    def _parse_level(name):
        name = name.upper()
        try:
            return PermissionLevel[name]
        except KeyError:
            pass
        transform = {
            "1": PermissionLevel.REGULAR,
            "2": PermissionLevel.SUPPORTER,
            "3": PermissionLevel.MODERATOR,
            "4": PermissionLevel.ADMINISTRATOR,
            "5": PermissionLevel.OWNER,
        }
        return transform.get(name, PermissionLevel.INVALID)

    async def _permissions_help(self, ctx):
        """Show permissions command overview."""
        embed = discord.Embed(
            title="Permissions",
            color=self.bot.main_color,
            description=(
                "Set permissions for Modmail commands by command name or permission level.\n\n"
                "Levels: **Owner** [5], **Administrator** [4], **Moderator** [3], "
                "**Supporter** [2], **Regular** [1].\n\n"
                "Use `/permissions` with actions `add`, `remove`, `override`, or `get`."
            ),
        )
        await ctx.send(embed=embed)

    async def _permissions_override(self, ctx, command_name: str, level_name: str):
        """Change a permission level for a specific command."""
        command_name = command_name.lower()
        command = self.bot.get_command(command_name)
        if command is None:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"The referenced command does not exist: `{command_name}`.",
            )
            return await ctx.send(embed=embed)

        level = self._parse_level(level_name)
        if level is PermissionLevel.INVALID:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"The referenced level does not exist: `{level_name}`.",
            )
        else:
            logger.info(
                "Updated command permission level for `%s` to `%s`.",
                command.qualified_name,
                level.name,
            )
            self.bot.config["override_command_level"][command.qualified_name] = level.name

            await self.bot.config.update()
            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description="Successfully set command permission level for "
                f"`{command.qualified_name}` to `{level.name}`.",
            )
        return await ctx.send(embed=embed)

    async def _permissions_add(self, ctx, target_type: str, name: str, user_or_role):
        """Add a permission to a command or permission level."""
        if isinstance(user_or_role, str):
            user_or_role = await self._resolve_member_role(ctx, user_or_role)

        target_type = target_type.lower()
        if target_type not in {"command", "level"}:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description="`target_type` must be `command` or `level` for add.",
            )
            return await ctx.send(embed=embed)

        command = level = None
        if target_type == "command":
            name = name.lower()
            command = self.bot.get_command(name)
            check = command is not None
        else:
            level = self._parse_level(name)
            check = level is not PermissionLevel.INVALID

        if not check:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"The referenced {target_type} does not exist: `{name}`.",
            )
            return await ctx.send(embed=embed)

        try:
            value = self._verify_user_or_role(user_or_role)
        except commands.BadArgument as exc:
            embed = discord.Embed(title="Error", color=self.bot.error_color, description=str(exc))
            return await ctx.send(embed=embed)

        if target_type == "command":
            name = command.qualified_name
            await self.bot.update_perms(name, value)
        else:
            await self.bot.update_perms(level, value)
            name = level.name
            if level > PermissionLevel.REGULAR:
                if value == -1:
                    key = self.bot.modmail_guild.default_role
                elif isinstance(user_or_role, discord.Role):
                    key = user_or_role
                else:
                    key = self.bot.modmail_guild.get_member(value)
                if key is not None:
                    logger.info("Granting %s access to Modmail category.", key.name)
                    try:
                        await self.bot.main_category.set_permissions(key, read_messages=True)
                    except discord.Forbidden:
                        warn = discord.Embed(
                            title="Missing Permissions",
                            color=self.bot.error_color,
                            description=(
                                "I couldn't update the Modmail category permissions. "
                                "Please grant me 'Manage Channels' and 'Manage Roles' for this category."
                            ),
                        )
                        await ctx.send(embed=warn)

        embed = discord.Embed(
            title="Success",
            color=self.bot.main_color,
            description=f"Permission for `{name}` was successfully updated.",
        )
        return await ctx.send(embed=embed)

    async def _permissions_remove(self, ctx, target_type: str, name: str, user_or_role=None):
        """Remove command, level, or override permissions."""
        target_type = target_type.lower()
        if user_or_role is not None and isinstance(user_or_role, str):
            user_or_role = await self._resolve_member_role(ctx, user_or_role)

        if target_type not in {"command", "level", "override"} or (
            target_type != "override" and user_or_role is None
        ):
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description="Provide `target_type`, `name`, and `target` for command/level remove.",
            )
            return await ctx.send(embed=embed)

        if target_type == "override":
            name = name.lower()
            name = getattr(self.bot.get_command(name), "qualified_name", name)
            level = self.bot.config["override_command_level"].get(name)
            if level is None:
                perm = self.bot.command_perm(name)
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description=f"The command permission level was never overridden: `{name}`, "
                    f"current permission level is {perm.name}.",
                )
            else:
                logger.info("Restored command permission level for `%s`.", name)
                self.bot.config["override_command_level"].pop(name)
                await self.bot.config.update()
                perm = self.bot.command_perm(name)
                embed = discord.Embed(
                    title="Success",
                    color=self.bot.main_color,
                    description=f"Command permission level for `{name}` was successfully restored to {perm.name}.",
                )
            return await ctx.send(embed=embed)

        level = None
        if target_type == "command":
            name = name.lower()
            name = getattr(self.bot.get_command(name), "qualified_name", name)
        else:
            level = self._parse_level(name)
            if level is PermissionLevel.INVALID:
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description=f"The referenced level does not exist: `{name}`.",
                )
                return await ctx.send(embed=embed)
            name = level.name

        try:
            value = self._verify_user_or_role(user_or_role)
        except commands.BadArgument as exc:
            embed = discord.Embed(title="Error", color=self.bot.error_color, description=str(exc))
            return await ctx.send(embed=embed)
        await self.bot.update_perms(level or name, value, add=False)

        if target_type == "level":
            if level > PermissionLevel.REGULAR:
                if value == -1:
                    logger.info("Denying @everyone access to Modmail category.")
                    try:
                        await self.bot.main_category.set_permissions(
                            self.bot.modmail_guild.default_role, read_messages=False
                        )
                    except discord.Forbidden:
                        warn = discord.Embed(
                            title="Missing Permissions",
                            color=self.bot.error_color,
                            description=(
                                "I couldn't update the Modmail category permissions. "
                                "Please grant me 'Manage Channels' and 'Manage Roles' for this category."
                            ),
                        )
                        await ctx.send(embed=warn)
                elif isinstance(user_or_role, discord.Role):
                    logger.info("Denying %s access to Modmail category.", user_or_role.name)
                    try:
                        await self.bot.main_category.set_permissions(user_or_role, overwrite=None)
                    except discord.Forbidden:
                        warn = discord.Embed(
                            title="Missing Permissions",
                            color=self.bot.error_color,
                            description=(
                                "I couldn't update the Modmail category permissions. "
                                "Please grant me 'Manage Channels' and 'Manage Roles' for this category."
                            ),
                        )
                        await ctx.send(embed=warn)
                else:
                    member = self.bot.modmail_guild.get_member(value)
                    if member is not None and member != self.bot.modmail_guild.me:
                        logger.info("Denying %s access to Modmail category.", member.name)
                        try:
                            await self.bot.main_category.set_permissions(member, overwrite=None)
                        except discord.Forbidden:
                            warn = discord.Embed(
                                title="Missing Permissions",
                                color=self.bot.error_color,
                                description=(
                                    "I couldn't update the Modmail category permissions. "
                                    "Please grant me 'Manage Channels' and 'Manage Roles' for this category."
                                ),
                            )
                            await ctx.send(embed=warn)

        embed = discord.Embed(
            title="Success",
            color=self.bot.main_color,
            description=f"Permission for `{name}` was successfully updated.",
        )
        return await ctx.send(embed=embed)

    def _get_perm(self, ctx, name, type_):
        if type_ == "command":
            permissions = self.bot.config["command_permissions"].get(name, [])
        else:
            permissions = self.bot.config["level_permissions"].get(name, [])
        if not permissions:
            embed = discord.Embed(
                title=f"Permission entries for {type_} `{name}`:",
                description="No permission entries found.",
                color=self.bot.main_color,
            )
        else:
            values = []
            for perm in permissions:
                if perm == -1:
                    values.insert(0, "**everyone**")
                    continue
                member = ctx.guild.get_member(int(perm))
                if member is not None:
                    values.append(member.mention)
                    continue
                user = self.bot.get_user(int(perm))
                if user is not None:
                    values.append(user.mention)
                    continue
                role = ctx.guild.get_role(int(perm))
                if role is not None:
                    values.append(role.mention)
                else:
                    values.append(str(perm))

            embed = discord.Embed(
                title=f"Permission entries for {type_} `{name}`:",
                description=", ".join(values),
                color=self.bot.main_color,
            )
        return embed

    async def _permissions_get(self, ctx, user_or_role, name: str = None):
        """View currently-set permissions for a user, command, level, or override."""
        if user_or_role not in {"command", "level", "override"}:
            user_or_role = await self._resolve_member_role(ctx, user_or_role)

        if name is None and user_or_role not in {"command", "level", "override"}:
            try:
                value = str(self._verify_user_or_role(user_or_role))
            except commands.BadArgument as exc:
                embed = discord.Embed(title="Error", color=self.bot.error_color, description=str(exc))
                return await ctx.send(embed=embed)

            cmds = []
            levels = []

            done = set()
            command_permissions = self.bot.config["command_permissions"]
            level_permissions = self.bot.config["level_permissions"]
            for command in self.bot.walk_commands():
                if command not in done:
                    done.add(command)
                    permissions = command_permissions.get(command.qualified_name, [])
                    if value in permissions:
                        cmds.append(command.qualified_name)

            for level in PermissionLevel:
                permissions = level_permissions.get(level.name, [])
                if value in permissions:
                    levels.append(level.name)

            mention = getattr(user_or_role, "name", getattr(user_or_role, "id", user_or_role))
            desc_cmd = ", ".join(map(lambda x: f"`{x}`", cmds)) if cmds else "No permission entries found."
            desc_level = (
                ", ".join(map(lambda x: f"`{x}`", levels)) if levels else "No permission entries found."
            )

            embeds = [
                discord.Embed(
                    title=f"{mention} has permission with the following commands:",
                    description=desc_cmd,
                    color=self.bot.main_color,
                ),
                discord.Embed(
                    title=f"{mention} has permission with the following permission levels:",
                    description=desc_level,
                    color=self.bot.main_color,
                ),
            ]
        else:
            user_or_role = (user_or_role or "").lower()
            if user_or_role == "override":
                if name is None:
                    done = set()

                    overrides = {}
                    for command in self.bot.walk_commands():
                        if command not in done:
                            done.add(command)
                            level = self.bot.config["override_command_level"].get(command.qualified_name)
                            if level is not None:
                                overrides[command.qualified_name] = level

                    embeds = []
                    if not overrides:
                        embeds.append(
                            discord.Embed(
                                title="Permission Overrides",
                                description="You don't have any command level overrides at the moment.",
                                color=self.bot.error_color,
                            )
                        )
                    else:
                        for items in zip_longest(*(iter(sorted(overrides.items())),) * 15):
                            description = "\n".join(
                                ": ".join((f"`{cmd_name}`", level))
                                for cmd_name, level in takewhile(lambda x: x is not None, items)
                            )
                            embed = discord.Embed(color=self.bot.main_color, description=description)
                            embed.set_author(
                                name="Permission Overrides",
                                icon_url=self.bot.get_guild_icon(guild=ctx.guild, size=128),
                            )
                            embeds.append(embed)

                    session = EmbedPaginatorSession(ctx, *embeds)
                    return await session.run()

                name = name.lower()
                name = getattr(self.bot.get_command(name), "qualified_name", name)
                level = self.bot.config["override_command_level"].get(name)
                perm = self.bot.command_perm(name)
                if level is None:
                    embed = discord.Embed(
                        title="Error",
                        color=self.bot.error_color,
                        description=f"The command permission level was never overridden: `{name}`, "
                        f"current permission level is {perm.name}.",
                    )
                else:
                    embed = discord.Embed(
                        title="Success",
                        color=self.bot.main_color,
                        description=f'Permission override for command "{name}" is "{perm.name}".',
                    )

                return await ctx.send(embed=embed)

            if user_or_role not in {"command", "level"}:
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description="Provide a user/role in `target`, or use `target_type` command/level/override.",
                )
                return await ctx.send(embed=embed)
            embeds = []
            if name is not None:
                name = name.strip('"')
                command = level = None
                if user_or_role == "command":
                    name = name.lower()
                    command = self.bot.get_command(name)
                    check = command is not None
                else:
                    level = self._parse_level(name)
                    check = level is not PermissionLevel.INVALID

                if not check:
                    embed = discord.Embed(
                        title="Error",
                        color=self.bot.error_color,
                        description=f"The referenced {user_or_role} does not exist: `{name}`.",
                    )
                    return await ctx.send(embed=embed)

                if user_or_role == "command":
                    embeds.append(self._get_perm(ctx, command.qualified_name, "command"))
                else:
                    embeds.append(self._get_perm(ctx, level.name, "level"))
            else:
                if user_or_role == "command":
                    done = set()
                    for command in self.bot.walk_commands():
                        if command not in done:
                            done.add(command)
                            embeds.append(self._get_perm(ctx, command.qualified_name, "command"))
                else:
                    for perm_level in PermissionLevel:
                        embeds.append(self._get_perm(ctx, perm_level.name, "level"))

        session = EmbedPaginatorSession(ctx, *embeds)
        return await session.run()

    @app_commands.command(name="permissions", description="Manage Modmail command and level permissions.")
    @app_commands.describe(
        action="Permissions action to perform",
        target_type="Whether to target a command, level, or override",
        name="Command name or permission level",
        target="User or role for add/remove/get",
        command_name="Command for override",
        level_name="Permission level for override",
    )
    @app_commands.autocomplete(
        action=permissions_action_autocomplete,
        target_type=permission_type_autocomplete,
        name=permissions_name_autocomplete,
        target=member_role_autocomplete,
        command_name=command_name_autocomplete,
        level_name=permission_level_autocomplete,
    )
    @checks.slash_has_permissions(PermissionLevel.OWNER)
    async def permissions_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        target_type: str = "",
        name: str = "",
        target: str = "",
        command_name: str = "",
        level_name: str = "",
    ):
        """Dispatch merged slash permissions actions."""
        ctx = await checks.InteractionContext.from_interaction(interaction)
        if await slash_dispatch.reject_action(
            interaction, action, PERMISSIONS_ACTIONS, command_name="permissions"
        ):
            return

        await ctx.defer()

        if action == "help":
            return await self._permissions_help(ctx)
        if action == "override":
            if await slash_dispatch.reject_missing(interaction, command_name, "command_name", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, level_name, "level_name", for_action=action):
                return
            return await self._permissions_override(ctx, command_name, level_name)
        if action == "add":
            if await slash_dispatch.reject_missing(interaction, target_type, "target_type", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, name, "name", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, target, "target", for_action=action):
                return
            return await self._permissions_add(ctx, target_type, name, target)
        if action == "remove":
            if await slash_dispatch.reject_missing(interaction, target_type, "target_type", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, name, "name", for_action=action):
                return
            if target_type != "override":
                if await slash_dispatch.reject_missing(interaction, target, "target", for_action=action):
                    return
            return await self._permissions_remove(
                ctx,
                target_type,
                name,
                target if target else None,
            )
        if action == "get":
            if target_type in {"command", "level", "override"}:
                resolved_name = name if name else None
                return await self._permissions_get(ctx, target_type, resolved_name)
            if target:
                return await self._permissions_get(ctx, target, name if name else None)
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description="Provide `target` (user/role) or `target_type` (command/level/override) for get.",
            )
            return await ctx.send(embed=embed)

    async def _oauth_help(self, ctx):
        """Show oauth command overview."""
        embed = discord.Embed(
            title="OAuth",
            color=self.bot.main_color,
            description=(
                "Commands relating to logviewer oauth2 login authentication.\n\n"
                "Use `/oauth` with actions `whitelist` or `show`."
            ),
        )
        await ctx.send(embed=embed)

    async def _oauth_whitelist(self, ctx, target):
        """Whitelist or un-whitelist a user or role for log access."""
        if isinstance(target, str):
            target = await self._resolve_member_role(ctx, target)
        if isinstance(target, str):
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f'User or Role "{target}" not found.',
            )
            return await ctx.send(embed=embed)

        whitelisted = self.bot.config["oauth_whitelist"]

        if target.id in whitelisted:
            whitelisted.remove(target.id)
            removed = True
        else:
            whitelisted.append(target.id)
            removed = False

        await self.bot.config.update()

        embed = discord.Embed(color=self.bot.main_color)
        embed.title = "Success"

        if not hasattr(target, "mention"):
            target = self.bot.get_user(target.id) or self.bot.modmail_guild.get_role(target.id)

        embed.description = f"{'Un-w' if removed else 'W'}hitelisted {target.mention} to view logs."

        await ctx.send(embed=embed)

    async def _oauth_show(self, ctx):
        """Show users and roles whitelisted for log access."""
        whitelisted = self.bot.config["oauth_whitelist"]

        users = []
        roles = []

        for id_ in whitelisted:
            user = self.bot.get_user(id_)
            if user:
                users.append(user)
            role = self.bot.modmail_guild.get_role(id_)
            if role:
                roles.append(role)

        embed = discord.Embed(color=self.bot.main_color)
        embed.title = "Oauth Whitelist"

        embed.add_field(name="Users", value=" ".join(u.mention for u in users) or "None")
        embed.add_field(name="Roles", value=" ".join(r.mention for r in roles) or "None")

        await ctx.send(embed=embed)

    @app_commands.command(name="oauth", description="Manage logviewer OAuth whitelist.")
    @app_commands.describe(
        action="OAuth action to perform",
        target="User or role to whitelist or un-whitelist",
    )
    @app_commands.autocomplete(action=oauth_action_autocomplete, target=member_role_autocomplete)
    @checks.slash_has_permissions(PermissionLevel.OWNER)
    async def oauth_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        target: str = "",
    ):
        """Dispatch merged slash oauth actions."""
        ctx = await checks.InteractionContext.from_interaction(interaction)
        if await slash_dispatch.reject_action(interaction, action, OAUTH_ACTIONS, command_name="oauth"):
            return

        await ctx.defer()

        if action == "help":
            return await self._oauth_help(ctx)
        if action == "whitelist":
            if await slash_dispatch.reject_missing(interaction, target, "target", for_action=action):
                return
            return await self._oauth_whitelist(ctx, target)
        if action == "show":
            return await self._oauth_show(ctx)

    async def _autotrigger_add(self, ctx, keyword: str, command: str):
        """Add an autotrigger keyword."""
        if keyword in self.bot.auto_triggers:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"Another autotrigger with the same name already exists: `{keyword}`.",
            )
        else:
            valid = False
            split_cmd = command.split(" ")
            for n in range(1, len(split_cmd) + 1):
                if self.bot.get_command(" ".join(split_cmd[0:n])):
                    valid = True
                    break

            if not valid and self.bot.aliases:
                for n in range(1, len(split_cmd) + 1):
                    if self.bot.aliases.get(" ".join(split_cmd[0:n])):
                        valid = True
                        break

            if valid:
                self.bot.auto_triggers[keyword] = command
                await self.bot.config.update()

                embed = discord.Embed(
                    title="Success",
                    color=self.bot.main_color,
                    description=f"Keyword `{keyword}` has been linked to `{command}`.",
                )
            else:
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description="Invalid command. Please provide a valid command or alias.",
                )

        await ctx.send(embed=embed)

    async def _autotrigger_edit(self, ctx, keyword: str, command: str):
        """Edit an existing autotrigger keyword."""
        if keyword not in self.bot.auto_triggers:
            embed = utils.create_not_found_embed(keyword, self.bot.auto_triggers.keys(), "Autotrigger")
        else:
            valid = False
            split_cmd = command.split(" ")
            for n in range(1, len(split_cmd) + 1):
                if self.bot.get_command(" ".join(split_cmd[0:n])):
                    valid = True
                    break

            if not valid and self.bot.aliases:
                for n in range(1, len(split_cmd) + 1):
                    if self.bot.aliases.get(" ".join(split_cmd[0:n])):
                        valid = True
                        break

            if valid:
                self.bot.auto_triggers[keyword] = command
                await self.bot.config.update()

                embed = discord.Embed(
                    title="Success",
                    color=self.bot.main_color,
                    description=f"Keyword `{keyword}` has been linked to `{command}`.",
                )
            else:
                embed = discord.Embed(
                    title="Error",
                    color=self.bot.error_color,
                    description="Invalid command. Please provide a valid command or alias.",
                )

        await ctx.send(embed=embed)

    async def _autotrigger_remove(self, ctx, keyword: str):
        """Remove an autotrigger keyword."""
        try:
            del self.bot.auto_triggers[keyword]
        except KeyError:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"Keyword `{keyword}` could not be found.",
            )
            await ctx.send(embed=embed)
        else:
            await self.bot.config.update()

            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description=f"Keyword `{keyword}` has been removed.",
            )
            await ctx.send(embed=embed)

    async def _autotrigger_test(self, ctx, text: str):
        """Test a string against the current autotrigger setup."""
        for keyword in self.bot.auto_triggers:
            if self.bot.config.get("use_regex_autotrigger"):
                check = re.search(keyword, text)
                regex = True
            else:
                check = keyword.lower() in text.lower()
                regex = False

            if check:
                alias = self.bot.auto_triggers[keyword]
                embed = discord.Embed(
                    title=f"{'Regex ' if regex else ''}Keyword Found",
                    color=self.bot.main_color,
                    description=f"autotrigger keyword `{keyword}` found. Command executed: `{alias}`",
                )
                return await ctx.send(embed=embed)

        embed = discord.Embed(
            title="Keyword Not Found",
            color=self.bot.error_color,
            description="No autotrigger keyword found.",
        )
        return await ctx.send(embed=embed)

    async def _autotrigger_list(self, ctx):
        """List all configured autotriggers."""
        embeds = []
        for keyword in self.bot.auto_triggers:
            command = self.bot.auto_triggers[keyword]
            embed = discord.Embed(
                title=keyword,
                color=self.bot.main_color,
                description=command,
            )
            embeds.append(embed)

        if not embeds:
            embeds.append(
                discord.Embed(
                    title="No autotrigger set",
                    color=self.bot.error_color,
                    description="Use `/autotrigger` with action `add` to add new autotriggers.",
                )
            )

        await EmbedPaginatorSession(ctx, *embeds).run()

    @app_commands.command(name="autotrigger", description="Manage keyword-based command autotriggers.")
    @app_commands.describe(
        action="Autotrigger action to perform",
        keyword="Trigger keyword",
        command="Command or alias to run for add or edit",
        text="Text to test against autotriggers",
    )
    @app_commands.autocomplete(
        action=autotrigger_action_autocomplete,
        keyword=autotrigger_keyword_autocomplete,
    )
    @checks.slash_has_permissions(PermissionLevel.OWNER)
    async def autotrigger_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        keyword: str = "",
        command: str = "",
        text: str = "",
    ):
        """Dispatch merged slash autotrigger actions."""
        ctx = await checks.InteractionContext.from_interaction(interaction)
        if await slash_dispatch.reject_action(
            interaction, action, AUTOTRIGGER_ACTIONS, command_name="autotrigger"
        ):
            return

        await ctx.defer()

        if action == "list":
            return await self._autotrigger_list(ctx)
        if action == "add":
            if await slash_dispatch.reject_missing(interaction, keyword, "keyword", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, command, "command", for_action=action):
                return
            return await self._autotrigger_add(ctx, keyword, command)
        if action == "edit":
            if await slash_dispatch.reject_missing(interaction, keyword, "keyword", for_action=action):
                return
            if await slash_dispatch.reject_missing(interaction, command, "command", for_action=action):
                return
            return await self._autotrigger_edit(ctx, keyword, command)
        if action == "remove":
            if await slash_dispatch.reject_missing(interaction, keyword, "keyword", for_action=action):
                return
            return await self._autotrigger_remove(ctx, keyword)
        if action == "test":
            if await slash_dispatch.reject_missing(interaction, text, "text", for_action=action):
                return
            return await self._autotrigger_test(ctx, text)

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.OWNER)
    @checks.github_token_required()
    @trigger_typing
    async def github(self, ctx):
        """Shows the GitHub user your Github_Token is linked to."""
        data = await self.bot.api.get_user_info()

        if data:
            embed = discord.Embed(title="GitHub", description="Current User", color=self.bot.main_color)
            user = data["user"]
            embed.set_author(
                name=user["username"],
                icon_url=user["avatar_url"] if user["avatar_url"] else None,
                url=user["url"],
            )
            embed.set_thumbnail(url=user["avatar_url"] if user["avatar_url"] else None)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=discord.Embed(title="Invalid Github Token", color=self.bot.error_color))

    @commands.hybrid_command()
    @checks.has_permissions(PermissionLevel.OWNER)
    @checks.github_token_required(ignore_if_not_heroku=True)
    @checks.updates_enabled()
    @trigger_typing
    @app_commands.describe(flag='Use "force" to update even when already up to date')
    async def update(self, ctx, *, flag: str = ""):
        """
        Update Modmail.
        To stay up-to-date with the latest commit from GitHub, specify "force" as the flag.
        """

        changelog = await Changelog.from_url(self.bot)
        latest = changelog.latest_version

        desc = (
            f"The latest version is [`{self.bot.version}`]"
            "(https://github.com/modmail-dev/modmail/blob/master/bot.py#L1)"
        )

        if self.bot.version >= Version(latest.version) and flag.lower() != "force":
            embed = discord.Embed(title="Already up to date", description=desc, color=self.bot.main_color)

            data = await self.bot.api.get_user_info()
            if data:
                user = data["user"]
                embed.set_author(
                    name=user["username"],
                    icon_url=user["avatar_url"] if user["avatar_url"] else None,
                    url=user["url"],
                )
            await ctx.send(embed=embed)
        else:
            error = None
            data = {}
            try:
                # update fork if gh_token exists
                data = await self.bot.api.update_repository()
            except InvalidConfigError:
                pass
            except ClientResponseError as exc:
                error = exc

            if self.bot.hosting_method == HostingMethod.HEROKU:
                if error is not None:
                    embed = discord.Embed(
                        title="Update failed",
                        description=f"Error status: {error.status}.\nError message: {error.message}",
                        color=self.bot.error_color,
                    )
                    return await ctx.send(embed=embed)
                if not data:
                    # invalid gh_token
                    embed = discord.Embed(
                        title="Update failed",
                        description="Invalid Github token.",
                        color=self.bot.error_color,
                    )
                    return await ctx.send(embed=embed)

                commit_data = data["data"]
                user = data["user"]
                if commit_data and commit_data.get("html_url"):
                    embed = discord.Embed(color=self.bot.main_color)

                    embed.set_footer(text=f"Updating Modmail v{self.bot.version} -> v{latest.version}")

                    embed.set_author(
                        name=user["username"] + " - Updating bot",
                        icon_url=user["avatar_url"] if user["avatar_url"] else None,
                        url=user["url"],
                    )

                    embed.description = latest.description
                    for name, value in latest.fields.items():
                        embed.add_field(name=name, value=truncate(value, 200))

                    html_url = commit_data["html_url"]
                    short_sha = commit_data["sha"][:6]
                    embed.add_field(name="Merge Commit", value=f"[`{short_sha}`]({html_url})")
                else:
                    embed = discord.Embed(
                        title="Already up to date",
                        description="No further updates required.",
                        color=self.bot.main_color,
                    )
                    embed.set_footer(text="Force update")
                    embed.set_author(
                        name=user["username"],
                        icon_url=user["avatar_url"] if user["avatar_url"] else None,
                        url=user["url"],
                    )
                await ctx.send(embed=embed)
            else:
                command = "git pull"
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stderr=PIPE,
                    stdout=PIPE,
                )
                err = await proc.stderr.read()
                err = err.decode("utf-8").rstrip()
                res = await proc.stdout.read()
                res = res.decode("utf-8").rstrip()

                if err and not res:
                    embed = discord.Embed(
                        title="Update failed",
                        description=err,
                        color=self.bot.error_color,
                    )
                    await ctx.send(embed=embed)

                elif res != "Already up to date.":
                    logger.info("Bot has been updated.")

                    embed = discord.Embed(
                        title="Bot has been updated",
                        color=self.bot.main_color,
                    )
                    embed.set_footer(text=f"Updating Modmail v{self.bot.version} " f"-> v{latest.version}")
                    embed.description = latest.description
                    for name, value in latest.fields.items():
                        embed.add_field(name=name, value=truncate(value, 200))

                    if self.bot.hosting_method == HostingMethod.OTHER:
                        embed.description = (
                            "If you do not have an auto-restart setup, please manually start the bot.",
                        )

                    await ctx.send(embed=embed)
                    return await self.bot.close()
                else:
                    embed = discord.Embed(
                        title="Already up to date",
                        description=desc,
                        color=self.bot.main_color,
                    )
                    embed.set_footer(text="Force update")
                    await ctx.send(embed=embed)

    @commands.command(hidden=True, name="eval")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def eval_(self, ctx, *, body: str):
        """Evaluates Python code."""

        logger.warning("Running eval command:\n%s", body)

        env = {
            "ctx": ctx,
            "bot": self.bot,
            "channel": ctx.channel,
            "author": ctx.author,
            "guild": ctx.guild,
            "message": ctx.message,
            "source": inspect.getsource,
            "discord": __import__("discord"),
        }

        env.update(globals())

        body = utils.cleanup_code(body)
        stdout = StringIO()

        to_compile = f"async def func():\n{indent(body, '  ')}"

        def paginate(text: str):
            """Simple generator that paginates text."""
            last = 0
            pages = []
            appd_index = curr = None
            for curr in range(0, len(text)):
                if curr % 1980 == 0:
                    pages.append(text[last:curr])
                    last = curr
                    appd_index = curr
            if appd_index != len(text) - 1:
                pages.append(text[last:curr])
            return list(filter(lambda a: a != "", pages))

        try:
            exec(to_compile, env)  # pylint: disable=exec-used
        except Exception as exc:
            await ctx.send(f"```py\n{exc.__class__.__name__}: {exc}\n```")
            return await self.bot.add_reaction(ctx.message, "\u2049")

        func = env["func"]
        try:
            with redirect_stdout(stdout):
                ret = await func()
        except Exception:
            value = stdout.getvalue()
            await ctx.send(f"```py\n{value}{traceback.format_exc()}\n```")
            return await self.bot.add_reaction(ctx.message, "\u2049")

        else:
            value = stdout.getvalue()
            if ret is None:
                if value:
                    try:
                        await ctx.send(f"```py\n{value}\n```")
                    except Exception:
                        paginated_text = paginate(value)
                        for page in paginated_text:
                            if page == paginated_text[-1]:
                                await ctx.send(f"```py\n{page}\n```")
                                break
                            await ctx.send(f"```py\n{page}\n```")
            else:
                try:
                    await ctx.send(f"```py\n{value}{ret}\n```")
                except Exception:
                    paginated_text = paginate(f"{value}{ret}")
                    for page in paginated_text:
                        if page == paginated_text[-1]:
                            await ctx.send(f"```py\n{page}\n```")
                            break
                        await ctx.send(f"```py\n{page}\n```")

        await self.bot.add_reaction(ctx.message, "\u2705")


async def setup(bot):
    await bot.add_cog(Utility(bot))
