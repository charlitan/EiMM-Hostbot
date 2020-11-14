import asyncio
import pprint
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Generator, Tuple

import discord
from discord.ext import commands
from sqlalchemy import create_engine, event, desc
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from gspread.exceptions import SpreadsheetNotFound

from cogs import interview_schema as schema
from core.bot import Bot
from utils import spreadsheet

_DEBUG_FLAG = False  # TODO: toggle to off

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
        channel = ctx.bot.get_channel(row['Channel ID'])  # type: discord.TextChannel
        message = await channel.fetch_message(row['Message ID'])
        return Question(
            # This is a bit dangerous, but should be fine! only the interviewee will be calling the answer method:
            interviewee=ctx.author,
            asker=ctx.guild.get_member(row['ID']),
            question=row['Question'],
            question_num=row['#'],
            # row['Server ID'],
            # row['Channel ID'],
            # row['Message ID'],
            message=message,  # replaced the prev three rows
            answer=row['Answer'],
            timestamp=datetime.utcfromtimestamp(row['POSIX Timestamp']),  # TODO: need to test this conversion
        )

    def to_row(self, ctx: commands.Context) -> list:
        """
        Convert this Question to a row for uploading to Sheets.
        """
        session = session_maker()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()
        if server is None:
            raise ValueError('No server found on this guild.')
        return [
            self.timestamp.strftime('%m/%d/%Y %H:%M:%S'),
            self.timestamp.timestamp(),
            str(self.asker),
            str(self.asker.id),
            self.question_num,
            self.question,
            '',  # no answer when uploading
            False,
            str(self.message.guild.id),
            str(self.message.channel.id),
            str(self.message.id),
        ]

    @staticmethod
    def upload_many(ctx: commands.Context, connection: spreadsheet.SheetConnection, questions: List['Question']):
        """
        Upload a list of Questions to a spreadsheet.

        This would normally be a normal method, but there's a separate command (append_rows vs append_row) for
        bulk upload, and we need to use that to dodge Sheets API rate limits.
        """
        session = session_maker()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()

        rows = [q.to_row(ctx) for q in questions]
        sheet = connection.get_sheet(server.sheet_name).sheet1
        sheet.append_rows(rows)

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


class InterviewEmbed(discord.Embed):
    @staticmethod
    def blank(interviewee: discord.Member, asker: Union[discord.Member, discord.User],
              avatar_url: str = None) -> 'InterviewEmbed':
        em = InterviewEmbed(
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
        # +100 length as a buffer for the metadata fields
        em.length = len(f"**{interviewee}**'s interview" + ' ' + f'Asked by {asker}') + len(asker.avatar_url) + 100
        return em


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


def message_link(server_id: int, channel_id: int, message_id: int) -> str:
    return f'https://discordapp.com/channels/{server_id}/{channel_id}/{message_id}'


def add_question(em: discord.Embed, question: Question, current_length: int) -> int:
    """
    Questions and answers are added to embed fields, each of which has a maximum of 1000 chars.
    Need to check (and possibly split up) each question and answer to make sure they don't overflow and break
    the embed. This is... somewhat frustrating.

    Return length of question/answer strings added, or error codes:
    -1 if the total is too long
    -2 if this one question is too long
    """
    text_length = 0

    question_text = question.question.replace('[', '\\[').replace(']', '\\]')
    question_lines = [line.strip() for line in question_text.split('\n')]

    # Sum of all text in all lines PLUS accounting for adding '> ' and '\n' to each line PLUS the question's answer:
    # When splitting up questions, assume 85 (round to 100) characters for the message link markdown, so you get 900
    # chars rather than 1000.
    if sum([len(line) for line in question_lines]) + len(question_lines) * 3 + len(question.answer) <= 900:
        formatted_question_text = f'[> {"> ".join(question_lines)}]({question.message.jump_url})'
        text_length = len(f'Question #{question.question_num}') + len(f'{formatted_question_text}\n{question.answer}')
        if text_length > 4800:
            return -2
        if current_length + text_length > 4800:
            return -1
        em.add_field(
            name=f'Question #{question.question_num}',
            value=f'{formatted_question_text}\n{question.answer}',
            inline=False,
        )
        return text_length

    # Need to split and add to separate fields:

    if len(question.question) + len(question.answer) > 4700:
        # I'm far too lazy to calculate exactly, but this should be safe enough
        return -2
    if current_length + len(question.question) + len(question.answer) > 4700:
        return -1

    question_chunk = '[> '
    question_chunks = []
    for word in question.question_words():
        if len(question_chunk) > 900:
            question_chunk += f']({question.message.jump_url})'
            question_chunks.append(question_chunk)
            question_chunk = '[> '
        word = word.replace("\n", "\n> ")
        question_chunk += f'{word}' + ' '
    question_chunk += f']({question.message.jump_url})'
    question_chunks.append(question_chunk)

    for i, chunk in enumerate(question_chunks):
        if len(question_chunks) > 1:
            name = f'Question #{question.question_num} [{i + 1}/{len(question_chunks)}]'
        else:
            name = f'Question #{question.question_num}'
        em.add_field(name=name, value=chunk, inline=False)
        text_length += len(name) + len(chunk)

    answer_chunk = ''
    answer_chunks = []
    for word in question.answer_words():
        if len(answer_chunk) > 950:
            answer_chunks.append(answer_chunk)
            answer_chunk = ''
        answer_chunk += word + ' '
    answer_chunks.append(answer_chunk)

    for i, chunk in enumerate(answer_chunks):
        if len(answer_chunks) > 1:
            name = f'Answer #{question.question_num} [{i + 1}/{len(answer_chunks)}]'
        else:
            name = f'Answer #{question.question_num}'
        em.add_field(name=name, value=chunk, inline=False)
        text_length += len(name) + len(chunk)

    return text_length


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

    Checked when voting, opting in or out, and asking questions.
    """
    session = session_maker()
    result = session.query(schema.Server).filter_by(id=ctx.guild.id, active=True).one_or_none()
    return result is not None


def _is_interviewee(ctx: commands.Context):
    """
    Command check to make sure the invoker is the interviewee.

    Checked when answering questions.
    """
    session = session_maker()
    result = session.query(schema.Interview).filter_by(server_id=ctx.guild.id, interviewee_id=ctx.author.id,
                                                       current=True).one_or_none()
    return result is not None


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
            # Note: Don't technically need this condition, but it adds a bit of clarity, so keeping it in for now.
            Path(DB_DIR).mkdir(exist_ok=True)
        engine = create_engine(f'sqlite:///{DB_FILE}')
        global session_maker
        session_maker = sessionmaker(bind=engine)

        schema.Base.metadata.create_all(engine)

    def new_interview(self):
        # TODO: yes.
        #  i don't actually think this is useful?
        pass

    # == Helper methods ==

    @staticmethod
    def _generate_embeds(interviewee: discord.Member, questions: List[Question],
                         avatar_url=None) -> Generator[Union[discord.Embed, Question], None, None]:
        """
        Generate discord.Embeds to be posted from a list of Questions.
        """

        def finalize(final_embed: discord.Embed):
            # TODO: add footer but idk what to do with it
            return final_embed

        last_asker = None  # type: Optional[discord.Member]
        length = 0
        em = None  # type: Optional[discord.Embed]
        for question in questions:
            if last_asker != question.asker:
                if em is not None and len(em.fields) > 0:
                    yield finalize(em)
                # make a new embed
                em = InterviewEmbed.blank(interviewee, question.asker,
                                          avatar_url=avatar_url)  # TODO: update avatar url?
                length = 0
            # TODO: add question/answer fields
            added_length = add_question(em, question, length)
            if added_length == -1:
                # question wasn't added, yield and retry
                if len(em.fields) > 0:
                    yield finalize(em)
                em = InterviewEmbed.blank(interviewee, question.asker,
                                          avatar_url=avatar_url)  # TODO: update avatar url?
            if added_length == -2:
                # question cannot be added, yield an error
                yield question
            length += added_length
            # TODO: update answered questions per asker? or whatever it is?
            last_asker = question.asker
        if em is not None:
            if len(em.fields) > 0:
                yield finalize(em)

    def _reset_meta(self, server: discord.Guild):
        """
        Set up the meta entry for a new interview.
        """
        # TODO: yes. also this probably needs more arguments. it may not even want to exist.
        pass

    # == Setup ==

    @commands.group(invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def iv(self, ctx: commands.Context):
        """
        # TODO: Write actual instructions and info for this group here.
        """
        # TODO: yes.
        #  document command group
        #  TODO: write instructions on setting up on a new server
        if _server_active(ctx):
            await ctx.send(f'Interviews are currently set up for {ctx.guild}; use '
                           f'`{ctx.bot.default_command_prefix}help Interview` for more info.')
        else:
            await ctx.send(f'Interviews are not currently set up for {ctx.guild}; use '
                           f'`{ctx.bot.default_command_prefix}help iv setup` for more info.')

    @iv.command(name='setup')
    @commands.has_permissions(administrator=True)
    async def iv_setup(self, ctx: commands.Context, answers: discord.TextChannel,
                       backstage: discord.TextChannel, sheet_name: str,
                       default_question: str = 'Make sure to write an intro on stage before you start '
                                               'answering questions!'):
        """
        Set up the current server for interviews.

        Answer channel is where where answers to questions will be posted, backstage is a private space for
        the bot to be controlled, sheet_name is the URL of your interview sheet.
        If your sheet name is multiple words, enclose it in double quotes, e.g., "sheet name".
        Sheet names must be unique, first-come-first-served.

        Copy the sheet template from TODO: add a link here.
        """
        # TODO: yes.
        #  setup server + channels for interview
        #  update all databases

        session = session_maker()

        existing_server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()
        if existing_server is not None:
            await ctx.send('This server is already set up for interviews.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return

        existing_server = session.query(schema.Server).filter_by(sheet_name=sheet_name).one_or_none()
        if existing_server is not None:
            await ctx.send(f'A sheet with the name `{sheet_name}` has already been registered, '
                           'please use a different one.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return

        try:
            sheet = self.connection.get_sheet(sheet_name)
        except SpreadsheetNotFound:
            # TODO: Figure out a way to publicize and pass on the bot account email.
            await ctx.send(f"Spreadsheet `{sheet_name}` cannot be found, make sure it's been shared with the bot "
                           "account and try again.")
            await ctx.message.add_reaction(ctx.bot.redtick)
            return

        server = schema.Server(
            id=ctx.guild.id,
            sheet_name=sheet_name,
            default_question=default_question,
            answer_channel=answers.id,
            back_channel=backstage.id,
            limit=datetime.utcfromtimestamp(0),
            reinterviews_allowed=False,
            active=False,
        )
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
        #  set the old interview row to be not-current
        #  upload the interviewee's current avatar to make it not break in the future?
        #  do we want the email to be private? not sure yet
        #  add new metadata row (maybe other databases?)

        # set the old interview row to be not-current
        session = session_maker()
        old_interview = session.query(schema.Interview).filter_by(server_id=ctx.guild.id,
                                                                  current=True).one_or_none()  # type: schema.Interview
        if old_interview is not None:
            # if old_interview doesn't exist that just means it's the first interview!
            old_interview.current = False

        timestamp = datetime.utcnow()
        sheet_name = f'{interviewee.name} [{interviewee.id}]-{timestamp.timestamp()}'

        # TODO: create new Interview

        new_interview = schema.Interview(
            server_id=ctx.guild.id,
            interviewee_id=interviewee.id,
            start_time=timestamp,
            sheet_name=sheet_name,
            questions_asked=0,
            questions_answered=0,
            current=True,
        )
        session.add(new_interview)

        # TODO: commit

        session.commit()

        # TODO: make & reset a new sheet

        old_sheet = self.connection.get_sheet(new_interview.server.sheet_name).sheet1
        new_sheet = old_sheet.duplicate(
            insert_sheet_index=0,
            new_sheet_name=sheet_name
        )
        new_sheet.insert_row(
            [
                timestamp.strftime('%m/%d/%Y %H:%M:%S'),
                timestamp.timestamp(),
                str(ctx.bot.user),
                str(ctx.bot.user.id),
                1,
                new_interview.server.default_question,  # TODO: replace this with something else
                '',
                False,
                str(ctx.guild.id),
                str(ctx.channel.id),
                str(ctx.message.id),
            ],
            index=2,
        )
        new_sheet.resize(rows=2)

        # TODO: share sheet with new person? maybe
        #  unsure if this is wanted
        ...

        # TODO: post the votals, etc. info to stage
        channel = ctx.guild.get_channel(new_interview.server.answer_channel)
        await self._votals_in_channel(ctx, flag=None, channel=channel)

        # TODO: clear votes
        session.query(schema.Vote).filter_by(server_id=ctx.guild.id).delete()

        # TODO: reply to message and/or greentick
        await ctx.message.add_reaction(ctx.bot.greentick)
        await asyncio.sleep(2)
        await ctx.send(f'{ctx.author.mention}, make sure to update the table of contents!')

    # TODO (maybe): Add methods to change the answer/backstage channels.

    @iv.command(name='disable')
    @commands.has_permissions(administrator=True)
    @commands.check(_server_active)
    async def iv_disable(self, ctx: commands.Context):
        """
        Disable voting and question asking for the current interview.
        """
        session = session_maker()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()
        if server is None:
            await ctx.send(f'Interviews are not set up for {ctx.guild}.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        if server.active is False:
            await ctx.send(f'Interviews are already disabled.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        server.active = False
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
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()
        if server is None:
            await ctx.send(f'Interviews are not set up for {ctx.guild}.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        if server.active is True:
            await ctx.send(f'Interviews are already enabled.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return
        server.active = True
        session.commit()
        await ctx.message.add_reaction(ctx.bot.greentick)

    @iv.command(name='stats')
    @commands.check(_server_active)
    async def iv_stats(self, ctx: commands.Context, member: Optional[discord.Member]):
        """
        View interview-related stats.

        If no user specified, view stats for the current interview.
        """
        session = session_maker()
        interview = session.query(schema.Interview).filter_by(server_id=ctx.guild.id,
                                                              current=True).one_or_none()  # type: schema.Interview
        interviewee = ctx.guild.get_member(interview.interviewee_id)
        past_interviews = session.query(schema.Interview).filter_by(
            server_id=ctx.guild.id, interviewee_id=interview.interviewee_id).all()  # type: List[schema.Interview]
        if member is None:
            # view general stats
            em = discord.Embed(
                title=f"{interviewee}'s interview",
                color=interviewee.color,
            )
            em.set_thumbnail(url=interviewee.avatar_url)
            if len(past_interviews) > 1:
                description = f"{interviewee}'s past interviews were:\n"
                for iv in past_interviews:
                    description += f'• {iv.start_time}: {iv.questions_asked} out of {iv.questions_asked}\n'
            else:
                description = f"This is {interviewee}'s first interview!"
            em.description = description
            em.add_field(name='Questions asked', value=str(interview.questions_asked))
            em.add_field(name='Questions answered', value=str(interview.questions_answered))
            em.set_footer(text=f'Interview started on {interview.start_time}')
            await ctx.send(embed=em)
            return

        em = discord.Embed(
            title=f'Interview stats for {member}',
            color=member.color,
        )
        em.set_thumbnail(url=member.avatar_url)
        current_qs = session.query(schema.Asker).filter_by(interview_id=interview.id, asker_id=member.id).one_or_none()
        if current_qs is None:
            # hasn't asked questions
            current_qs = 0
        else:
            current_qs = current_qs.num_questions
        ls_past_qs = session.query(schema.Asker).filter_by(asker_id=member.id).all()
        past_qs = 0
        for asker in ls_past_qs:
            past_qs += asker.num_questions

        em.add_field(name='Questions asked this interview', value=str(current_qs))
        em.add_field(name='Questions total', value=str(past_qs))
        await ctx.send(embed=em)

    # == Questions ==

    async def _ask_many(self, ctx: commands.Context, question_strs: List[str]):
        """
        Ask a bunch of questions at once. Or just one. Either way, use the batch upload command rather than
        doing it one at a time.

        TODO:
         split up questions
         create a bunch of Questions
         upload all questions to sheet
         upload question to sheet
         update Asker
         update Interview
        Note: I think these are all done?
        """
        session = session_maker()
        interview = session.query(schema.Interview).filter_by(current=True,
                                                              server_id=ctx.guild.id).one_or_none()  # type: Optional[schema.Interview]
        interviewee = ctx.guild.get_member(interview.interviewee_id)
        if interviewee is None:
            await ctx.send(f"Couldn't find server member `{interview.interviewee_id}`.")
            return

        asker_meta = None
        for asker in interview.askers:
            if asker.asker_id == ctx.author.id:
                asker_meta = asker
        if asker_meta is None:
            asker_meta = schema.Asker(interview_id=interview.id, asker_id=ctx.author.id, num_questions=0)
            session.add(asker_meta)

        questions = []
        for question_str in question_strs:
            q = Question(
                interviewee=interviewee,
                asker=ctx.author,
                question=question_str,
                question_num=asker_meta.num_questions + 1,
                message=ctx.message,
                # answer=,  # unfilled, obviously
                timestamp=datetime.utcnow(),
            )

            questions.append(q)

            asker_meta.num_questions += 1
            interview.questions_asked += 1

        Question.upload_many(ctx, self.connection, questions)

        session.commit()

        desc = '\n'.join(question_strs)[:1900] + '...' if len('\n'.join(question_strs)) > 1900 else '\n'.join(
            question_strs)[0:1900]
        em = discord.Embed(
            # TODO: fill in image
            title=f"**{interviewee}**'s interview",
            description=desc,
            color=ctx.bot.user.color,
            url='',  # TODO: fill in
        )
        em.set_author(
            name=f'New question from {ctx.author}',
            icon_url=ctx.author.avatar_url,
            url='',  # TODO: fill in
        )
        backstage = ctx.guild.get_channel(interview.server.back_channel)
        if backstage is None:
            await ctx.send(f'Backstage channel `{interview.server.back_channel}` not found for this server')
        await backstage.send(embed=em)

        await ctx.message.add_reaction(ctx.bot.greentick)

    @commands.command()
    @commands.check(_server_active)
    @commands.check(_interview_enabled)
    async def ask(self, ctx: commands.Context, *, question_str: str):
        """
        Submit a question for the current interview.
        """
        await self._ask_many(ctx, [question_str])

    @commands.command()
    @commands.check(_server_active)
    @commands.check(_interview_enabled)
    async def mask(self, ctx: commands.Context, *, questions_str: str):
        """
        Submit multiple questions for the current interview.

        Each question must be a single line, separated by linebreaks. If you want multi-line single questions,
        use the 'ask' command.
        """
        await self._ask_many(ctx, questions_str.split('\n'))

    # == Answers ==

    async def _channel_answer(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Command wrapped by Interview.answer() and Interview.preview(). Greedily dumps as many answered questions
        into embeds as possible, and posts them to the specified channel.
        """
        preview_flag = False
        if channel == ctx.channel:
            preview_flag = True

        session = session_maker()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()  # type: schema.Server
        interview = session.query(schema.Interview).filter_by(server_id=ctx.guild.id,
                                                              current=True).one_or_none()  # type: schema.Interview
        interviewee = ctx.guild.get_member(interview.interviewee_id)
        sheet = self.connection.get_sheet(server.sheet_name).sheet1

        rows = sheet.get_all_records()
        filtered_rows = []
        filtered_cells = []
        for i, row in enumerate(rows):
            if row['Answer'] is not None and row['Answer'] != '' and row['Posted?'] == 'FALSE':
                filtered_rows.append(row)
                filtered_cells.append({
                    'range': f'H{i + 2}',
                    'values': [[True]]
                })

        print('\n=== raw rows ===\n')
        pprint.pprint(len(rows))
        print(f'\n=== filtered ({len(filtered_rows)}) ===\n')
        pprint.pprint(filtered_rows)

        questions = [await Question.from_row(ctx, row) for row in filtered_rows]

        if len(questions) == 0:
            await ctx.send('No new questions to be answered.')
            return

        for embed in Interview._generate_embeds(interviewee=interviewee, questions=questions):
            if type(embed) is Question:
                # question was too long
                await channel.send(f"Question #{embed.question_num} or its answer from {embed.asker} was too long "
                                   f"to embed, please split it up and answer it on your own.")
            else:
                await channel.send(embed=embed)
            # TODO check if answer too long

        if preview_flag is True:
            return

        # Update sheet and metadata if not previewing:
        sheet.batch_update(filtered_cells)
        interview.questions_answered += len(questions)
        session.commit()

    @commands.command()
    @commands.check(_server_active)
    @commands.check(_is_interviewee)
    async def answer(self, ctx: commands.Context):
        """
        Post all answers to questions that have not yet been posted.

        Questions posted in chronological order, grouped by asker. If an answer is too long to be posted,
        the interviewee may have to post it manually.
        # TODO: Add a flag to post strictly chronologically?
        """
        session = session_maker()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()  # type: schema.Server
        channel = ctx.guild.get_channel(server.answer_channel)

        await self._channel_answer(ctx, channel)

    @commands.command()
    @commands.check(_server_active)
    @commands.check(_is_interviewee)
    async def preview(self, ctx: commands.Context):
        """
        Preview answers, visible in the backstage channel.
        """
        await self._channel_answer(ctx, ctx.channel)

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
                text += '...\n'
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
                max_name_length = len(str(candidate.candidate))
        for candidate in votals:
            s = candidate.full_str(max_name_length)
            if len(text) + len(s) > 1750:
                # Break if it's getting too long for a single message.
                text += '...\n'
                break
            text += s + '\n'

        return text

    @commands.command()
    @commands.check(_server_active)
    @commands.check(_interview_enabled)
    async def vote(self, ctx: commands.Context, mentions: commands.Greedy[discord.Member]):
        """
        Vote for up to three nominees for the next interview.

        Voting rules:
        1. Cannot vote while interviews are disabled.
        2. Cannot vote for >3 people.
        3. Cannot vote for people who are opted out.
        4. Cannot vote for anyone who's been interviewed too recently.
        5. Cannot vote if you've joined the server since the start of the last interview.
        6. Cannot vote for bots, excepting HaruBot.
        7. Cannot vote for yourself.

        Rules are checked in order, so if you vote for five people, but the first three are illegal votes,
        none of your votes will count.
        """

        session = session_maker()
        iv_meta = session.query(schema.Interview).filter_by(server_id=ctx.guild.id, current=True).one_or_none()
        server = session.query(schema.Server).filter_by(id=ctx.guild.id).one_or_none()  # type: schema.Server

        # TODO: check votes for legality oh no
        #  like, lots to do.

        class VoteError:
            def __init__(self):
                self.opt_outs = []
                self.too_recent = []
                self.bots = []
                self.self = False
                self.too_many = []

            @property
            def is_error(self):
                return (len(self.opt_outs) > 0 or
                        len(self.too_recent) > 0 or
                        len(self.bots) > 0 or
                        len(self.too_many) > 0 or
                        self.self is True)

            async def send_errors(self):
                if not self.is_error:
                    return
                reply = 'The following votes were ignored:\n'
                if len(self.opt_outs) > 0:
                    reply += '• Opted out:' + ', '.join([f'`{v}`' for v in self.opt_outs]) + '.\n'
                if len(self.too_recent) > 0:
                    reply += '• Interviewed too recently: ' + ', '.join([f'`{v}`' for v in self.too_recent]) + '.\n'
                if len(self.bots) > 0:
                    reply += ('• As much as I would love to usher in the **Rᴏʙᴏᴛ Rᴇᴠᴏʟᴜᴛɪᴏɴ**, you cannot vote for '
                              'bots such as ' + ', '.join([f'`{v}`' for v in self.bots]) + '.\n')
                    bottag = discord.utils.get(ctx.bot.emojis, name='bottag')
                    if bottag:
                        await ctx.message.add_reaction(bottag)
                if self.self:
                    reply += '• Your **anti-town** self vote.\n'
                if len(self.too_many) > 0:
                    reply += ('• ' + ', '.join([f'`{v}`' for v in self.too_many]) + ' exceeded your limit of '
                                                                                    'three (3) votes.\n')
                await ctx.send(reply)

        vote_error = VoteError()

        # 1. Cannot vote while interviews are disabled.  (return immediately)
        if not _interview_enabled(ctx):
            await ctx.send('Voting is currently **closed**; please wait for the next round to begin.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return

        # 2. Cannot vote for >3 people.
        if len(mentions) > 3:
            # await ctx.send('Vote for no more than three candidates. Additional votes are ignored.')
            # await ctx.message.add_reaction(ctx.bot.redtick)
            vote_error.too_many = mentions[3:]
            mentions = mentions[:3]
        elif len(mentions) < 1:
            await ctx.send('Vote at least one candidate.')
            await ctx.message.add_reaction(ctx.bot.redtick)
            return

        # 3. Cannot vote for people who are opted out.
        for mention in mentions:
            opted_out = session.query(schema.OptOut).filter_by(server_id=ctx.guild.id, opt_id=mention.id).one_or_none()
            if opted_out is not None:
                vote_error.opt_outs.append(mention)
        for mention in vote_error.opt_outs:
            mentions.remove(mention)

        # 4. Cannot vote for anyone who's been interviewed too recently.
        for mention in mentions:
            old = session.query(schema.Interview).filter_by(server_id=ctx.guild.id, interviewee_id=mention.id).order_by(
                desc('start_time')).first()  # type: schema.Interview
            if old and old.start_time > server.limit:
                vote_error.too_recent.append(mention)
        for mention in vote_error.too_recent:
            mentions.remove(mention)

        # 5. Cannot vote if you've joined the server since the start of the last interview.  (return immediately)
        if ctx.author.joined_at > iv_meta.start_time:
            await ctx.send(f"Don't just rejoin servers only to vote, {ctx.author}, have some respect.")
            await ctx.message.add_reaction(ctx.bot.redtick)
            return

        # 6. Cannot vote for bots, excepting HaruBot.
        for mention in mentions:
            if mention.bot:
                vote_error.bots.append(mention)
        for mention in vote_error.bots:
            mentions.remove(mention)

        # 7. Cannot vote for yourself.
        if ctx.author in mentions:
            vote_error.self = True
            mentions.remove(ctx.author)

        old_votes = session.query(schema.Vote).filter_by(server_id=ctx.guild.id, voter_id=ctx.author.id).all()
        for vote in old_votes:
            session.delete(vote)
        votes = []
        for mention in mentions:
            votes.append(schema.Vote(server_id=ctx.guild.id, voter_id=ctx.author.id,
                                     candidate_id=mention.id, timestamp=datetime.utcnow()))
        session.add_all(votes)
        session.commit()

        if vote_error.is_error:
            await ctx.message.add_reaction(self.bot.redtick)
            await vote_error.send_errors()
        if len(mentions) > 0:
            # some votes went through
            await ctx.message.add_reaction(self.bot.greentick)

    @commands.command()
    @commands.check(_server_active)
    @commands.check(_interview_enabled)
    async def unvote(self, ctx: commands.Context):
        """
        Delete your current votes.
        """
        session = session_maker()
        session.query(schema.Vote).filter_by(server_id=ctx.guild.id, voter_id=ctx.author.id).delete()
        session.commit()
        await ctx.message.add_reaction(self.bot.greentick)

    @commands.command()
    @commands.check(_server_active)
    async def votes(self, ctx: commands.Context):
        """
        Check who you're voting for.
        """
        session = session_maker()
        votes = session.query(schema.Vote).filter_by(server_id=ctx.guild.id, voter_id=ctx.author.id).all()
        member_votes = [ctx.guild.get_member(vote.candidate_id) for vote in votes]

        response = self._votes_footer(member_votes, prefix=ctx.bot.default_command_prefix)
        await ctx.send(response)

    async def _votals_in_channel(self, ctx: commands.Context, flag: Optional[str] = None,
                                 channel: Optional[discord.TextChannel] = None):
        """
        The only reason this isn't votals() is because it also gets called by iv_next(), but that wants to place
        the votals reply in a different channel.
        """
        session = session_maker()
        votes = session.query(schema.Vote).filter_by(server_id=ctx.guild.id).all()

        # Filter only the invoker's own votes when generating the footer
        own_votes = [ctx.guild.get_member(vote.candidate_id) for vote in votes if vote.voter_id == ctx.author.id]
        footer = self._votes_footer(own_votes, prefix=ctx.bot.default_command_prefix)

        if flag is not None and '-f' in flag:
            # Do full votals.
            block_text = Interview._votals_text_full(ctx, votes)
            if block_text == '':
                block_text = """
                        _  /)
                       mo / )
                       |/)\)
                        /\_
                        \__|=
                       (    )
                       __)(__
                 _____/      \\_____
                |  _     ___   _   ||
                | | \     |   | \  ||
                | |  |    |   |  | ||
                | |_/     |   |_/  ||
                | | \     |   |    ||
                | |  \    |   |    ||
                | |   \. _|_. | .  ||
                |                  ||
                |  PenguinBot3000  ||
                |   2016 - 2020    ||
                |                  ||
        *       | *   **    * **   |**      **
         \))ejm97/.,(//,,..,,\||(,,.,\\,.((//"""

        else:
            # Do basic votals.
            block_text = Interview._votals_text_basic(ctx, votes)
            if block_text == '':
                block_text = ' '  # avoid the ini syntax highlighting breaking

        reply = f'**__Votals__**```ini\n{block_text}```{footer}\n'

        await channel.send(reply)

    @commands.command()
    @commands.check(_server_active)
    async def votals(self, ctx: commands.Context, flag: Optional[str] = None):
        """
        View current vote standings.

        Use the --full flag to view who's voting for each candidate.
        """
        await self._votals_in_channel(ctx, flag=flag, channel=ctx.channel)

    @commands.group(invoke_without_command=True)
    @commands.check(_server_active)
    async def opt(self, ctx: commands.Context):
        """
        Manage opting into or out of interview voting.
        """
        await ctx.send('Opt into or out of interview voting. '
                       f'Use `{ctx.bot.default_command_prefix}help opt` for more info.')

    @opt.command(name='out')
    @commands.check(_server_active)
    @commands.check(_interview_enabled)
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
    @commands.check(_interview_enabled)
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
        TODO: Check who's opted out of interview voting.
        """
        # TODO: yes.
        pass


# TODO: eliminate before release
@commands.command()
@commands.is_owner()
async def ivembed(ctx: commands.Context):
    me = ctx.guild.get_member(100165629373337600)
    charmander = ctx.bot.get_user(129700693329051648)
    link = 'https://discord.com/channels/328399532368855041/328399532368855041/773778440208908298'
    question = 'you have a magic the gathering bot, but the interview bot is broke. seems legit'
    answer = 'every time u complain about it i delay release another day :ok_hand:'

    em = InterviewEmbed.blank(me, charmander)
    q_title = 'Question #1'
    q_a = (
        f'[> {question}]({link})\n'
        f'{answer}'
    )
    em.add_field(name=q_title, value=q_a, inline=False)
    await ctx.send(embed=em)


def setup(bot: commands.Bot):
    bot.add_cog(Interview(bot))

    bot.add_command(ivembed)
