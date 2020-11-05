from datetime import datetime
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union, Generator, Tuple

import discord
from discord.ext import commands
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from cogs import interview_schema as schema
from cogs.interview_schema import Server, Meta, Vote, OptOut, TotalQuestions
from core.bot import Bot
from utils import menu, spreadsheet

_DEBUG_FLAG = True  # TODO: toggle to off

DB_DIR = 'databases'
DB_FILE = f'{DB_DIR}/interviews.db'


# For some awful reason, SQLite doesn't turn on foreign key constraints by default.
# This is the fix.
# TODO: Move this to a SQL utility file so it gets run globally every time :)
@event.listens_for(Engine, 'connect')
def set_sqlite_pragma(dbapi_connection, connection_record):
    # Some examples online would have you only run this if the SQLite version is high enough to
    # support foreign keys. That isn't a concern here. If your SQLite doesn't support foreign keys,
    # it can crash and burn.
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA foreign_keys=ON')
    cursor.close()


session_maker = None  # type: Optional[sessionmaker]

# Google sheets API constants
SCOPE = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
SECRET = 'conf/google_creds.json'
SHEET_NAME = 'eimm role templates & keywords'

SERVER_LEFT_MSG = '[Member Left]'
ERROR_MSG = '[Bad User]'


class Candidate:
    """
    Utility class, used only for votals.
    """

    def __init__(self, ctx: commands.Context, candidate_id: int):
        self._ctx = ctx
        self.candidate = ctx.guild.get_member(candidate_id)
        self.voters = []

    def str(self, length: int) -> str:
        return f'{name_or_default(self.candidate) + ":" : <{length + 1}}'

    def voters_str(self) -> str:
        # Hopefully it should never fall through to default! Preprocessing strips those out.
        voters = [name_or_default(self._ctx.guild.get_member(voter)) for voter in self.voters]
        voters = sorted(voters, key=lambda x: str(x).lower())
        return ', '.join(voters)

    def basic_str(self, length: int) -> str:
        return f'{self.str(length)} {len(self.voters)}'

    def full_str(self, length: int) -> str:
        return f'{self.str(length)} {len(self.voters)} ({self.voters_str()})'

    def sortkey(self) -> Tuple[int, str]:
        """
        Sort first by number of votes, then alphabetically.
        Votes are negative so that sorting by votes (greatest to least) can be consistent with sorting
        alphabetically (A to Z).
        """
        return -len(self.voters), name_or_default(self.candidate).lower()


class Question:
    def __init__(self, interviewee: discord.Member, asker: Union[discord.Member, discord.User],
                 # question: str, question_num: int, server_id: int, channel_id: int, message_id: int,
                 question: str, question_num: int, message: discord.Message,
                 answer: str = None, timestamp: datetime = None):
        self.interviewee = interviewee
        self.asker = asker
        self.question = question
        self.question_num = question_num
        self.message = message
        # self.server_id = server_id
        # self.channel_id = channel_id
        # self.message_id = message_id

        self.answer = answer
        if timestamp is None:
            self.timestamp = datetime.utcnow()
        else:
            self.timestamp = timestamp

    @staticmethod
    async def from_row(ctx: commands.Context, row: Dict[str, Any]) -> 'Question':
        """
        Translates a row from the Google sheet to an object.
        """
        channel = await ctx.bot.get_channel(row['Channel ID'])  # type: discord.TextChannel
        message = await channel.fetch_message(row['Message ID'])
        return Question(
            # This is a bit dangerous, but should be fine! only the interviewee will be calling the answer method:
            interviewee=ctx.author,
            asker=ctx.guild.get_member(row['ID']),
            question=row['question'],
            question_num=row['#'],
            # row['Server ID'],
            # row['Channel ID'],
            # row['Message ID'],
            message=message,  # replaced the prev three rows
            answer=row['Answer'],
            timestamp=datetime.utcfromtimestamp(row['POSIX Timestamp']),  # TODO: need to test this conversion
        )

    def upload(self, ctx: commands.Context, connection: spreadsheet.SheetConnection):
        """
        Upload a question to the Google sheet.
        """
        # TODO: ctx may be unnecessary
        pass

    def question_words(self) -> Generator[str, None, None]:
        """
        Add angle braces as quote styling *after* this method.
        """
        words = self.question.replace('[', '\\[').replace(']', '\\]').split(' ')
        for word in words:
            yield word

    def answer_words(self) -> Generator[str, None, None]:
        """
        Add angle braces as quote styling *after* this method.
        """
        words = self.answer.replace('[', '\\[').replace(']', '\\]').split(' ')
        for word in words:
            yield word

    # NOTE: This won't work, since you need to be able to answer multiple questions at once.
    # def answer(self, channel: discord.TextChannel):
    #     """
    #     Post the answer to a completed question to Discord.
    #     """
    #     assert self.answer is not None  # probably important
    #     embed = answer_embed
    #     await channel.send(...)  # TODO
    #     pass


# def alphabetize_users(u1: discord.User, u2: discord.User) -> bool:
#     """
#     True if u1 is alphabetically ahead of u2. Case-insensitive.
#     """
#     if u2 is None:
#         return True
#     if u1 is None:
#         return False
#     return str(u1).lower() <= str(u2).lower()


def name_or_default(user: discord.User) -> str:
    if user is not None:
        return str(user)
    return SERVER_LEFT_MSG


def translate_name(member: Union[discord.Member, discord.User]):
    if member is None:
        return SERVER_LEFT_MSG
    if type(member) is discord.User:
        return member.name
    if type(member) is discord.Member:
        return member.nick
    return ERROR_MSG


def blank_answer_embed(interviewee: discord.Member, asker: Union[discord.Member, discord.User],
                       avatar_url: str = None) -> discord.Embed:
    em = discord.Embed(
        title=f"**{interviewee}**'s interview",
        description=' ',
        color=interviewee.color,
        url='',  # TODO: fill in
    )
    if avatar_url is None:
        em.set_thumbnail(url=interviewee.avatar_url)
    else:
        em.set_thumbnail(url=avatar_url)
    em.set_author(
        name=f'Asked by {asker}',
        icon_url=asker.avatar_url,
        url='',  # TODO: fill in
    )
    return em


def message_link(server_id: int, channel_id: int, message_id: int) -> str:
    # message = await ctx.guild.get_message(message_id)
    # return message.link
    return f'https://discordapp.com/channels/{server_id}/{channel_id}/{message_id}'


def add_question(em: discord.Embed, question: Question):
    """
    Questions and answers are added to embed fields, each of which has a maximum of 1000 chars.
    Need to check (and possibly split up) each question and answer to make sure they don't overflow and break
    the embed. This is... somewhat frustrating.
    """
    # TODO: test the replaces
    # question_text = question.question.strip().replace('\n', '\n> ').replace('[', '\\[').replace(']', '\\]')
    question_text = question.question.replace('[', '\\[').replace(']', '\\]')
    question_lines = [line.strip() for line in question_text.split('\n')]

    # sum of all text in all lines PLUS accounting for adding '> ' and '\n' to each line PLUS the question's answer:
    # When splitting up questions, assume 85 (round to 100) characters for the message link markdown, so you get 900
    # chars rather than 1000.
    if sum([len(line) for line in question_lines]) + len(question_lines) * 3 + len(question.answer) <= 900:
        formatted_question_text = f'[> {"> ".join(question_lines)}]({question.message.jump_url}'
        em.add_field(
            name=f'Question #{question.question_num}',
            value=f'{formatted_question_text}\n{question.answer}'
        )
        return

    # Split and add to separate fields:

    question_chunk = '[> '
    question_chunks = []
    for word in question.answer_words():
        if len(question_chunk) > 900:
            question_chunk += f']({question.message.jump_url})'
            question_chunks.append(question_chunk)
            question_chunk = '[> '
        word = word.replace("\n", "\n> ")
        question_chunk += f'{word}'
    question_chunk += f']({question.message.jump_url})'
    question_chunks.append(question_chunk)

    for i, chunk in enumerate(question_chunks):
        em.add_field(name=f'Question #{question.question_num} [{i}/{len(question_chunks)}]', value=chunk)

    answer_chunk = ''
    answer_chunks = []
    for word in question.answer_words():
        if len(answer_chunk) > 950:
            answer_chunks.append(answer_chunk)
            answer_chunk = ''
        answer_chunk += word

    for i, chunk in enumerate(question_chunks):
        em.add_field(name=f'Answer #{question.question_num} [{i}/{len(answer_chunks)}]', value=chunk)

    # split_question = []
    # # while len(question_text) > 900:
    # #     pos = question_text[:900].rfind(' ')
    # #     if pos == -1:
    # #         raise ValueError('Text cannot be split')
    # #     text = question_text[:pos]
    # #     if text[-1] == '>':
    # #         text = text[:-1]
    # #     question_text = '> ' + question_text[pos + 1:]
    # #     split_question.append(text)
    #
    # answer_text = question.answer
    # split_answer = []
    # while len(answer_text) >= 1000:
    #     pos = answer_text[:999].rfind(' ')
    #     if pos == -1:
    #         raise ValueError('Answer cannot be split')
    #     text = answer_text[:pos]
    #     answer_text = answer_text[pos + 1:]
    #     split_answer.append(text)
    #
    # # TODO: more
    pass


def _server_active(ctx: commands.Context):
    """
    Command check to make sure the server is set up for interviews.
    """
    if _DEBUG_FLAG:
        return True
    session = session_maker()
    server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()
    return server is not None


def _interview_enabled(ctx: commands.Context):
    """
    Command check to make sure the interview is not disabled.

    Used for voting, opting out, and answering questions.
    """
    pass


class Interview(commands.Cog):
    """
    Runs member interviews, interfaced with Google Sheets as a GUI.

    # TODO: Write actual instructions and info for this module here.

    Note: Most commands are not displayed unless your server is set up for interviews.
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.connection = None  # type: Optional[spreadsheet.SheetConnection]
        self.load()

    def load(self):
        self.connection = spreadsheet.SheetConnection(SECRET, SCOPE)

        if not Path(DB_FILE).exists():
            # TODO: Don't technically need this condition?
            # Adds a bit of clarity though, so keeping it in for now.
            Path(DB_DIR).mkdir(exist_ok=True)
        engine = create_engine(f'sqlite:///{DB_FILE}')
        global session_maker
        session_maker = sessionmaker(bind=engine)

        schema.Base.metadata.create_all(engine)

    def new_interview(self):
        # TODO: yes.
        pass

    # == Helper methods ==

    def _generate_embeds(self, interviewee: discord.Member, questions: List[Question],
                         avatar_url=None) -> Generator[discord.Embed, None, None]:
        """
        TODO: what was i doing here
        Generate discord.Embeds to be posted from a list of Questions.
        """
        # TODO: it's entirely possible this wants to be a static method
        last_asker = None  # type: Optional[discord.Member]
        em = None  # type: Optional[discord.Embed]
        for question in questions:
            if last_asker != question.asker:
                if em is not None:
                    # TODO: add footer but idk what to do with it
                    yield em
                # make a new embed
                em = blank_answer_embed(interviewee, question.asker, avatar_url=avatar_url)  # TODO: update avatar url?
            # TODO: add question/answer fields
            add_question(em, question)
            # TODO: update answered questions per asker? or whatever it is?
            last_asker = question.asker
        if em is not None:
            # TODO: add footer but idk what to do with it
            yield em

    def _reset_meta(self, server: discord.Guild):
        """
        Set up the meta entry for a new interview.
        """
        # TODO: yes. also this probably needs more arguments. it may not even want to exist.
        pass

    # == Setup ==

    @commands.group('iv')
    @commands.has_permissions(administrator=True)
    async def iv(self, ctx: commands.Context):
        """
        # TODO: Write actual instructions and info for this group here.
        """
        # TODO: yes.
        #  document command group
        #  TODO: write instructions on setting up on a new server
        pass

    @iv.command(name='setup')
    @commands.has_permissions(administrator=True)
    async def iv_setup(self, ctx: commands.Context, answers: discord.TextChannel,
                       backstage: discord.TextChannel, sheet_name: str):
        """
        Set up the current server for interviews.

        Answer channel is where where answers to questions will be posted, backstage is a private space for
        the bot to be controlled, sheet_name is the URL of your interview sheet.
        If your sheet name is multiple words, enclose it in double quotes, e.g., "sheet name".

        Copy the sheet template from TODO: add a link here.
        """
        # TODO: yes.
        #  setup server + channels for interview
        #  update all databases

        session = session_maker()

        server = schema.Server(
            id=ctx.guild.id,
            sheet_name=sheet_name,
            answer_channel=answers.id,
            back_channel=backstage.id,
        )
        meta = schema.Meta(
            # server_id field filled in by assigning the relationship in the next statement
            interviewee_id=0,
            start_time=datetime.utcnow(),
            num_questions=0,
            limit=datetime.utcfromtimestamp(0),
            reinterviews_allowed=False,
            active=False,
        )
        meta.server = server
        session.add(server)
        session.commit()
        await ctx.send(f'Set up {ctx.guild} for interviews.\n'
                       f'Answers will be posted in {answers}, hidden channel is {backstage}.')
        await ctx.message.add_reaction(ctx.bot.greentick)

    @iv.command(name='next')
    @commands.has_permissions(administrator=True)
    @commands.check(_server_active)
    async def iv_next(self, ctx: commands.Context, interviewee: discord.Member, *, email: Optional[str] = None):
        """
        TODO: Set up the next interview for <interviewee>.

        Creates a new interview sheet for the next interviewee. If the optional <email> parameter is provided,
        shares the document with them. Old emails must still be cleared out manually.
        # TODO: deprecate former behavior of using current channel as new backstage
        """

        # TODO: yes.
        #  setup a new interview
        #  upload the interviewee's current avatar to make it not break in the future?
        #  do we want the email to be private? not sure yet
        #  update metadata (maybe other databases?)
        pass

    # TODO (maybe): Add methods to change the answer/backstage channels.

    @iv.command(name='disable')
    @commands.has_permissions(administrator=True)
    @commands.check(_server_active)
    async def iv_disable(self, ctx: commands.Context):
        """
        Disable voting and question asking for the current interview.
        """
        session = session_maker()
        meta = session.query(schema.Meta).filter_by(server_id=ctx.guild.id).one_or_none()
        if meta is None:
            await ctx.send(f'Interviews are not set up for {ctx.guild}.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        if meta.active is False:
            await ctx.send(f'Interviews are already disabled.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        meta.active = False
        session.commit()
        await ctx.message.add_reaction(ctx.bot.greentick)

    @iv.command(name='enable')
    @commands.has_permissions(administrator=True)
    @commands.check(_server_active)
    async def iv_enable(self, ctx: commands.Context):
        """
        Re-enable voting and question asking for the current interview.
        """
        session = session_maker()
        meta = session.query(schema.Meta).filter_by(server_id=ctx.guild.id).one_or_none()
        if meta is None:
            await ctx.send(f'Interviews are not set up for {ctx.guild}.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        if meta.active is True:
            await ctx.send(f'Interviews are already enabled.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        meta.active = True
        session.commit()
        await ctx.message.add_reaction(ctx.bot.greentick)

    @iv.command(name='stats')
    @commands.check(_server_active)
    async def iv_stats(self, ctx: commands.Context):
        """
        TODO: View interview-related stats.

        Which stats? I dunno. # TODO: Figure that out.
        """
        # TODO: yes.
        pass

    # == Questions ==

    @commands.command()
    @commands.check(_server_active)
    # TODO: check for interview active
    async def ask(self, ctx: commands.Context, *, question: str):
        """
        TODO: Submit a question for the current interview.
        """
        # TODO:
        #  upload question to sheet
        #  update metadata
        pass

    @commands.command()
    @commands.check(_server_active)
    # TODO: check for interview active
    async def mask(self, ctx: commands.Context, *, questions_str: str):
        """
        TODO: Submit multiple questions for the current interview.

        Each question must be a single line, separated by linebreaks.
        """
        # TODO:
        #  split up questions
        #  upload all questions to sheet
        #  update metadata
        pass

    # == Answers ==

    @commands.command()
    @commands.check(_server_active)
    async def answer(self, ctx: commands.Context):
        """
        TODO: Post all answers to questions that have not yet been posted.

        Questions will be grouped by asker, rather than strictly in chronological order.
        # TODO: Add a flag to post strictly chronologically?
        """
        # TODO:
        #  check if invoker is interviewee
        #  dump a bunch of answers
        #  update sheet
        pass

    @commands.command()
    @commands.check(_server_active)
    async def preview(self, ctx: commands.Context):
        """
        TODO: Preview answers, visible in the backstage channel.
        """
        # TODO:
        #  check if invoker is interviewee
        #  dump a bunch of answers
        pass

    # == Votes ==

    @staticmethod
    def _votes_footer(votes: List[discord.User], prefix: str = None):
        if len(votes) == 0:
            return f'_You are not currently voting; vote with `{prefix}vote`._'

        votes = sorted(votes, key=lambda x: name_or_default(x).lower())
        return '_You are currently voting for: ' + ', '.join([f'`{name_or_default(vote)}`' for vote in votes]) + '._'

    @staticmethod
    def _preprocess_votals(ctx: commands.Context, votes: List[schema.Vote]) -> List[Candidate]:
        """
        Returns a list of candidates and vote counts, sorted by (vote count, alphabetical name).
        """
        candidates = list(set(vote.candidate_id for vote in votes))
        votals = {}
        for candidate_id in candidates:
            votals[candidate_id] = Candidate(ctx, candidate_id)
            for vote in votes:
                if vote.candidate_id == candidate_id and ctx.guild.get_member(vote.voter_id) is not None:
                    votals[candidate_id].voters.append(vote.voter_id)

        return sorted(list(votals.values()), key=lambda x: x.sortkey())

    @staticmethod
    def _votals_text_basic(ctx: commands.Context, votes: List[schema.Vote]) -> str:
        votals = Interview._preprocess_votals(ctx, votes)
        text = ''
        max_name_length = len(SERVER_LEFT_MSG)
        for candidate in votals:
            if len(str(candidate)) > max_name_length:
                max_name_length = len(str(candidate.candidate))
        for candidate in votals:
            s = candidate.basic_str(max_name_length)
            if len(text) + len(s) > 1750:
                # Break if it's getting too long for a single message.
                break
            text += s + '\n'

        return text

    @staticmethod
    def _votals_text_full(ctx: commands.Context, votes: List[schema.Vote]) -> str:
        votals = Interview._preprocess_votals(ctx, votes)
        text = ''
        max_name_length = len(SERVER_LEFT_MSG)
        for candidate in votals:
            if len(str(candidate)) > max_name_length:
                max_name_length = len(str(candidate))
        for candidate in votals:
            s = candidate.full_str(max_name_length)
            if len(text) + len(s) > 1750:
                # Break if it's getting too long for a single message.
                break
            text += s + '\n'

        return text

    @commands.command()
    @commands.check(_server_active)
    # TODO: check for interview active
    async def vote(self, ctx: commands.Context, mentions: commands.Greedy[discord.Member]):
        """
        Vote for up to three nominees for the next interview.

        Voting rules:
        1. Cannot vote for yourself.
        2. Cannot vote for anyone who's been interviewed too recently.
        3. Cannot vote if you've joined the server since the start of the last interview.
        4. Cannot vote for bots, excepting HaruBot.
        5. Cannot vote while interviews are disabled.
        6. Cannot vote for people who are opted out.
        """
        # TODO: check votes for legality oh no
        #  like, lots to do.

        session = session_maker()
        old_votes = session.query(Vote).filter_by(server_id=ctx.guild.id, voter_id=ctx.author.id).all()
        for vote in old_votes:
            session.delete(vote)
        votes = []
        for mention in mentions:
            votes.append(Vote(server_id=ctx.guild.id, voter_id=ctx.author.id,
                              candidate_id=mention.id, timestamp=datetime.utcnow()))
        session.add_all(votes)
        session.commit()
        await ctx.message.add_reaction(self.bot.greentick)

    @commands.command()
    @commands.check(_server_active)
    # TODO: check for interview active
    async def unvote(self, ctx: commands.Context):
        """
        Delete your current votes.
        """
        session = session_maker()
        session.query(Vote).filter_by(server_id=ctx.guild.id, voter_id=ctx.author.id).delete()
        session.commit()
        await ctx.message.add_reaction(self.bot.greentick)

    @commands.command()
    @commands.check(_server_active)
    async def votes(self, ctx: commands.Context):
        """
        Check who you're voting for.
        """
        session = session_maker()
        votes = session.query(Vote).filter_by(server_id=ctx.guild.id, voter_id=ctx.author.id).all()
        member_votes = [ctx.guild.get_member(vote.candidate_id) for vote in votes]

        response = self._votes_footer(member_votes, prefix=ctx.bot.default_command_prefix)
        await ctx.send(response)

    @commands.command()
    @commands.check(_server_active)
    async def votals(self, ctx: commands.Context, flag: Optional[str]):
        """
        View current vote standings.

        Use the --full flag to view who's voting for each candidate.
        """
        session = session_maker()
        votes = session.query(Vote).filter_by(server_id=ctx.guild.id).all()

        # Filter only the invoker's own votes when generating the footer
        own_votes = [ctx.guild.get_member(vote.candidate_id) for vote in votes if vote.voter_id == ctx.author.id]
        footer = self._votes_footer(own_votes, prefix=ctx.bot.default_command_prefix)

        if flag is not None and '-f' in flag:
            # Do full votals.
            block_text = Interview._votals_text_full(ctx, votes)
        else:
            # Do basic votals.
            block_text = Interview._votals_text_basic(ctx, votes)

        reply = f'**__Votals__**```ini\n{block_text}```{footer}\n'

        await ctx.send(reply)

    @commands.group('opt', invoke_without_command=True)
    @commands.check(_server_active)
    async def opt(self, ctx: commands.Context):
        """
        Manage opting into or out of interview voting.
        """
        await ctx.send('Opt into or out of interview voting. '
                       f'Use `{ctx.bot.default_command_prefix}help opt` for more info.')

    @opt.command(name='out')
    @commands.check(_server_active)
    # TODO: check for interview active
    async def opt_out(self, ctx: commands.Context):
        """
        Opt out of voting.

        When opting out, all votes for you are deleted.
        """
        session = session_maker()
        status = session.query(schema.OptOut).filter_by(server_id=ctx.guild.id, opt_id=ctx.author.id).one_or_none()
        if status is None:
            optout = schema.OptOut(server_id=ctx.guild.id, opt_id=ctx.author.id)
            session.add(optout)

            # null votes currently on this user
            session.query(schema.Vote).filter_by(server_id=ctx.guild.id, candidate_id=ctx.author.id).delete()

            session.commit()
            await ctx.message.add_reaction(ctx.bot.greentick)
            return
        await ctx.send('You are already opted out of interviews.')
        await ctx.message.add_reaction(ctx.bot.redtick)

    @opt.command(name='in')
    @commands.check(_server_active)
    # TODO: check for interview active
    async def opt_in(self, ctx: commands.Context):
        """
        Opt into voting.
        """
        session = session_maker()
        status = session.query(schema.OptOut).filter_by(server_id=ctx.guild.id, opt_id=ctx.author.id).one_or_none()
        if status is None:
            await ctx.send('You are already opted into interviews.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        session.delete(status)
        session.commit()
        await ctx.message.add_reaction(ctx.bot.greentick)
        return

    @opt.command(name='list')
    @commands.check(_server_active)
    async def opt_list(self, ctx: commands.Context):
        """
        Check who's opted out of interview voting.
        """
        # TODO: yes.
        pass

    # TODO: eliminate before release
    @commands.command()
    @commands.is_owner()
    async def dbtest(self, ctx: commands.Context):
        # TODO: for some reason the foreign key constraint isn't enforced.
        session = session_maker()
        status = session.query(schema.OptOut).filter_by(server_id=ctx.guild.id, opt_id=ctx.author.id).one_or_none()
        print(status.server.id)

    # TODO: eliminate before release
    @commands.command()
    @commands.is_owner()
    async def gstest(self, ctx: commands.Context):
        session = session_maker()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()
        worksheet = self.connection.get_sheet(server.sheet_name)
        await ctx.send(f'sheet1 is {worksheet.sheet1.title}')


# TODO: eliminate before release
@commands.command()
@commands.is_owner()
async def ivembed(ctx: commands.Context):
    me = ctx.guild.get_member(100165629373337600)
    charmander = ctx.bot.get_user(139085841581473792)
    link = 'https://discord.com/channels/328399532368855041/508588908829736970/770767582244503622'
    question = 'who can we get to fill in the last **2 slots** for s5, currently we have: like nobody'
    answer = 'good question idk, just get eli...eson to run 3 games in one season'

    em = blank_answer_embed(me, charmander)
    q_title = 'Question #1'
    q_a = (
        f'[> {question}\n'
        f'> more stuff]({link})\n'
        f'{answer}'
    )
    em.add_field(name=q_title, value=q_a, inline=False)
    await ctx.send(embed=em)


def setup(bot: commands.Bot):
    bot.add_cog(Interview(bot))

    bot.add_command(ivembed)
