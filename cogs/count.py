import datetime
from typing import Optional

import asyncpg
import discord
from discord.ext import commands
import emoji

import bot
from . import profile
from .utils import db


class Counts(db.Table):
    id = db.PrimaryKeyColumn()

    started_by = db.Column(db.ForeignKey(table='profiles', column='discord_id', sql_type=db.Integer(big=True)))
    started_at = db.Column(db.Datetime, default="now() at time zone 'utc'")

    score = db.Column(db.Integer, default='0')
    contributors = db.Column(db.JSON, default="'{}'::jsonb")

    timed_out = db.Column(db.Boolean, default="FALSE")
    duration = db.Column(db.Interval)
    ruined_by = db.Column(db.ForeignKey(table='profiles', column='discord_id', sql_type=db.Integer(big=True)))

    type = db.Column(db.String, default="normal")


class Counters(db.Table):
    user_id = db.Column(db.ForeignKey(table='profiles', column='discord_id', sql_type=db.Integer(big=True)),
                        primary_key=True)
    last_count = db.Column(db.ForeignKey(table='counts', column='id', sql_type=db.Integer()))
    best_count = db.Column(db.ForeignKey(table='counts', column='id', sql_type=db.Integer()))
    best_ruin = db.Column(db.ForeignKey(table='counts', column='id', sql_type=db.Integer()))
    total_score = db.Column(db.Integer, default=0)
    counts_participated = db.Column(db.Integer, default=0)
    counts_ruined = db.Column(db.Integer, default=0)
    counts_started = db.Column(db.Integer, default=0)


number_aliases = {
    ':keycap_0:': ['0'],
    ':O_button_(blood_type):': ['0'],
    ':hollow_red_circle:': ['0'],
    ':keycap_1:': ['1'],
    ':1st_place_medal:': ['1'],
    ':keycap_2:': ['2'],
    ':2nd_place_medal:': ['2'],
    ':keycap_3:': ['3'],
    ':3rd_place_medal:': ['3'],
    ':evergreen_tree:': ['3'],
    ':deciduous_tree:': ['3'],
    ':palm_tree:': ['3'],
    ':christmas_tree:': ['3'],
    ':cactus:': ['3'],
    ':shamrock:': ['3'],
    ':keycap_4:': ['4'],
    ':four_leaf_clover:': ['4'],
    ':keycap_5:': ['5'],
    ':keycap_6:': ['6'],
    ':keycap_7:': ['7'],
    ':keycap_8:': ['8'],
    ':pool_8_ball:': ['8'],
    ':keycap_9:': ['9'],
    ':keycap_10:': ['10'],
    ':ok_hand:': ['69'],
    ':cancer:': ['69'],
    ':hundred_points:': ['100', '00'],
    ':input_numbers:': ['1234']
}


def parsed(number: str) -> str:
    s = emoji.demojize(number)
    for key in number_aliases.keys():
        for i in range(s.count(key)):
            s = ', '.join([s.replace(key, alias, 1) for alias in number_aliases[key]])
    s = ', '.join(set([i for i in s.split(', ') if i.isdigit()]))
    return s or 'invalid'


def is_number(number: str, to_check: str) -> bool:
    return parsed(to_check) == number


class CounterProfile:
    __slots__ = (
        'user_id', 'last_count', 'best_count', 'best_ruin', 'total_score', 'counts_participated', 'counts_ruined',
        'counts_started')

    def __init__(self, *, d: dict):
        self.user_id = d['user_id']
        self.last_count = d['last_count']
        self.best_count = d['best_count']
        self.best_ruin = d['best_ruin']
        self.total_score = d['total_score']
        self.counts_participated = d['counts_participated']
        self.counts_ruined = d['counts_ruined']
        self.counts_started = d['counts_started']


class Counter:
    __slots__ = ('original', 'current')
    original: CounterProfile
    current: CounterProfile

    def __init__(self, *, record):
        self.original = CounterProfile(d=db.dict_from_record(record))
        self.current = CounterProfile(d=db.dict_from_record(record))

    @classmethod
    async def load(cls, discord_id, *, connection=bot.instance.pool):
        return cls(record=await connection.fetchrow(
            'INSERT INTO counters (user_id) VALUES ($1) ON CONFLICT (discord_id) DO UPDATE SET user_id = counters.user_id RETURNING *',
            discord_id))

    async def save(self, *, connection=bot.instance.pool):
        original_keys = self.original.__dict__.keys()
        updates = [(key, value) for key, value in self.current.__dict__.items() if
                   key not in original_keys or value != self.original[key]]
        if updates:
            await connection.execute(
                f'UPDATE SET {", ".join([str(key) + " = " + str(value) for key, value in updates])} RETURNING *;')

    def __enter__(self) -> CounterProfile:
        return self.current

    def __exit__(self):
        self.save()

    def __repr__(self):
        return f'<Counter discord_id={self.original["discord_id"]}>'


class Counting:
    __slots__ = (
        'id', 'started_by', 'started_at', 'score', 'contributors', 'last_active_at', 'last_counter', 'timed_out',
        'duration', 'ruined_by')
    contributors: dict

    def __init__(self, *, record):
        self.id = record['id']
        self.started_by = record['started_by']
        self.started_at = record['started_at']
        self.score = record['score']
        self.contributors = record['contributors']
        self.last_active_at = record['last_active_at']
        self.last_counter = record['last_counter']
        self.timed_out = False
        self.ruined_by = None

    @classmethod
    def temporary(cls, *, started_by, started_at=datetime.datetime.utcnow(), score=0, contributors=None,
                  last_active_at=datetime.datetime.utcnow(), last_counter=None):
        if contributors is None:
            contributors = {}
        pseudo = {
            'id': None,
            'started_by': started_by,
            'started_at': started_at,
            'score': score,
            'contributors': contributors,
            'last_active_at': last_active_at,
            'last_counter': last_counter
        }
        return cls(record=pseudo)

    def attempt_count(self, counter: discord.User, count: str) -> bool:
        if self.is_next(count) and counter.id != self.last_counter:
            self.last_active_at = datetime.datetime.utcnow()
            self.last_counter = counter.id
            self.score += 1
            if counter.id not in self.contributors.keys():
                self.contributors[counter.id] = 1
            else:
                self.contributors[counter.id] += 1
            return True
        return False

    def is_next(self, message: str):
        return is_number(str(self.score + 1), message.split()[0])

    async def finish(self, curator: bot.Curator, timed_out: bool, ruined_by: discord.User):
        connection: asyncpg.pool = curator.pool
        self.timed_out = timed_out
        self.ruined_by = ruined_by.id

        with Counter.load(discord_id=self.started_by) as counter:
            counter.counts_started += 1

        with Counter.load(discord_id=self.ruined_by) as counter:
            counter.counts_ruined += 1

        query = """INSERT INTO counts (started_by, started_at, score, contributors, timed_out, duration, ruined_by )
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                   RETURNING id;
                """
        self.id = await connection.fetchval(query, self.started_by, self.started_at, self.score, self.contributors,
                                            self.timed_out, datetime.datetime.utcnow() - self.started_at,
                                            self.ruined_by)

        score_query = 'SELECT score FROM counts where id=$1'

        for discord_id, contribution in self.contributors.items():
            with Counter.load(discord_id=discord_id) as counter:
                counter.last_count = self.id
                counter.total_score += contribution
                counter.counts_participated += 1

                if counter.best_count is None:
                    counter.best_count = self.id
                else:
                    best_score = await connection.fetchval(score_query, counter.best_count)
                    if not best_score or best_score < self.score:
                        counter.best_count = self.id

                if counter.user_id == self.started_by:
                    counter.counts_started += 1

                if counter.user_id == self.ruined_by:
                    counter.counts_ruined += 1
                    if counter.best_ruin is None:
                        counter.best_ruin = self.id
                    else:
                        best_ruin_score = await connection.fetchval(score_query, counter.best_ruin)
                        if not best_ruin_score or best_ruin_score < self.score:
                            counter.best_ruin = self.id


class Count(commands.Cog):
    def __init__(self, curator: bot.Curator):
        self.bot = curator
        self.counting = None
        self.count_channel = None

    def is_count_channel(self, channel: discord.TextChannel):
        return 'count' in channel.name.lower()

    async def check_channel(self, channel: discord.TextChannel, message=False) -> bool:
        if not self.is_count_channel(channel):
            if message:
                await channel.send(
                    'Count commands are intended for use only in channels that contain "count" in the name...')
            return False
        return True

    async def check_count(self, message: discord.Message) -> bool:
        if not self.is_count_channel(message.channel) or self.counting is None:
            return False

        c: Counting = self.counting

        if not c.attempt_count(message.author, message.content.split()[0]):
            await message.channel.send(message.author.mention + ' failed, and ruined the count for ' + str(
                len(c.contributors.keys())) + ' counters...\nThe count reached ' + str(c.score) + '.')
            await c.finish(self.bot, False, message.author)
            self.counting = None
            return False
        return True

    @commands.group(invoke_without_command=True)
    async def count(self, ctx: commands.Context):
        if await self.check_channel(ctx.channel):
            await ctx.send(f'You need to supply a subcommand. Try {ctx.prefix}help count')

    @count.command()
    async def start(self, ctx: commands.Context):
        if await self.check_channel(ctx.channel):
            await ctx.send('Count has been started. Good luck!')
            self.counting = Counting.temporary(started_by=ctx.author.id)
        else:
            await ctx.send("You can't start a count outside of the count channel.")

    @count.command()
    async def profile(self, ctx: commands.Context, *, user: Optional[discord.User]):
        user: discord.User = user or ctx.author
        counter: CounterProfile = await self.get_profile_with_create(user.id)

        if profile:
            embed = discord.Embed(title=f'{user.name} - counting profile')
            embed.add_field(name='Total Score', value=f'{counter.total_score} counts')
            embed.add_field(name='Contributed in', value=f'{counter.counts_participated} rounds')
            embed.add_field(name='Rounds Started', value=f'{counter.counts_started} rounds')
            embed.add_field(name='Rounds Ruined', value=f'{counter.counts_ruined} rounds')
            embed.add_field(name='Best Round', value=f'Round {counter.best_count}')
            embed.add_field(name='Worst Fail', value=f'Round {counter.best_ruin}')
            embed.add_field(name='Last Count', value=f'Round {counter.last_count}')
            await ctx.send(embed=embed)
        else:
            await ctx.send('Could not find your profile.')

    @count.command()
    async def data(self, ctx: commands.Context):
        if self.counting:
            await ctx.send(self.counting.__dict__)
        else:
            await ctx.send('No count is running.')

    @count.command(aliases=['best', 'highscore', 'hiscore', 'top'])
    async def leaderboard(self, ctx: commands.Context):
        embed = discord.Embed(title='Count Leaderboard', description='Top 5 Highest Counts :slight_smile:')
        query = 'SELECT score, contributors FROM counts ORDER BY score DESC LIMIT 5;'
        rows = await self.bot.pool.fetch(query)
        users = {
        }
        i = 0
        for row in rows:
            i += 1
            contributors = row['contributors']
            keys = contributors.keys()
            a = [f'**Score: {row["score"]}**']
            for user_id in keys:
                if user_id in users.keys():
                    name = users[user_id]
                else:
                    member = await ctx.guild.fetch_member(user_id)
                    name = member.name
                    users[user_id] = name

                a.append(f'**{name}**: {contributors[user_id]}')

            embed.add_field(name=str(i), value='\n'.join(a), inline=False)

        await ctx.send(embed=embed)

    @count.command()
    async def parse(self, ctx: commands.Context, number: str):
        parse = parsed(number)
        await ctx.send(parse)


def setup(curator: bot.Curator):
    curator.add_cog(Count(curator))
