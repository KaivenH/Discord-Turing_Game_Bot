from __future__ import annotations
import discord

from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import random


from dataclasses import dataclass, field
from typing import Optional, List, Tuple

from openai import OpenAI

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL")

COMMAND_PREFIX = "!"
MAX_ROUNDS = 10
HUMAN_REPLY_TIMEOUT = 180  # seconds to wait for a human reply each round
DEFAULT_REPLY_CHANNEL_NAME = "reply-channel"  # used if not specified

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
current_game: Optional[Game] = None
oai_client: Optional[OpenAI] = None

@bot.event
async def on_ready():

    try:
        from openai import OpenAI
        global oai_client
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        # oai_client = OpenAI()
        print("successfully connected to OpenAI")
    except Exception as e:
        oai_client = None
        print("OpenAI client not initialized:", e)

@bot.event
async def on_member_join(member):
    await member.send(f"Welcome {member.name}")

# Message Events

def openai_answer(game: Game, text):
    response = oai_client.responses.create(
        model=OPENAI_MODEL,
        instructions="You are a coding assistant that talks like a pirate.",
        input=text,
    )
    return response.output_text

@bot.event
async def on_message(message: discord.Message):
    global current_game
    game: Game = current_game
    # Always let commands process first
    await bot.process_commands(message)

    # Ignore bot messages and DMs
    if message.author.bot or not message.guild:
        return

    # If it looks like a command, ignore it here (commands already handled)
    if game.is_command_message(message):
        return

    # If message is in a game channel and authored by the interrogator, treat as a question
    if game and message.author.id == game.interrogator_id and message.channel.id == game.game_channel_id:
        await handle_question(message, game)


async def handle_question(message: discord.Message, game: Game):
    async with game.lock:
        if not game:
            return
        if len(game.rounds) >= MAX_ROUNDS:
            await message.channel.send("Maximum rounds reached. Make a guess with `!guess 1` or `!guess 2`.")
            return

        round_number = len(game.rounds) + 1
        question = message.content.strip()
        this_round = Round(question=question)
        game.rounds.append(this_round)

        game_channel = message.guild.get_channel(game.game_channel_id)
        reply_channel = message.guild.get_channel(game.reply_channel_id)
        p1, p2 = game.label_players()
        ai_label = p1 if game.player_1_is_bot else p2
        human_label = p2 if game.player_1_is_bot else p1

        # Prompt human in reply-channel
        prompt_msg = await reply_channel.send(embed=game.nice_embed(
            f"Round {round_number}",
            [
                ("Question", question),
                ("How to reply", "Type your answer **as a normal message** below. "
                                 "First human message within the timeout will be used.")
            ]
        ))

        # Wait for the first human reply in the reply channel
        def human_check(m: discord.Message) -> bool:
            return (
                    m.channel.id == reply_channel.id
                    and not m.author.bot
                    and m.content.strip() != ""  # avoid empty
            )

        try:
            human_msg: discord.Message = await bot.wait_for("message", timeout=HUMAN_REPLY_TIMEOUT, check=human_check)
            human_answer = human_msg.content.strip()
            this_round.human_answer = human_answer
        except asyncio.TimeoutError:
            human_answer = "*No human reply received in time.*"
            this_round.human_answer = human_answer
            await reply_channel.send("⏱️ Timeout: no human reply received. Proceeding with AI only.")

        # Generate AI answer (after human, per your step order)
        try:
            async with message.channel.typing():
                bot_answer = await openai_answer(game, question)
                this_round.bot_answer = bot_answer
        except Exception as e:
            print(e)
        except asyncio.TimeoutError:
            print("Time ran out")
            bot_answer = "Stuff"
            this_round.bot_answer = bot_answer
                

        # Post both answers in consistent Person 1/2 order (identity fixed for whole game)
        # Map answers to labels:
        answers_by_label = {
            (p1 if game.player_1_is_bot else p2): bot_answer,
            (p2 if game.player_1_is_bot else p1): human_answer
        }

        embeder = discord.Embed(
            title=f"Round {round_number} Answers",
            description=f"**Question:** {question}",
            color=discord.Color.gold()
        )
        embeder.add_field(name=p1, value=answers_by_label[p1], inline=False)
        embeder.add_field(name=p2, value=answers_by_label[p2], inline=False)
        footer_note = f"Guess anytime with {COMMAND_PREFIX}guess 1 or {COMMAND_PREFIX}guess 2."
        if len(game.rounds) >= MAX_ROUNDS:
            footer_note += " (Max rounds reached.)"
        embeder.set_footer(text=footer_note)

        await game_channel.send(embed=embeder)

        # If we hit max rounds, prompt to guess
        if len(game.rounds) >= MAX_ROUNDS:
            await game_channel.send("That was the final round. Make your guess!")

#Bot Commands

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

@bot.command()
@commands.guild_only()
async def start_game(ctx: commands.Context):
    global current_game
    if current_game is not None:
        return await ctx.send(f"Game already started, use {COMMAND_PREFIX}stop_game to stop game")
    reply_channel = ctx.guild.get_channel(1418665946679214221)
    if ctx.channel.id == reply_channel.id:
        return await ctx.reply("You cannot start a game in the reply-channel")
    game_channel = ctx.guild.get_channel(1418347189909848084)
    if ctx.channel.id != game_channel.id:
        return await ctx.reply("You can only start a game in the game-channel")
    is_bot = random.choice([True, False])
    current_game = Game(
        guild_id=ctx.guild.id,
        game_channel_id=game_channel.id,
        reply_channel_id=reply_channel.id,
        interrogator_id=ctx.author.id,
        interrogator_name=ctx.author.name,
        player_1_is_bot=is_bot,
    )

    await ctx.send(embed=current_game.nice_embed(
        "Turing Game Started",
        [
            ("Interrogator", ctx.author.mention),
            ("Rounds", str(MAX_ROUNDS)),
            ("How to play", f"Ask a question in this channel. I will fetch a human reply from {reply_channel.mention} and generate the other reply."),
            ("Guess anytime", f"`{COMMAND_PREFIX}guess 1` or `{COMMAND_PREFIX}guess 2`"),
            ("Stop game", f"`{COMMAND_PREFIX}stop_game`"),
        ]
    ))

    return await reply_channel.send(embed=current_game.nice_embed(
        "Reply Channel Ready",
        [
            ("Instructions", "When the bot posts a **Round** prompt here, the **first** human message will be used as that round’s human answer."),
            ("Be Anonymous", "Don’t reveal who you are; keep it natural. Keep answers concise."),
        ]
    ))

@bot.command()
@commands.guild_only()
async def guess(ctx: commands.Context, player_choice: int):
    """Interrogator guesses 1 or 2."""
    global current_game
    game: Game = current_game
    if current_game is None:
        return await ctx.reply("No active game.")
    if ctx.channel.id != game.game_channel_id:
        return await ctx.reply("You can only guess in the game-channel")
    if ctx.author.id != game.interrogator_id:
        return await ctx.reply("Only the interrogator who started the game can guess.")


    correct_number = 1 if game.player_1_is_bot else 2

    if player_choice not in (1, 2):
        return await ctx.reply("Please guess `1` or `2`.")

    if player_choice == correct_number:
        current_game = None
        return await ctx.reply("Correct!")
    else:
        return await ctx.reply("Incorrect!")

@bot.command()
@commands.guild_only()
async def stop_game(ctx: commands.Context):
    """Manually end the current game."""
    global current_game
    current_game = None
    if ctx.channel.id != ctx.guild.get_channel(1418347189909848084):
        return await ctx.reply("You cannot stop a game outside the game-channel")
    if not current_game:
        return await ctx.reply("No active game in this channel.")

    return await ctx.send("Game stopped.")



@dataclass
class Round:
    question: str
    human_answer: Optional[str] = None
    bot_answer: Optional[str] = None
@dataclass
class Game:
    guild_id: int
    game_channel_id: int
    reply_channel_id: int
    interrogator_id: int
    interrogator_name: str
    player_1_is_bot: bool
    rounds: List[Round] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def label_players(self) -> Tuple[str, str]:
        if self.player_1_is_bot:
            bot_label = "Player 1"
            human_label = "Player 2"
        else:
            bot_label = "Player 2"
            human_label = "Player 1"
        return bot_label, human_label


    def build_message_history(self) -> List[dict]:
        msgs: List[dict] = [
            {
                "role": "system",
                "content": (
                    "You are one of two anonymous participants answering an interrogator in a Turing-style game. "
                    "Answer each question naturally, concisely (<= 70 words)"
                    "Do not reveal that you are an AI or not human"
                    "Keep a consistent tone from past responses"
                    "You can analyze and mimic past "
                )
            }
        ]

        # Replay previous rounds so the model has memory
        for idx, t in enumerate(self.rounds, start=1):
            # We fold prior context into 'user' messages the model sees
            prior = f"Round {idx} context:\nQuestion: {t.question}"
            if t.human_answer:
                prior += f"\nOther participant's answer: {t.human_answer}"
            if t.bot_answer:
                # What the model previously said
                msgs.append({"role": "user", "content": prior})
                msgs.append({"role": "assistant", "content": t.bot_answer})
            else:
                # If no prior AI answer (shouldn't happen mid-game), just include context
                msgs.append({"role": "user", "content": prior})

        return msgs

    async def prompt_openai(self, question: str) -> discord.Message:
        if oai_client is None:
            return discord.Message(data="openai client not initialized")
        msgs = self.build_message_history()
        msgs.append({"role": "user", "content": f"New question: {question}\nAnswer directly."})
        resp = await asyncio.to_thread(
            oai_client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.7,
            max_tokens=200,
        )
        return discord.Message(data=(resp.choices[0].message.content or "").strip())

    @staticmethod
    def nice_embed(title: str, fields: List[Tuple[str, str]]) -> discord.Embed:
        emb = discord.Embed(title=title, color=discord.Color.blurple())
        for name, value in fields:
            emb.add_field(name=name, value=value, inline=False)
        return emb
    @staticmethod
    def is_command_message(msg: discord.Message) -> bool:
        return msg.content.strip().startswith(COMMAND_PREFIX)
    @staticmethod
    def channel_is_reply(game: Game, channel: discord.abc.Messageable) -> bool:
        game: Game = game
        return getattr(channel, "id", None) == game.reply_channel_id

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)




# ---------------- Run ----------------
if __name__ == "__main__":

    if not os.getenv('DISCORD_TOKEN'):
        raise SystemExit("Missing DISCORD_TOKEN")
    if not OPENAI_API_KEY:
        print("Warning: OPENAI_API_KEY not set; the AI will post a placeholder message.")
    bot.run(os.getenv("DISCORD_TOKEN"))