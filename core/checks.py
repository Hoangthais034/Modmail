import discord
from discord.ext import commands

from core.models import HostingMethod, PermissionLevel, getLogger

logger = getLogger(__name__)


class _SlashMessage:
    """Minimal message stand-in for slash handlers that call thread.reply(ctx.message, ...)."""

    __slots__ = ("author", "channel", "content", "attachments", "guild", "id", "created_at")

    def __init__(
        self,
        interaction: discord.Interaction,
        content: str = "",
        attachments: list | None = None,
    ):
        self.author = interaction.user
        self.channel = interaction.channel
        self.guild = interaction.guild
        self.content = content
        self.attachments = attachments or []
        self.id = interaction.id
        self.created_at = discord.utils.utcnow()

    async def delete(self, *, delay=None):
        return

    async def add_reaction(self, emoji):
        return


class InteractionContext:
    """Context adapter for slash interactions with send, defer, and thread support."""

    __slots__ = (
        "bot",
        "author",
        "channel",
        "guild",
        "interaction",
        "thread",
        "_message_content",
        "_attachments",
    )

    def __init__(
        self,
        interaction: discord.Interaction,
        *,
        content: str = "",
        attachments: list | None = None,
        thread=None,
    ):
        self.interaction = interaction
        self.bot = interaction.client
        self.author = interaction.user
        self.channel = interaction.channel
        self.guild = interaction.guild
        self.thread = thread
        self._message_content = content
        self._attachments = attachments or []

    @classmethod
    async def from_interaction(
        cls,
        interaction: discord.Interaction,
        *,
        content: str = "",
        attachments: list | None = None,
    ) -> "InteractionContext":
        """Build a slash context and resolve the Modmail thread from the channel when possible."""
        ctx = cls(interaction, content=content, attachments=attachments)
        if ctx.channel is not None:
            ctx.thread = await ctx.bot.threads.find(channel=ctx.channel)
        return ctx

    @property
    def message(self) -> _SlashMessage:
        return _SlashMessage(
            self.interaction,
            content=self._message_content,
            attachments=self._attachments,
        )

    def set_message_content(self, content: str) -> None:
        self._message_content = content

    def set_attachments(self, attachments: list) -> None:
        self._attachments = attachments

    def typing(self):
        if self.channel is not None:
            return self.channel.typing()
        return discord.utils.MISSING

    async def defer(self, *, ephemeral: bool = False) -> None:
        if not self.interaction.response.is_done():
            await self.interaction.response.defer(ephemeral=ephemeral)

    async def send(self, content=None, **kwargs):
        if not self.interaction.response.is_done():
            return await self.interaction.response.send_message(content, **kwargs)
        return await self.interaction.followup.send(content, **kwargs)

    async def reply(self, content=None, **kwargs):
        return await self.send(content, **kwargs)

    async def send_error(self, message: str, *, ephemeral: bool = True):
        embed = discord.Embed(color=self.bot.error_color, description=message)
        return await self.send(embed=embed, ephemeral=ephemeral)

    def wait_for(self, event: str, *, check=None, timeout=None):
        return self.bot.wait_for(event, check=check, timeout=timeout)


def has_permissions_predicate(
    permission_level: PermissionLevel = PermissionLevel.REGULAR,
):
    async def predicate(ctx):
        return await check_permissions(ctx, ctx.command.qualified_name)

    predicate.permission_level = permission_level
    return predicate


def has_permissions(permission_level: PermissionLevel = PermissionLevel.REGULAR):
    """
    A decorator that checks if the author has the required permissions.

    Parameters
    ----------

    permission_level : PermissionLevel
        The lowest level of permission needed to use this command.
        Defaults to REGULAR.

    Examples
    --------
    ::
        @has_permissions(PermissionLevel.OWNER)
        async def setup(ctx):
            await ctx.send('Success')
    """

    return commands.check(has_permissions_predicate(permission_level))


async def has_at_least_permission(ctx, permission_level: PermissionLevel) -> bool:
    """Check whether the author meets at least the given permission level."""
    if await ctx.bot.is_owner(ctx.author) or ctx.author.id == ctx.bot.user.id:
        return True

    if (
        permission_level is not PermissionLevel.OWNER
        and ctx.channel.permissions_for(ctx.author).administrator
        and ctx.guild == ctx.bot.modmail_guild
    ):
        return True

    level_permissions = ctx.bot.config["level_permissions"]
    checkables = {*ctx.author.roles, ctx.author}

    for level in PermissionLevel:
        if level >= permission_level and level.name in level_permissions:
            if -1 in level_permissions[level.name] or any(
                str(check.id) in level_permissions[level.name] for check in checkables
            ):
                return True
    return False


def slash_has_permissions(permission_level: PermissionLevel = PermissionLevel.REGULAR):
    """Attach a permission level to a slash command callback for command_perm lookup."""

    def decorator(func):
        func.permission_level = permission_level
        return func

    return decorator


async def check_interaction_permissions(interaction: discord.Interaction) -> bool:
    """Evaluate slash command permissions using the shared check_permissions logic."""
    if interaction.command is None:
        return True
    ctx = InteractionContext(interaction)
    return await check_permissions(ctx, interaction.command.qualified_name)


async def check_permissions(ctx, command_name) -> bool:
    """Logic for checking permissions for a command for a user"""
    if await ctx.bot.is_owner(ctx.author) or ctx.author.id == ctx.bot.user.id:
        # Bot owner(s) (and creator) has absolute power over the bot
        return True

    permission_level = ctx.bot.command_perm(command_name)

    if permission_level is PermissionLevel.INVALID:
        logger.warning("Invalid permission level for command %s.", command_name)
        return True

    if (
        permission_level is not PermissionLevel.OWNER
        and ctx.channel.permissions_for(ctx.author).administrator
        and ctx.guild == ctx.bot.modmail_guild
    ):
        # Administrators have permission to all non-owner commands in the Modmail Guild
        logger.debug("Allowed due to administrator.")
        return True

    command_permissions = ctx.bot.config["command_permissions"]
    checkables = {*ctx.author.roles, ctx.author}

    if command_name in command_permissions:
        # -1 is for @everyone
        if -1 in command_permissions[command_name] or any(
            str(check.id) in command_permissions[command_name] for check in checkables
        ):
            return True

    level_permissions = ctx.bot.config["level_permissions"]

    for level in PermissionLevel:
        if level >= permission_level and level.name in level_permissions:
            # -1 is for @everyone
            if -1 in level_permissions[level.name] or any(
                str(check.id) in level_permissions[level.name] for check in checkables
            ):
                return True
    return False


def thread_only():
    """
    A decorator that checks if the command
    is being ran within a Modmail thread.
    """

    async def predicate(ctx):
        """
        Parameters
        ----------
        ctx : Context
            The current discord.py `Context`.

        Returns
        -------
        Bool
            `True` if the current `Context` is within a Modmail thread.
            Otherwise, `False`.
        """
        return ctx.thread is not None

    predicate.fail_msg = "This is not a Modmail thread."
    return commands.check(predicate)


def github_token_required(ignore_if_not_heroku=False):
    """
    A decorator that ensures github token
    is set
    """

    async def predicate(ctx):
        if ignore_if_not_heroku and ctx.bot.hosting_method != HostingMethod.HEROKU:
            return True
        else:
            return ctx.bot.config.get("github_token")

    predicate.fail_msg = (
        "You can only use this command if you have a "
        "configured `GITHUB_TOKEN`. Get a "
        "personal access token from developer settings."
    )
    return commands.check(predicate)


def updates_enabled():
    """
    A decorator that ensures
    updates are enabled
    """

    async def predicate(ctx):
        return not ctx.bot.config["disable_updates"]

    predicate.fail_msg = (
        "Updates are disabled on this bot instance. "
        "View `?config help disable_updates` for "
        "more information."
    )
    return commands.check(predicate)
