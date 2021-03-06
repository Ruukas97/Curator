import signal
from uuid import UUID

from asyncpg import UniqueViolationError
from discord.ext import commands
import asyncio
import traceback
import discord
import textwrap
import importlib
from contextlib import redirect_stdout
import io
import os
import re
import sys
import copy
import subprocess
from typing import Union, Optional
import random
import bot
from os import kill
from .profile import UserConnection, fetch_user_record

from cogs.utils.paginator import TextPages
from .utils.messages import on_load, on_unload, on_reload, refuse_logout, on_logout, logout_log

# to expose to the eval command


class GlobalChannel(commands.Converter):
    async def convert(self, ctx, argument):
        try:
            return await commands.TextChannelConverter().convert(ctx, argument)
        except commands.BadArgument:
            # Not found... so fall back to ID + global lookup
            try:
                channel_id = int(argument, base=10)
            except ValueError:
                raise commands.BadArgument(f'Could not find a channel by ID {argument!r}.')
            else:
                channel = ctx.bot.get_channel(channel_id)
                if channel is None:
                    raise commands.BadArgument(f'Could not find a channel by ID {argument!r}.')
                return channel


class Admin(commands.Cog):
    """Admin-only commands that make the bot dynamic."""

    def __init__(self, curator: bot.Curator):
        self.bot: bot.Curator = curator
        self._last_result = None
        self.sessions = set()

    async def run_process(self, command):
        try:
            process = await asyncio.create_subprocess_shell(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = await process.communicate()
        except NotImplementedError:
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = await self.bot.loop.run_in_executor(None, process.communicate)

        return [output.decode() for output in result]

    def cleanup_code(self, content):
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')

    async def cog_check(self, ctx: commands.Context):
        if ctx.author.id in self.bot.admins:
            return True
        if not ctx.command.name == 'help':
            await ctx.send(f'This command is only for the bot admin{"" if len(self.bot.admins) == 1 else "s"}.')
        return False

    def get_syntax_error(self, e):
        if e.text is None:
            return f'```py\n{e.__class__.__name__}: {e}\n```'
        return f'```py\n{e.text}{"^":>{e.offset}}\n{e.__class__.__name__}: {e}```'

    @commands.command(hidden=True, aliases=['modules'])
    async def extensions(self, ctx):
        """Lists the currently loaded modules."""
        await ctx.send('\n'.join(sorted([i for i in self.bot.extensions.keys()])))

    @commands.command(hidden=True)
    async def load(self, ctx, *, module):
        """Loads a module."""
        try:
            self.bot.load_extension(module)
        except commands.ExtensionError as e:
            await ctx.send(f'{e.__class__.__name__}: {e}')
        else:
            print(f'Loaded {module}')
            await ctx.send(on_load())

    @commands.command(hidden=True)
    async def unload(self, ctx, *, module):
        """Unloads a module."""
        try:
            self.bot.unload_extension(module)
        except commands.ExtensionError as e:
            await ctx.send(f'{e.__class__.__name__}: {e}')
        else:
            print(f'Unloaded {module}')
            await ctx.send(on_unload())

    @commands.group(name='reload', aliases=['update'], hidden=True, invoke_without_command=True)
    async def _reload(self, ctx, *, module):
        """Reloads a module."""
        try:
            self.bot.reload_extension(module)
        except commands.ExtensionError as e:
            await ctx.send(f'{e.__class__.__name__}: {e}')
        else:
            print(f'Reloaded {module}')
            await ctx.send(on_reload())

    _GIT_PULL_REGEX = re.compile(r'\s*(?P<filename>.+?)\s*\|\s*[0-9]+\s*[+-]+')

    def find_modules_from_git(self, output):
        files = self._GIT_PULL_REGEX.findall(output)
        ret = []
        for file in files:
            root, ext = os.path.splitext(file)
            if ext != '.py':
                continue

            if root.startswith('cogs/'):
                # A submodule is a directory inside the main cog directory for
                # my purposes
                ret.append((root.count('/') - 1, root.replace('/', '.')))

        # For reload order, the submodules should be reloaded first
        ret.sort(reverse=True)
        return ret

    def reload_or_load_extension(self, module):
        try:
            self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            self.bot.load_extension(module)

    @_reload.command(name='all', hidden=True)
    async def _reload_all(self, ctx):
        """Reloads all modules, while pulling from git."""

        async with ctx.typing():
            stdout, stderr = await self.run_process('cd Curator;git pull')

        # progress and stuff is redirected to stderr in git pull
        # however, things like "fast forward" and files
        # along with the text "already up-to-date" are in stdout

        if stdout.startswith('Already up-to-date.'):
            return await ctx.send(stdout)

        modules = self.find_modules_from_git(stdout)
        mods_text = '\n'.join(f'{index}. `{module}`' for index, (_, module) in enumerate(modules, start=1))
        prompt_text = f'This will update the following modules, are you sure?\n{mods_text}'
        confirm = await ctx.prompt(prompt_text, reacquire=False)
        if not confirm:
            return await ctx.send('Aborting.')

        statuses = []
        for is_submodule, module in modules:
            if is_submodule:
                try:
                    actual_module = sys.modules[module]
                except KeyError:
                    statuses.append((ctx.tick(None), module))
                else:
                    try:
                        importlib.reload(actual_module)
                    except Exception as e:
                        statuses.append((ctx.tick(False), module))
                    else:
                        statuses.append((ctx.tick(True), module))
            else:
                try:
                    self.reload_or_load_extension(module)
                except commands.ExtensionError:
                    statuses.append((ctx.tick(False), module))
                else:
                    statuses.append((ctx.tick(True), module))

        await ctx.send('\n'.join(f'{status}: `{module}`' for status, module in statuses))

    @commands.command(hidden=True, name='eval')
    async def _eval(self, ctx, *, body: str):
        """Evaluates a code"""

        env = {
            'curator': self.bot,
            'ctx': ctx,
            'channel': ctx.channel,
            'author': ctx.author,
            'guild': ctx.guild,
            'message': ctx.message,
            '_': self._last_result
        }

        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:
            return await ctx.send(f'```py\n{e.__class__.__name__}: {e}\n```')

        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = await func()
        except Exception as e:
            value = stdout.getvalue()
            await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction('\u2705')
            except:
                pass

            if ret is None:
                if value:
                    await ctx.send(f'```py\n{value}\n```')
            else:
                self._last_result = ret
                await ctx.send(f'```py\n{value}{ret}\n```')

    @commands.command(hidden=True)
    async def sudo(self, ctx, channel: Optional[GlobalChannel], who: Union[discord.Member, discord.User], *, command: str):
        """Run a command as another user optionally in another channel."""
        msg = copy.copy(ctx.message)
        channel = channel or ctx.channel
        msg.channel = channel
        msg.author = channel.guild.get_member(who.id) or who
        msg.content = ctx.prefix + command
        new_ctx = await self.bot.get_context(msg, cls=type(ctx))
        new_ctx._db = ctx._db
        await self.bot.invoke(new_ctx)

    @commands.command(hidden=True)
    async def sh(self, ctx, *, command):
        """Runs a shell command."""
        async with ctx.typing():
            stdout, stderr = await self.run_process(command)

        if stderr:
            text = f'stdout:\n{stdout}\nstderr:\n{stderr}'
        else:
            text = stdout

        try:
            pages = TextPages(ctx, text)
            await pages.paginate()
        except Exception as e:
            await ctx.send(str(e))

    @commands.command(hidden=True)
    async def sql(self, ctx, *, command):
        """Runs a sql query."""
        try:
            res = await self.bot.pool.fetch(command)
            await ctx.send(str(res))
        except Exception as e:
            await ctx.send(str(e))

    @commands.command(hidden=True)
    async def select(self, ctx, *, command):
        """Runs a sql select query."""
        await ctx.invoke(self.sql, command='select '+command)

    @commands.command(hidden=True)
    async def mcuuid(self, ctx, who: discord.User, minecraft_uuid: str):
        """Link a Minecraft account to a Discord account."""
        minecraft_uuid = UUID(minecraft_uuid)
        if who and who.id and minecraft_uuid:
            try:
                async with UserConnection(await fetch_user_record(discord_id=who.id, connection=self.bot.pool),
                                          connection=self.bot.pool) as user:
                    user.minecraft_uuid = minecraft_uuid
                    await ctx.send(f'Updated: {self.bot.get_user(user.discord_id)} = {user.minecraft_uuid}')
            except UniqueViolationError as e:
                await ctx.send(f'Error: {e}')
        else:
            await ctx.send(f'Something went wrong with arguments: {who} and {minecraft_uuid}')

    @commands.command(hidden=True)
    async def authlist(self, ctx):
        """Get a list of Discord accounts and their linked Minecraft accounts."""
        query = 'SELECT * FROM profiles WHERE minecraft_uuid IS NOT NULL'
        values = await self.bot.pool.fetch(query)
        if values:
            await ctx.send('\n'.join(f'{self.bot.get_user(i["discord_id"])}: {i["minecraft_uuid"]}' for i in values))
        else:
            await ctx.send('The list was empty.')

    @commands.command(hidden=True)
    async def processes(self, ctx):
        """Get a list of processes running on the bot's server."""
        await self.sh(ctx, command='ps -A')

    @commands.command(hidden=True)
    async def oskill(self, ctx, pid: int):
        """Kill a running process."""
        kill(pid, signal.SIGKILL)
        await ctx.send('Sent kill signal.')

    @commands.command(aliases=['kill', 'die'], hidden=True)
    async def logout(self, ctx: commands.Context):
        """Turn off the bot."""
        if random.randint(1, 5) == 1:
            await ctx.send(refuse_logout())
        else:
            await ctx.send(on_logout(ctx))
            await self.bot.get_channel(474922467626975233).send(logout_log(self.bot.user.display_name))
            print(logout_log(self.bot.user.display_name))
            await ctx.bot.logout()

    @commands.command(hidden=True)
    async def findmember(self, ctx: commands.Context, filter: str):
        """Find a member by (a part of) their name."""
        all = ctx.guild.members
        members = [m.mention for m in all if filter in str(m).lower()]
        if members:
            await ctx.send('\n'.join(members))
        else:
            await ctx.send('No members found.')

    @commands.command(hidden=True)
    async def unguild(self, ctx: commands.Context, guild_id: int):
        pass

    @commands.command(hidden=True)
    async def unchannel(self, ctx: commands.Context, channel_id: int):
        pass


def setup(curator: bot.Curator):
    curator.add_cog(Admin(curator))
