import discord
from discord.ext import commands
from random import choice
import requests


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command()
    async def hello(self, ctx: commands.Context):
        author: discord.Member = ctx.author
        role: str = choice(list(map(str, author.roles[1:])))
        await ctx.send(
            choice(('Hey', 'Hello', 'Hi', 'Ey', 'Yo', 'Sup', ':wave:', 'Good to see you', 'Greetings', 'Peace')) + ', ' + choice((ctx.author.name, 'dude', 'buddy', 'mate', choice(('bro', 'brother')), 'man', 'there', 'silly', 'you', 'master', 'traveler', 'fancypants', 'human', (role if len(role) > 0 else 'nobody'))) + '!')

    @commands.command()
    async def yesno(self, ctx: commands.Context):
        r = requests.get('https://yesno.wtf/api')
        j = r.json()
        embed = discord.Embed(title=j['answer'])
        embed.set_image(url=j['image'])
        await ctx.send(embed=embed)

    @commands.command()
    async def reverse(self, ctx: commands.Context, *options):
        if len(options) < 1:
            await ctx.send('Give me something to reverse.')
        else:
            await ctx.send(f"{ctx.author}: {' '.join(options)[::-1].replace('@','AT')}")


def setup(bot: commands.Bot):
    bot.add_cog(Fun(bot))
