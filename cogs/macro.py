import random
from typing import List, Optional

import discord
from discord.ext import commands
from imgurpython.imgur.models.image import Image

from core.bot import Bot


class Macro(commands.Cog):
    """
    Runs member interviews, interfaced with Google Sheets as a GUI.

    Instructions:

    1. Clone this sheet and rename it for your server:
    https://docs.google.com/spreadsheets/d/1cC3YtXrXlykd6vfI5Q6y1sw8EGH9walpidZB4BJKTbw/edit?usp=sharing

    2. Share it with this bot's credentialed email (ask the bot owner for it, I'm not uploading it to github).
    (You can query this with the command `iv` if it's been set up in the configuration file.)

    3. Run the command `iv setup #answer_channel #question_channel sheet_name`.

    4. Run the command `iv enable` to open voting.

    5. Once you decide voting is ended, run the command `iv next @vote_winner`.

    6. Share the sheet with the winner, hide old sheet pages, and let them get to answering.

    Note: Most commands are not displayed unless your server is set up for interviews.
    """

    def __init__(self, bot: Bot):
        self.bot = bot

    def _retrieve_images(self, album_ids: List[str]) -> List[Image]:
        images = []
        for album in album_ids:
            images += self.bot.imgur.get_album_images(album)
        return images

    def _retrieve_album_image(self, album_ids: List[str], key: str = None) -> discord.Embed:
        images = self._retrieve_images(album_ids)
        image = random.choice(images)  # type: Image
        if key is not None:
            # if a key is specified, attempt to override the randomly selected image
            for img in images:
                if img.description is not None and img.description.lower() == key.lower():
                    image = img
                    break
        return discord.Embed().set_image(url=image.link)

    @commands.command()
    async def bidoof(self, ctx: commands.Context, key: Optional[str]):
        """
        I can't make a Mafia Bidoof bot *without* this command.

        God bless Mafia Bidoof.
        """
        if not ctx.bot.imgur:
            await ctx.send('Imgur not enabled.')
            return
        BIDOOF_ALBUM = 'kn6ieEv'
        # bidoofs = self._retrieve_images([BIDOOF_ALBUM])
        # bidoof_img = random.choice(bidoofs)  # type: Image
        # if key is not None:
        #     # if a key is specified, attempt to override the randomly selected image
        #     for img in bidoofs:
        #         if img.description is not None and img.description.lower() == key.lower():
        #             bidoof_img = img
        #             break
        # em = discord.Embed().set_image(url=bidoof_img.link)
        em = self._retrieve_album_image([BIDOOF_ALBUM], key=key)
        await ctx.send(embed=em)

    @commands.command()
    async def sadcat(self, ctx: commands.Context, key: Optional[str]):
        """
        Post a random sadcat.

        Optional unique keys can be used to retrieve specific sadcats.
        """
        if not ctx.bot.imgur:
            await ctx.send('Imgur not enabled.')
            return
        SADCAT_ALBUM = ['tYiOD5a', 'kSwj6F5']
        # sadcats = self._retrieve_images(SADCAT_ALBUM)
        # sadcat_img = random.choice(sadcats)  # type: Image
        # if key is not None:
        #     # if a key is specified, attempt to override the randomly selected image
        #     for img in sadcats:
        #         if img.description is not None and img.description.lower() == key.lower():
        #             sadcat_img = img
        #             break
        # em = discord.Embed().set_image(url=bidoof_img.link)
        em = self._retrieve_album_image(SADCAT_ALBUM, key=key)
        await ctx.send(embed=em)


def setup(bot: commands.Bot):
    bot.add_cog(Macro(bot))
