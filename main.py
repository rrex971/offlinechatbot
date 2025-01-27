# coding=utf-8

# TODO: clean up code in general
#       utilize DMs (maybe)
#       make decorators for osu arguments
#       implement rate limit for unverified bot
#       save bans to database so they can be made with a command and also check via user id
#       try communicating with the mysql server more instead of saving data locally
from dotenv import load_dotenv
load_dotenv()

import websockets
import requests
import html
import os
import sys
from get_top_players import Client
from sql import Database
from emotes import EmoteRequester
from helper_objects import *
from context import *
from util import *
from constants import *
from client import Bot as CommunicationClient
from osu import AsynchronousClient, GameModeStr, Score, Mods
from osu_diff_calc import OsuPerformanceCalculator, OsuDifficultyAttributes, OsuScoreAttributes
from pytz import timezone, all_timezones
from copy import deepcopy
from aiohttp import client_exceptions


TESTING = "--test" in sys.argv

if not TESTING or not os.path.exists("data/top players (200).json"):
    Client().run()  # Update top player json file
if not TESTING or not os.path.exists("data/anime.json"):
    import get_popular_anime  # Update popular anime json file
if not TESTING or not os.path.exists("data/azur_lane.json"):
    from azur_lane import download_azur_lane_ship_names
    download_azur_lane_ship_names()

command_manager = CommandManager()


class Bot:
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    username = "sheppsubot"
    oauth = os.getenv("OAUTH")
    uri = "ws://irc-ws.chat.twitch.tv:80"

    restarts = 0

    def __init__(self, cm):
        self.database = Database()
        channels_to_run_in = [ChannelConfig("sheppsu", 156710598)] if TESTING else self.database.get_channels()

        self.cm = cm
        self.cm.init(self, channels_to_run_in)

        self.ws = None
        self.comm_client = CommunicationClient(self)
        self.running = False
        self.loop = asyncio.get_event_loop()
        self.last_message = {}
        self.own_state = None
        self.irc_command_handlers = {
            ContextType.CONNECTED: self.on_running,
            ContextType.PRIVMSG: self.on_message,
            ContextType.USERSTATE: self.on_user_state,
            ContextType.JOIN: self.on_join,
        }

        # Is ed offline or not
        self.offlines = {channel: True for channel in self.cm.channels}

        # Twitch api stuff
        self.access_token, self.expire_time = self.get_access_token()
        self.expire_time += perf_counter()

        # Message related variables
        self.message_send_cd = 1.5
        self.message_locks = {}

        # Save/load data from files or to/from database
        self.pity = {}
        self.userdata = {}
        self.top_players = []
        self.top_maps = []
        self.word_list = []
        self.facts = []
        self.pull_options = {}
        self.afk = {}
        self.all_words = []
        self.anime = []
        self.osu_data = {}
        self.user_id_cache = {}
        self.timezones = {}
        self.azur_lane = []

        # Load save data
        self.load_data()
        self.genshin = self.pull_options["3"] + self.pull_options["4"] + self.pull_options["5"]

        # Guess the number
        self.number = random.randint(1, 1000)

        # Trivia
        self.trivia_helpers = {}

        self.scrambles = {
            "word": Scramble("word", lambda: random.choice(self.word_list), 1),
            "osu": Scramble("player name", lambda: random.choice(self.top_players), 0.8),
            "map": Scramble("map name", lambda: random.choice(self.top_maps), 1.3),
            "genshin": Scramble("genshin weap/char", lambda: random.choice(self.genshin), 0.7),
            "emote": Scramble("emote", lambda channel: random.choice(self.emotes[channel]).name, 0.7,
                              ScrambleHintType.EVERY_OTHER, True, ScrambleRewardType.LOGARITHM),
            "anime": Scramble("anime", lambda: random.choice(self.anime[:250]), 1.1),
            "al": Scramble("azurlane ship", lambda: random.choice(self.azur_lane), 0.9),
        }
        self.scramble_manager = ScrambleManager(self.scrambles)

        # emote stuff
        self.emote_requester = EmoteRequester(self.client_id, self.client_secret)
        self.emote_requester.http.set_access_token(self.access_token)
        self.emotes = {channel: sum(self.emote_requester.get_channel_emotes(channel), []) for channel in self.cm.channels}

        # Bomb party
        self.bomb_party_helper = BombParty()
        self.bomb_party_future = None
        self.exploding = False

        # Anime compare
        self.compare_helper = AnimeCompare(self.anime)
        self.anime_compare_future = {}

        # osu! stuff
        self.recent_score_cache = {}

        # timezone stuff
        self.tz_abbreviations = {}
        for name in all_timezones:
            tzone = timezone(name)
            for _, _, abbr in getattr(tzone, "_transition_info", [[None, None, datetime.now(tzone).tzname()]]):
                if abbr not in self.tz_abbreviations:
                    self.tz_abbreviations[abbr] = []
                if name in self.tz_abbreviations[abbr]:
                    continue
                self.tz_abbreviations[abbr].append(name)

        self.osu_client = AsynchronousClient.from_osu_credentials(
            os.getenv("OSU_USERNAME"), os.getenv("OSU_PASSWORD"),
            request_wait_time=0, limit_per_minute=1200
        )
        self.osu_client.http.use_lazer = False
        self.osu_lock = asyncio.Lock()

        self.message_buffer = []

    # Util

    def set_timed_event(self, wait, callback, *args, **kwargs):
        future = asyncio.run_coroutine_threadsafe(do_timed_event(wait, callback, *args, **kwargs), self.loop)
        future.add_done_callback(future_callback)
        return future

    async def create_periodic_message(self, channel, message, wait_time, offset):
        async def send_message():
            await self.send_message(channel, message)
            self.set_timed_event(wait_time, send_message)

        if offset == 0:
            await send_message()
        else:
            self.set_timed_event(offset, send_message)

    def get_wait_for_channel(self, channel):
        # TODO: make a check for if the bot is a moderator in the channel
        if channel == self.username or (self.own_state is not None and self.own_state.mod):
            return 0.3
        return 1.5

    def send_message_in_progress(self):
        for task in asyncio.all_tasks(self.loop):
            if id(task.get_coro().cr_code) == id(self.send_message.__code__):
                return True
        return False
    # File save/load

    def load_top_players(self):
        with open("data/top players (200).json", "r") as f:
            self.top_players = json.load(f)

    def load_top_maps(self):
        with open("data/top_maps.json", "r") as f:
            self.top_maps = json.load(f)

    def load_words(self):
        with open("data/words.json", "r") as f:
            self.word_list = json.load(f)

    def load_facts(self):
        with open("data/facts.json", "r") as f:
            self.facts = json.load(f)

    def load_all_words(self):
        with open("data/all_words.json", "r") as f:
            self.all_words = [word.lower() for word in json.load(f)]

    def load_anime(self):
        with open("data/anime.json", "r") as f:
            self.anime = json.load(f)

    def load_azur_lane(self):
        with open("data/azur_lane.json", "r") as f:
            self.azur_lane = json.load(f)

    def load_db_data(self):
        self.pity = self.database.get_pity()
        self.userdata = self.database.get_userdata()
        self.afk = self.database.get_afk()
        self.osu_data = self.database.get_osu_data()
        self.user_id_cache = {}
        for data in self.osu_data.values():
            self.user_id_cache[data["username"]] = data["user_id"]
        self.timezones = self.database.get_timezones()

    def reload_db_data(self):
        self.database.close()
        self.database.create_connection()
        self.load_db_data()

    async def reload_channels(self):
        if TESTING:
            return print("Cannot reload channels in testing mode")
        current_channels = set([channel.id for channel in self.cm.channels.values()])
        channels = self.database.get_channels()
        channel_ids = set([channel.id for channel in channels])
        leave_channels = current_channels - channel_ids
        join_channels = channel_ids - current_channels
        for channel in channels:
            if channel.id in leave_channels:
                await self.part(channel.name)
            elif channel.id in join_channels:
                await self.join(channel.name)
        self.cm.load_channels(channels)

    def load_genshin(self):
        with open("data/genshin.json", "r") as f:
            self.pull_options = json.load(f)

    def load_data(self):
        self.load_top_players()
        self.load_top_maps()
        self.load_words()
        self.load_facts()
        self.load_all_words()
        self.load_genshin()
        self.load_anime()
        self.load_azur_lane()
        self.load_db_data()

    def save_money(self, user):
        self.database.update_userdata(user, 'money', round(self.userdata[user]['money']))

    # Api request stuff
    # TODO: consider moving api stuff to its own class

    def load_top_plays(self):  # To be used in the future maybe
        resp = requests.get('https://osutrack-api.ameo.dev/bestplay?mode=0')
        resp.raise_for_status()
        top_plays = resp.json()

    def get_access_token(self):
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }
        resp = requests.post("https://id.twitch.tv/oauth2/token", params=params)
        resp.raise_for_status()
        resp = resp.json()
        return resp['access_token'], resp['expires_in']

    def get_stream_status(self, channel):
        try:
            params = {"query": channel, "first": 1}
            headers = {"Authorization": f"Bearer {self.access_token}", "Client-Id": self.client_id}
            resp = requests.get("https://api.twitch.tv/helix/search/channels", params=params, headers=headers)
            resp.raise_for_status()
            resp = resp.json()
            self.offlines[channel] = not resp['data'][0]['is_live']
        except:
            print(traceback.format_exc())
            self.offlines[channel] = False

    # Fundamental

    async def start(self):
        async with websockets.connect(self.uri) as ws:
            self.ws = ws
            self.running = True

            try:
                # Start up
                await self.connect()  # Connect to the irc server
                poll = asyncio.run_coroutine_threadsafe(self.poll(), self.loop)  # Begin polling for events sent by the server
                if not TESTING:
                    comm = asyncio.run_coroutine_threadsafe(self.comm_client.run(), self.loop)  # Start the client that communicates with remote clients

                # Running loop
                last_check = perf_counter() - 20
                last_ping = perf_counter() - 60*60  # 1 hour
                last_update = perf_counter() - 60
                comm_done = False
                while self.running:
                    await asyncio.sleep(1)  # Leave time for other threads to run

                    # Check if channels are live
                    if perf_counter() - last_check >= 20:
                        for channel in self.cm.channels.values():
                            if not channel.offlineonly:
                                continue
                            self.get_stream_status(channel.name)
                        last_check = perf_counter()

                    # Check if access token needs to be renewed
                    if perf_counter() >= self.expire_time:
                        self.access_token, self.expire_time = self.get_access_token()
                        self.expire_time += perf_counter()

                    # Ping database once an hour for keepalive
                    if perf_counter() - last_ping >= 60*60:
                        self.database.ping()

                    if perf_counter() - last_update >= 60*60:
                        import get_popular_anime  # Update popular anime json file
                        self.load_anime()

                    # Check if poll is no longer running, in which case, the bot is no longer running.
                    if poll.done():
                        print(poll.result())
                        self.running = False

                    if not TESTING and comm.done() and not comm_done:
                        comm_done = True
                        try:
                            print("Communication client finished")
                            print(comm.result())
                        except:
                            traceback.print_exc()

            except KeyboardInterrupt:
                pass
            except websockets.ConnectionClosedError as e:
                # Restart the bot
                print(e)
                if self.restarts < 5:
                    self.restarts += 1
                    print("Restarting bot...")
                    await self.start()
            except:
                print(traceback.format_exc())
        self.running = False

    async def connect(self):
        await self.ws.send(f"PASS {self.oauth}")
        print(f"> PASS {self.oauth}")
        await self.ws.send(f"NICK {self.username}")
        print(f"> NICK {self.username}")

    async def poll(self):
        while self.running:
            data = await self.ws.recv()
            print(f"< {data}")

            if data.startswith("PING"):
                await self.ws.send("PONG :tmi.twitch.tv")
                continue

            # Account for tags
            ctxs = Context(data)

            for ctx in ctxs:
                for cmd, handler in self.irc_command_handlers.items():
                    if ctx.type == cmd:
                        future = asyncio.run_coroutine_threadsafe(handler(ctx), self.loop)
                        future.add_done_callback(future_callback)

    async def join(self, channel):
        channel = channel.lower()
        await self.ws.send(f"JOIN #{channel}")
        print(f"> JOIN #{channel}")

    async def part(self, channel):
        await self.ws.send(f"PART #{channel}\r\n")
        print(f"< PART #{channel}\r\n")

    async def register_cap(self, *caps):
        caps = ' '.join([f'twitch.tv/{cap}' for cap in caps])
        await self.ws.send(f"CAP REQ :{caps}\r\n")
        print(f"< CAP REQ :{caps}\r\n")

    async def send_message(self, channel, message):
        message = message.strip()
        while (i := message.find("  ")) != -1:
            message = message[:i] + message[i+1:]

        # TODO: fix rate limit handling shit
        if channel in self.offlines and not self.offlines[channel]:
            return
        await self.message_locks[channel].acquire()
        messages = split_message(message)
        sent_messages = []
        for msg in messages:
            msg = msg + (" \U000e0000" if self.last_message[channel] == msg else "")
            await self.ws.send(f"PRIVMSG #{channel} :/me {msg}")
            self.last_message[channel] = msg
            print(f"> PRIVMSG #{channel} :/me {msg}")
            sent_messages.append(msg.strip())
            await asyncio.sleep(self.get_wait_for_channel(channel))  # Avoid going over ratelimits
        self.message_locks[channel].release()
        return sent_messages

    # IRC command handlers

    async def on_running(self, ctx):
        await self.register_cap("tags")
        await self.register_cap("commands")
        for channel in self.cm.channels:
            await self.join(channel)

    async def on_user_state(self, ctx: UserStateContext):
        if ctx.username == self.username:
            self.own_state = ctx

    async def on_join(self, ctx: JoinContext):
        self.message_locks[ctx.channel] = asyncio.Lock()
        self.last_message[ctx.channel] = ""
        self.emotes[ctx.channel] = sum(self.emote_requester.get_channel_emotes(ctx.channel), [])
        self.offlines[ctx.channel] = True
        self.recent_score_cache[ctx.channel] = {}
        self.trivia_helpers[ctx.channel] = TriviaHelper()

    async def on_message(self, ctx: MessageContext):
        if (ctx.channel in self.offlines and not self.offlines[ctx.channel]) or \
                ctx.user.username == self.username:
            return

        if ctx.message.lower().startswith("pogpega") and ctx.message.lower() != "pogpega":
            ctx.message = ctx.message[8:]

        ascii_message = "".join([char for char in ctx.message if char.isascii()]).strip()

        for scramble_type, scramble in self.scrambles.items():
            if scramble.in_progress(ctx.channel):
                await self.on_scramble(ctx, scramble_type)

        if ctx.user.username in self.anime_compare_future and self.anime_compare_future[ctx.user.username] is not None and \
                ascii_message.isdigit() and int(ascii_message.strip()) in range(1, 3):
            game = self.compare_helper.get_game(ctx.user.username)
            if game is not None:
                await self.on_anime_compare(ctx, game)

        elif self.trivia_helpers[ctx.channel].is_in_progress and ascii_message.isdigit() and \
                int(ascii_message.strip()) in range(1, 5):
            message = int(ascii_message)
            await self.on_answer(ctx, message)

        if self.bomb_party_helper.started:
            await self.on_bomb_party(ctx)

        await self.on_afk(ctx)

        if ctx.reply:
            ascii_message = " ".join(ascii_message.split()[1:])
            ctx.message = " ".join(ctx.message.split()[1:])

        if ascii_message.startswith("!"):
            command = ascii_message.split()[0].lower().replace("!", "")
            await self.cm(command, ctx)  # Automatically checks that the command exists

    # Commands

    @command_manager.command("pull", Cooldown(1, 2), aliases=["genshinpull"])
    async def pull(self, ctx):
        # TODO: Try and make this look more clean
        user = ctx.user.username
        if user not in self.pity:
            self.pity.update({user: {4: 0, 5: 0}})
            self.database.new_pity(user, 0, 0)
        pity = False
        self.pity[user][4] += 1
        self.pity[user][5] += 1
        if self.pity[user][4] == 10 and self.pity[user][5] != 90:
            pull = 4
            pity = True
        elif self.pity[user][5] == 90:
            pull = 5
            pity = True
        else:
            num = random.randint(1, 1000)
            pull = 3
            if num <= (300 - 20 * (self.pity[user][5] - 76) if self.pity[user][5] >= 76 else 6):
                pull = 5
            elif num <= 57:
                pull = 4
        await self.send_message(ctx.channel,
                                f"@{user} You pulled {random.choice(self.pull_options[str(pull)])} " +
                                ("\u2B50\u2B50\u2B50" if pull == 3 else '🌟' * pull) +
                                {3: ". 😔", 4: "! Pog", 5: "! PogYou"}[pull] +
                                ((" Rolls in: " + str(
                                    self.pity[user][pull] if not pity else {4: 10, 5: 90}[pull])) if pull != 3 else "")
                                )
        if pull == 5:
            self.pity[user][5] = 0
            self.pity[user][4] = 0
        elif pull == 4:
            self.pity[user][4] = 0
        self.database.save_pity(user, self.pity[user][4], self.pity[user][5])

    @command_manager.command("font")
    async def font(self, ctx):
        args = ctx.get_args()
        if len(args) < 2:
            return await self.send_message(ctx.channel, "Must provide a font name and characters to convert. Do !fonts to see a list of valid fonts.")

        font_name = args[0].lower()
        if font_name not in fonts:
            return await self.send_message(ctx.channel, f"{font_name} is not a valid font name.")

        await self.send_message(ctx.channel, "".join([fonts[font_name][layout.index(char)] if char in layout else char for char in " ".join(args[1:])]))

    @command_manager.command("fonts")
    async def fonts(self, ctx):
        await self.send_message(ctx.channel, f'Valid fonts: {", ".join(list(fonts.keys()))}.')

    @command_manager.command("guess", Cooldown(2, 3))
    async def guess(self, ctx):
        args = ctx.get_args()
        if len(args) < 1:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You must provide a number 1-1000 to guess with")

        if not args[0].isdigit():
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} That's not a valid number OuttaPocket Tssk")

        guess = int(args[0])

        if self.number == guess:
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} You got it PogYou")
            self.number = random.randint(1, 1000)
        else:
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} It's not {guess}. Try guessing " + (
                "higher" if guess < self.number else "lower") + ". veryPog")

    # TODO: consider putting trivia stuff in its own class
    @command_manager.command("trivia")
    async def trivia(self, ctx):
        if self.trivia_helpers[ctx.channel].is_in_progress:
            return

        args = ctx.get_args("ascii")
        question = self.trivia_helpers[ctx.channel].generate_question(args[0] if len(args) > 0 else None)
        if question is None:
            return await self.send_message(ctx.channel, "An error occurred when attempting to fetch the question...")
        await self.send_message(ctx.channel, question)

        self.trivia_helpers[ctx.channel].future = self.set_timed_event(20, self.on_trivia_finish, ctx.channel)

    @requires_gamba_data
    async def on_answer(self, ctx, answer):
        result = self.trivia_helpers[ctx.channel].check_guess(ctx, answer)
        if result is None:
            return
        message, amount = result
        if not self.trivia_helpers[ctx.channel].answer is None:
            self.trivia_helpers[ctx.channel].future.cancel()
        await self.send_message(ctx.channel, message)
        self.userdata[ctx.user.username]["money"] += amount
        self.save_money(ctx.user.username)

    async def on_trivia_finish(self, channel):
        self.trivia_helpers[channel].start_cancelling()
        await self.send_message(channel, "Time has run out for the trivia.")
        self.trivia_helpers[channel].finish_cancelling()

    @command_manager.command("slap")
    async def slap(self, ctx):
        args = ctx.get_args()
        if not args:
            return

        hit = random.choice((True, False))
        await self.send_message(ctx.channel,
                                f"{ctx.user.username} slapped {args[0]}! D:" if hit else f"{ctx.user.username} tried to slap {args[0]}, but they caught it! pepePoint")

    @command_manager.command("pity")
    async def pity(self, ctx):
        if ctx.user.username not in self.pity:
            return await self.send_message(ctx.channel, "You haven't rolled yet (from the time the bot started up).")
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} 4* pity in {10 - self.pity[ctx.user.username][4]} rolls; "
                                             f"5* pity in {90 - self.pity[ctx.user.username][5]} rolls.")

    @command_manager.command("scramble", fargs=["word"])
    @command_manager.command("scramble_osu", fargs=["osu"])
    @command_manager.command("scramble_map", fargs=["map"])
    @command_manager.command("scramble_emote", fargs=["emote"])
    @command_manager.command("scramble_genshin", fargs=["genshin"])
    @command_manager.command("scramble_anime", fargs=["anime"])
    @command_manager.command("scramble_al", fargs=["al"])
    async def scramble(self, ctx, scramble_type):
        if self.scramble_manager.in_progress(scramble_type, ctx.channel):
            return
        if scramble_type == "emote" and len(self.emotes[ctx.channel]) < 20:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Must have at least 20 emotes "
                                                        "in this channel to use the emote scramble.")

        scrambled_word = self.scramble_manager.get_scramble(scramble_type, ctx.channel)
        await self.send_message(ctx.channel, f"Unscramble this "
                                             f"{self.scramble_manager.get_scramble_name(scramble_type)}: "
                                             f"{scrambled_word.lower()}")
        future = self.set_timed_event(120, self.on_scramble_finish, ctx.channel, scramble_type)
        self.scramble_manager.pass_future(scramble_type, ctx.channel, future)

    async def on_scramble(self, ctx, scramble_type):
        money = self.scramble_manager.check_answer(scramble_type, ctx.channel, ctx.message)
        if money is None:
            return
        answer = self.scramble_manager.get_answer(scramble_type, ctx.channel)
        name = self.scramble_manager.get_scramble_name(scramble_type)
        self.scramble_manager.reset(scramble_type, ctx.channel)
        emotes = list(map(lambda e: e.name, self.emotes[ctx.channel]))
        await self.send_message(ctx.channel,
                                f"@{ctx.user.display_name} You got it right! "
                                f"{answer} was the "
                                f"{name}. "
                                f"{'Drake ' if 'Drake' in emotes else ''}"
                                f"You've won {money} Becky Bucks!")
        if ctx.user.username not in self.userdata:
            self.add_new_user(ctx.user.username, ctx.user_id)
        self.userdata[ctx.user.username]["money"] += money
        self.save_money(ctx.user.username)

    @command_manager.command("hint", fargs=["word"])
    @command_manager.command("hint_osu", fargs=["osu"])
    @command_manager.command("hint_map", fargs=["map"])
    @command_manager.command("hint_emote", fargs=["emote"])
    @command_manager.command("hint_genshin", fargs=["genshin"])
    @command_manager.command("hint_anime", fargs=["anime"])
    @command_manager.command("hint_al", fargs=["al"])
    async def hint(self, ctx, scramble_type):
        if not self.scramble_manager.in_progress(scramble_type, ctx.channel):
            return
        if not self.scramble_manager.hints_left(scramble_type, ctx.channel):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} There are no hints left bruh")
        await self.send_message(ctx.channel,
                                f"Here's a hint "
                                f"({self.scramble_manager.get_scramble_name(scramble_type)}): "
                                f"{self.scramble_manager.get_hint(scramble_type, ctx.channel).lower()}")

    async def on_scramble_finish(self, channel, scramble_type):
        answer = self.scramble_manager.get_answer(scramble_type, channel)
        name = self.scramble_manager.get_scramble_name(scramble_type)
        self.scramble_manager.reset(scramble_type, channel, cancel=False)
        await self.send_message(channel, f"Time is up! The {name} was {answer}")

    def add_new_user(self, user, user_id):
        self.userdata.update({user.username: {
            'money': 0,
            'settings': {
                'receive': True,
                "autoafk": True,
            },
            'userid': user_id,
        }})
        self.database.new_user(user.username, user_id)

    # @command_manager.command("collect")
    @requires_gamba_data
    async def collect(self, ctx):
        money = random.randint(10, 100)
        self.userdata[ctx.user.username]["money"] += money
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} You collected {money} Becky Bucks!")
        self.save_money(ctx.user.username)

    # @command_manager.command("gamba")
    @requires_gamba_data
    async def gamba(self, ctx):
        args = ctx.get_args()
        if not args:
            return await self.send_message(ctx.channel,
                                           f"@{ctx.user.display_name} You must provide an amount to bet and a risk factor. Do !riskfactor to learn more")
        if len(args) < 2:
            return await self.send_message(ctx.channel,
                                           f"@{ctx.user.display_name} You must also provide a risk factor. Do !riskfactor to learn more.")

        if args[0].lower() == "all":
            args[0] = self.userdata[ctx.user.username]['money']
        try:
            amount = float(args[0])
            risk_factor = int(args[1])
        except ValueError:
            return await self.send_message(ctx.channel,
                                           f"@{ctx.user.display_name} You must provide a valid number (integer for risk factor) value.")
        if risk_factor not in range(1, 100):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} The risk factor you provided is outside the range 1-99!")
        if amount > self.userdata[ctx.user.username]["money"]:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You don't have enough Becky Bucks to bet that much!")
        if amount == 0:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You can't bet nothing bruh")
        if amount < 0:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Please specify a positive integer bruh")

        loss = random.randint(1, 100) in range(risk_factor)
        if loss:
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} YIKES! You lost {amount} Becky Bucks ❌ [LOSE]")
            self.userdata[ctx.user.username]["money"] -= amount
        else:
            payout = round((1 + risk_factor * 0.01) * amount - amount, 2)
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} You gained {payout} Becky Bucks! ✅ [WIN]")
            self.userdata[ctx.user.username]["money"] += payout
        self.userdata[ctx.user.username]["money"] = round(self.userdata[ctx.user.username]["money"], 2)
        self.save_money(ctx.user.username)

    @command_manager.command("riskfactor")
    async def risk_factor(self, ctx):
        await self.send_message(ctx.channel,
                                f"@{ctx.user.display_name} The risk factor determines your chances of losing the bet and your payout. "
                                f"The chance of you winning the bet is 100 minus the risk factor. "
                                f"Your payout is (1 + riskfactor*0.01)) * amount bet "
                                f"(basically says more risk = better payout)")

    @command_manager.command("bal", Cooldown(2, 10), aliases=["balance"])
    @requires_gamba_data
    async def balance(self, ctx):
        args = ctx.get_args()
        user_to_check = ctx.user.username
        if args:
            user_to_check = args[0].replace("@", "").lower()
        if user_to_check not in self.userdata:
            user_to_check = ctx.user.username
        await self.send_message(ctx.channel, f"{user_to_check} currently has {round(self.userdata[user_to_check]['money'])} Becky Bucks.")

    @command_manager.command("leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx):
        lead = {k: v for k, v in sorted(self.userdata.items(), key=lambda item: item[1]['money'])}
        top_users = list(lead.keys())[-5:]
        top_money = list(lead.values())[-5:]
        output = "Top 5 richest users: "
        for i in range(5):
            output += f'{i + 1}. {top_users[4 - i]}_${round(top_money[4 - i]["money"], 2)} '
        await self.send_message(ctx.channel, output)

    @command_manager.command("ranking")
    @requires_gamba_data
    async def get_ranking(self, ctx):
        lead = {k: v for k, v in sorted(self.userdata.items(), key=lambda item: item[1]['money'])}
        users = list(lead.keys())
        users.reverse()
        rank = users.index(ctx.user.username) + 1
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} You are currently rank {rank} in terms of Becky Bucks!")

    @command_manager.command("sheepp_filter", aliases=["sheep_filter"])
    async def filter(self, ctx):
        await self.send_message(ctx.channel,
                                "Here's a filter that applies to me and any user that uses my commands: https://pastebin.com/nyBX5jbb")

    @command_manager.command("give")
    @requires_gamba_data
    async def give(self, ctx):
        args = ctx.get_args()
        user_to_give = args[0].lower()
        if user_to_give not in self.userdata:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} That's not a valid user to give money to.")
        if not self.userdata[user_to_give]['settings']['receive']:
            return await self.send_message(ctx.channel,
                                           f"@{ctx.user.display_name} This user has their receive setting turned off and therefore cannot accept money.")
        amount = args[1]
        try:
            amount = round(float(amount), 2)
        except ValueError:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} That's not a valid number.")
        if self.userdata[ctx.user.username]['money'] < amount:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You don't have that much money to give.")

        if amount < 0:
            return await self.send_message(ctx.channel, "You can't give someone a negative amount OuttaPocket Tssk")

        self.userdata[ctx.user.username]['money'] -= amount
        self.userdata[user_to_give]['money'] += amount
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} You have given {user_to_give} {amount} Becky Bucks!")
        self.save_money(ctx.user.username)
        self.save_money(user_to_give)

    @command_manager.command("toggle")
    @requires_gamba_data
    async def toggle(self, ctx):
        args = ctx.get_args()
        if len(args) < 2:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You must provide a setting name and either on or off")
        setting = args[0].lower()
        if setting not in self.userdata[ctx.user.username]['settings']:
            return await self.send_message(ctx.channel,
                                           f"@{ctx.user.display_name} That's not a valid setting name. The settings consist of the following: " + ", ".join(
                                               list(self.userdata[ctx.user.username]['settings'].keys())))
        try:
            value = {"on": True, "off": False}[args[1].lower()]
        except KeyError:
            return await self.send_message(ctx.channel, "You must specify on or off.")

        self.userdata[ctx.user.username]['settings'][setting] = value
        self.database.update_userdata(ctx.user.username, setting, value)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} The {setting} setting has been turned {args[1]}.")

    @command_manager.command("rps", Cooldown(2, 4))
    @requires_gamba_data
    async def rps(self, ctx):
        args = ctx.get_args()
        if not args:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You must say either rock, paper, or scissors. "
                                                        f"(You can also use the first letter for short)")
        choice = args[0][0].lower()
        if choice not in ('r', 'p', 's'):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} That's not a valid move. You must say either rock, paper, or scissors. "
                                                        f"(You can also use the first letter for short)")

        com_choice = random.choice(('r', 'p', 's'))
        win = {"r": "s", "s": "p", "p": "r"}
        abbr = {"r": "rock", "s": "scissors", "p": "paper"}
        if com_choice == choice:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} I also chose {abbr[com_choice]}! bruh")
        if win[com_choice] == choice:
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} LETSGO I won, {abbr[com_choice]} beats {abbr[choice]}. You lose 10 Becky Bucks!")
            self.userdata[ctx.user.username]['money'] -= 10
            return self.save_money(ctx.user.username)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} IMDONEMAN I lost, {abbr[choice]} beats {abbr[com_choice]}. You win 10 Becky Bucks!")
        self.userdata[ctx.user.username]['money'] += 10
        self.save_money(ctx.user.username)
        
    @command_manager.command("new_name", permission=CommandPermission.ADMIN)
    async def new_name(self, ctx):
        # TODO: save to db
        args = ctx.get_args()
        old_name = args[0]
        new_name = args[1]
        if old_name not in self.userdata or new_name not in self.userdata:
            return await self.send_message(ctx.channel, "One of the provided names is not valid.")
        self.userdata[old_name]['money'] += self.userdata[new_name]['money']
        self.userdata[new_name] = dict(self.userdata[old_name])
        del self.userdata[old_name]
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} The data has been updated for the new name!")
        self.database.delete_user(old_name)
        self.save_money(new_name)
        for setting, val in self.userdata[new_name]["settings"].items():
            self.database.update_userdata(new_name, setting, val)

    @command_manager.command("scramble_multipliers", aliases=["scramblemultipliers", "scramble_multiplier", "scramblemultiplier"])
    async def scramble_difficulties(self, ctx):
        await self.send_message(ctx.channel,
                                f"@{ctx.user.display_name} Difficulty multiplier for each scramble: "
                                "%s" % ', '.join(
                                    ['%s-%s' % (identifier, scramble.difficulty_multiplier)
                                     for identifier, scramble in self.scrambles.items()])
                                )

    @command_manager.command("scramble_calc", aliases=["scramblecalc"])
    async def scramble_calc(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Scramble payout is calculated by picking a random number 5-10, "
                                             f"multiplying that by the length of the word (excluding spaces), multiplying "
                                             f"that by hint reduction, and multiplying that by the scramble difficulty "
                                             f"multiplier for that specific scramble. To see the difficulty multipliers, "
                                             f"do !scramble_multiplier. Hint reduction is the length of the word minus the "
                                             f"amount of hints used divided by the length of the word.")

    @command_manager.command("cumfact", aliases=["cum_fact"])
    async def fact(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} {random.choice(self.facts)}")

    @command_manager.command("afk")
    async def afk(self, ctx):
        args = ctx.get_args()
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Your afk has been set.")
        message = " ".join(args)
        self.afk[ctx.user.username] = {"message": message, "time": datetime.now(pytz.UTC).isoformat()}
        self.database.save_afk(ctx.user.username, message)

    @command_manager.command("removeafk", aliases=["rafk", "afkremove", "afkr", "unafk"])
    @requires_gamba_data
    async def afk_remove(self, ctx):
        if self.userdata[ctx.user.username]["settings"]["autoafk"]:
            return
        await self.remove_user_afk(ctx)

    @command_manager.command("help", aliases=["sheepp_commands", "sheep_commands", "sheepcommands",
                                              "sheeppcommands", "sheephelp", "sheepphelp",
                                              "sheep_help", "sheep_help"])
    async def help_command(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} sheppsubot help (do !commands for StreamElements): https://bot.sheppsu.me/")

    @requires_gamba_data
    async def on_afk(self, ctx):
        pings = set([word.replace("@", "").replace(",", "").replace(".", "").replace("-", "") for word in ctx.message.lower().split() if word.startswith("@")])
        for ping in pings:
            if ping in self.afk:
                await self.send_message(ctx.channel,  f"@{ctx.user.display_name} {ping} is afk "
                                                      f"({format_date(datetime.fromisoformat(self.afk[ping]['time']))} ago): "
                                                      f"{self.afk[ping]['message']}")

        if ctx.user.username not in self.afk or not self.userdata[ctx.user.username]["settings"]["autoafk"]:
            return
        elif (datetime.now(tz=pytz.UTC) - datetime.fromisoformat(self.afk[ctx.user.username]['time']).replace(tzinfo=pytz.UTC)).seconds > 60:
            await self.remove_user_afk(ctx)

    async def remove_user_afk(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Your afk has been removed. "
                                             f"(Afk for {format_date(datetime.fromisoformat(self.afk[ctx.user.username]['time']))}.)")
        del self.afk[ctx.user.username]
        self.database.delete_afk(ctx.user.username)

    async def trivia_category(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} I'll make something more intuitive later but for now, "
                                             f"if you want to know which number correlates to which category, "
                                             f"go here https://opentdb.com/api_config.php, click a category, "
                                             f"click generate url and then check the category specified in the url.")

    @command_manager.command("sourcecode", aliases=["sheepcode", "sheeppcode", "sheep_code", "sheepp_code"])
    async def sourcecode(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} https://github.com/Sheepposu/offlinechatbot")

    # Bomb party functions
    @command_manager.command("bombparty", aliases=["bomb_party"])
    async def bomb_party(self, ctx):
        if self.bomb_party_helper.in_progress:
            return
        self.bomb_party_helper.add_player(ctx.user.username)
        self.bomb_party_helper.on_in_progress()

        await self.send_message(ctx.channel, f"{ctx.user.username} has started a Bomb Party game! Anyone else who wants to play should type !join. When enough players have joined, the host should type !start to start the game, otherwise the game will automatically start or close after 2 minutes.")
        self.bomb_party_future = self.set_timed_event(120, self.close_or_start_game, ctx.channel)

    async def close_or_start_game(self, channel):
        if not self.bomb_party_helper.can_start:
            self.bomb_party_helper.on_close()
            return await self.send_message(channel, "The bomb party game has closed since there is only one player in the party.")
        await self.start_bomb_party(MessageContext("", channel), True)

    @command_manager.command("start")
    async def start_bomb_party(self, ctx, auto=False):
        if not auto and (not self.bomb_party_helper.in_progress or
                         self.bomb_party_helper.started or
                         ctx.user.username != self.bomb_party_helper.host):
            return
        if not self.bomb_party_helper.can_start:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You need at least 2 players to start the bomb party game.")
        if not auto:
            self.bomb_party_future.cancel()

        self.bomb_party_helper.on_start()
        self.bomb_party_helper.set_letters()

        await self.send_message(ctx.channel, f"@{self.bomb_party_helper.current_player} You're up first! Your string of letters is {self.bomb_party_helper.current_letters}")
        self.bomb_party_future = self.set_timed_event(self.bomb_party_helper.starting_time, self.bomb_party_timer, ctx.channel)

    @command_manager.command("join", Cooldown(0, 3))
    async def join_bomb_party(self, ctx):
        if not self.bomb_party_helper.in_progress or self.bomb_party_helper.started:
            return
        if ctx.user.username in self.bomb_party_helper.party:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You have already joined the game")

        self.bomb_party_helper.add_player(ctx.user.username)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} You have joined the game of bomb party!")

    @command_manager.command("leave", Cooldown(0, 3))
    async def leave_bomb_party(self, ctx):
        if ctx.user.username not in self.bomb_party_helper.party:
            return
        self.bomb_party_helper.remove_player(ctx.user.username)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} You have left the game of bomb party.")
        if self.bomb_party_helper.started and await self.check_win(ctx.channel):
            if self.bomb_party_future is not None:
                self.bomb_party_future.cancel()  #
        elif self.bomb_party_helper.started and self.bomb_party_helper.current_player.user == ctx.user.username:
            if self.bomb_party_future is not None:
                self.bomb_party_future.cancel()
            await self.next_player(ctx.channel)
        elif self.bomb_party_helper.in_progress and not self.bomb_party_helper.started:
            if len(self.bomb_party_helper.party) == 0:
                self.close_bomb_party()
                await self.send_message(ctx.channel, "The game of bomb party has closed.")

    @command_manager.command("settings", Cooldown(0, 0))
    async def change_bomb_settings(self, ctx):
        if not self.bomb_party_helper.in_progress or \
                self.bomb_party_helper.started or \
                self.bomb_party_helper.host != ctx.user.username:
            return
        args = ctx.get_args("ascii")
        if len(args) < 2:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You must provide a setting name and the value: "
                                                        f"!settings <setting> <value>. Valid settings: "
                                                        f"{self.bomb_party_helper.valid_settings_string}")
        setting = args[0]
        value = args[1]
        return_msg = self.bomb_party_helper.set_setting(setting, value)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} {return_msg}")

    @command_manager.command("players")
    async def player_list(self, ctx):
        if not self.bomb_party_helper.in_progress:
            return
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Current players playing bomb party: {', '.join(self.bomb_party_helper.player_list)}")

    async def bomb_party_timer(self, channel):
        self.exploding = True
        msg = self.bomb_party_helper.on_explode()
        print(msg)
        await self.send_message(channel, msg)
        print("checking win")
        if await self.check_win(channel):
            return
        print("next player")
        self.exploding = False
        await self.next_player(channel)

    async def next_player(self, channel):
        self.bomb_party_helper.next_player()
        self.bomb_party_helper.set_letters()
        player = self.bomb_party_helper.current_player
        await self.send_message(channel, f"@{player} Your string of letters is {self.bomb_party_helper.current_letters} - "
                                         f"You have {round(self.bomb_party_helper.seconds_left)} seconds.")
        self.bomb_party_future = self.set_timed_event(self.bomb_party_helper.seconds_left, self.bomb_party_timer, channel)

    async def on_bomb_party(self, ctx):
        word = ctx.message.lower()
        if ctx.user.username != self.bomb_party_helper.current_player.user:
            return
        if word not in self.all_words:
            return
        return_msg = self.bomb_party_helper.check_message(word)
        if return_msg is not None:
            return await self.send_message(ctx.channel, f"@{self.bomb_party_helper.current_player} {return_msg}")
        if self.exploding:
            return
        self.bomb_party_future.cancel()
        self.bomb_party_helper.on_word_used(word)
        await self.next_player(ctx.channel)

    async def check_win(self, channel):
        winner = self.bomb_party_helper.get_winner()
        print(winner)
        if winner is None:
            return False
        winner = winner.user
        if winner not in self.userdata:
            return self.send_message(channel, f"@{winner} Well this is a bit awkward... You aren't yet added to the database and"
                                              "due to a something I'm too lazy to fix, I cannot create a user from this part in the code...")
        money = self.bomb_party_helper.winning_money
        self.userdata[winner]['money'] += money
        self.save_money(winner)
        self.close_bomb_party(False)
        await self.send_message(channel, f"@{winner} Congratulations on winning the bomb party game! You've won {money} Becky Bucks!")
        return True

    def close_bomb_party(self, cancel=True):
        if cancel and not self.bomb_party_future.done():
            self.bomb_party_future.cancel()
        self.bomb_party_future = None
        self.bomb_party_helper.on_close()

    @command_manager.command("funfact")
    async def random_fact(self, ctx):
        fact = requests.get("https://uselessfacts.jsph.pl/random.json?language=en")
        fact.raise_for_status()
        await self.send_message(ctx.channel, f"Fun fact: {fact.json()['text']}")

    @command_manager.command("reload_db", permission=CommandPermission.ADMIN)
    async def reload_from_db(self, ctx):
        self.reload_db_data()
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Local data has been reloaded from database.")

    @command_manager.command("anime_compare", aliases=["animecompare", "ac"], cooldown=Cooldown(0, 5),
                             banned=["osuwho"])
    async def anime_compare(self, ctx):
        game = self.compare_helper.get_game(ctx.user.username)
        if game is not None:
            return
        game = self.compare_helper.new_game(ctx.user.username, self.anime)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} {game.get_question_string()}")
        game.id = self.database.new_animecompare_game(ctx.user.username)
        self.anime_compare_future[ctx.user.username] = self.set_timed_event(10, self.anime_compare_timeout, ctx, game)

    async def on_anime_compare(self, ctx, game):
        check = self.compare_helper.check_guess(ctx, game)
        if check is None:
            return
        self.anime_compare_future[ctx.user.username].cancel()
        self.anime_compare_future[ctx.user.username] = None
        if not check:
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} Incorrect. Your final score is {game.score}. {game.get_ranking_string()}.")
            self.database.finish_animecompare_game(game.id)
            self.compare_helper.finish_game(game)
        else:
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} Correct! Your current score is {game.score}. {game.get_ranking_string()}.")
            self.compare_helper.generate_answer(self.anime, game)
            self.database.update_animecompare_game(game.id, game.score)
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} {game.get_question_string()}")
            self.anime_compare_future[ctx.user.username] = self.set_timed_event(10, self.anime_compare_timeout, ctx, game)

    async def anime_compare_timeout(self, ctx, game):
        self.compare_helper.finish_game(game)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} You did not answer in time. Your final score is {game.score}.")
        self.database.finish_animecompare_game(game.id)
        self.anime_compare_future[ctx.user.username] = None

    @command_manager.command("average_ac", aliases=["acaverage", "ac_avg", "ac_average", "acavg"])
    async def average_anime_compare(self, ctx):
        games = self.database.get_user_animecompare_games(ctx.user.username)
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Your average score is {round(sum([game['score'] for game in games])/len(games), 2)}.")

    @command_manager.command("ac_leaderboard", aliases=["aclb", "ac_lb", "acleaderboard"])
    async def anime_compare_leaderboard(self, ctx):
        games = self.database.get_top_animecompare_games()
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} The top anime compare scores are: {', '.join(['%s_%d' %(game['user'], game['score']) for game in games])}.")

    @command_manager.command("ac_top", aliases=["actop", "topac", "top_ac"], cooldown=Cooldown(0, 5))
    async def anime_compare_top(self, ctx):
        game = self.database.get_top_animecompare_game_for_user(ctx.user.username)
        if not game:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} You have not played any anime compare games yet.")
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Your top anime compare score is {game[0]['score']}.")

    def process_value_arg(self, flag, args, default=None):
        lower_args = list(map(str.lower, args))
        if flag in lower_args:
            index = lower_args.index(flag)
            args.pop(index)
            if len(args) == 0:
                return
            value = args.pop(index).strip()
            return value
        return default

    def process_arg(self, flag, args):
        lower_args = list(map(str.lower, args))
        if flag in lower_args:
            args.pop(lower_args.index(flag))
            return True
        return False

    async def process_osu_user_arg(self, ctx, args):
        if len(args) == 0 and ctx.user.username in self.osu_data:  # user did not provide a username, but osu! account is linked
            return self.osu_data[ctx.user.username]['user_id']
        elif len(args) == 0 or args[0].strip() == "":  # user did not provide a username and osu! account is not linked
            await self.send_message(ctx.channel, f"@{ctx.user.display_name} Please specify a username or "
                                                 "link your account with !link [username].")
        else:  # username was provided
            return " ".join(args).strip()

    async def process_osu_mode_args(self, ctx, args):
        arg = self.process_value_arg("-m", args, 0)
        if arg is None:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Must specify a mode with the -m argument. "
                                                        f"Valid modes are 0 (osu), 1 (taiko), 2 (catch), 3 (mania).")
        if type(arg) != int and (not arg.isdigit() or int(arg) not in range(0, 4)):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Invalid mode. Valid modes "
                                                        f"are 0 (osu), 1 (taiko), 2 (catch), 3 (mania).")
        return ("osu", "taiko", "fruits", "mania")[int(arg)]

    async def process_index_arg(self, ctx, args, rng=range(1, 101)):
        arg = self.process_value_arg("-i", args, -1)
        if arg == -1:
            return -1
        if arg is None:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Must specify an index with the -i argument. "
                                                        f"Specify a number between {rng[0]} and {rng[-1]}")
        if type(arg) != int and (not arg.isdigit() or int(arg) not in rng):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Must specify a number between "
                                                        f"{rng[0]} and {rng[-1]} for the -i argument.")
        return int(arg)-1

    async def get_osu_user_id_from_osu_username(self, ctx, username):
        if username not in self.user_id_cache:
            user = await self.make_osu_request(self.osu_client.get_user(user=username, key="username"))
            if user is None:
                return await self.send_message(ctx.channel, f"@{ctx.user.display_name} User {username} not found.")
            self.user_id_cache[username] = user.id
        return self.user_id_cache[username]

    @staticmethod
    def get_if_fc(o_score, beatmap, beatmap_attributes):
        score = deepcopy(o_score)
        if score.mode == GameModeStr.STANDARD:
            count_300 = score.statistics.count_300
            count_100 = score.statistics.count_100
            count_50 = score.statistics.count_50
            count_miss = score.statistics.count_miss
            total_objects = beatmap.count_sliders + beatmap.count_spinners + beatmap.count_circles
            total_hits = count_300 + count_100 + count_50 + count_miss

            count_300 += count_miss + total_objects - total_hits
            score.statistics.count_300 = count_300
            score.statistics.count_miss, count_miss = 0, 0
            score.max_combo = beatmap_attributes.max_combo

            accuracy = (count_300 * 300 + count_100 * 100 + count_50 * 50) / \
                       (300 * (count_300 + count_100 + count_50 + count_miss))
            score.accuracy = accuracy

            return accuracy, Bot.calculate_pp(score, beatmap, beatmap_attributes)

    @staticmethod
    def calculate_pp(score, beatmap, beatmap_attributes, transform=True):
        if beatmap.mode == GameModeStr.STANDARD:
            attributes = OsuDifficultyAttributes.from_attributes({
                'aim_difficulty': beatmap_attributes.mode_attributes.aim_difficulty,
                'speed_difficulty': beatmap_attributes.mode_attributes.speed_difficulty,
                'flashlight_difficulty': beatmap_attributes.mode_attributes.flashlight_difficulty,
                'slider_factor': beatmap_attributes.mode_attributes.slider_factor,
                'speed_note_count': beatmap_attributes.mode_attributes.speed_note_count,
                'approach_rate': beatmap_attributes.mode_attributes.approach_rate,
                'overall_difficulty': beatmap_attributes.mode_attributes.overall_difficulty,
                'max_combo': beatmap_attributes.max_combo,
                'drain_rate': beatmap.drain,
                'hit_circle_count': beatmap.count_circles,
                'slider_count': beatmap.count_sliders,
                'spinner_count': beatmap.count_spinners,
            })
            if transform:
                score = OsuScoreAttributes.from_osupy_score(score)
            calculator = OsuPerformanceCalculator(GameModeStr.STANDARD, attributes, score)
            try:
                return calculator.calculate()
            except:
                traceback.print_exc()

    def get_score_message(self, score, beatmap, beatmap_attributes, prefix="Recent score for {username}"):
        score_format = prefix+":{passed} {artist} - {title} [{diff}]{mods} ({mapper}, {star_rating}*) " \
                    "{acc}% {combo}/{max_combo} | ({genki_counts}) | {pp}{if_fc_pp} | {time_ago} ago"
        # Format and send message for recent score
        genkis = (score.statistics.count_300, score.statistics.count_100,
                  score.statistics.count_50, score.statistics.count_miss) if score.mode == GameModeStr.STANDARD else (
            score.statistics.count_geki, score.statistics.count_300, score.statistics.count_katu,
            score.statistics.count_100, score.statistics.count_50, score.statistics.count_miss
        )
        total_objects = beatmap.count_sliders + beatmap.count_spinners + beatmap.count_circles
        if score.pp is None and score.passed and score.mode == GameModeStr.STANDARD and beatmap_attributes.type is not None:
            score.pp = self.calculate_pp(score, beatmap, beatmap_attributes)
        if_fc_acc, if_fc_pp = None, None
        if beatmap_attributes.type is not None and score.max_combo != beatmap_attributes.max_combo and score.mode == GameModeStr.STANDARD:
            if_fc_acc, if_fc_pp = self.get_if_fc(score, beatmap, beatmap_attributes)
        return score_format.format(**{
            "username": score.user.username,
            "passed": "" if score.passed else f"(Failed {round(sum(genkis) / total_objects * 100)}%)",
            "artist": score.beatmapset.artist,
            "title": score.beatmapset.title,
            "diff": score.beatmap.version,
            "mods": " +" + score.mods.to_readable_string() if score.mods else "",
            "mapper": score.beatmapset.creator,
            "star_rating": round(beatmap_attributes.star_rating, 2),
            "pp": f"{round(score.pp, 2)}pp" if score.pp and score.passed else "",
            "if_fc_pp": f" ({round(if_fc_pp, 2)} for {round(if_fc_acc * 100, 2)}% FC)" if if_fc_pp is not None else "",
            "acc": round(score.accuracy * 100, 2),
            "combo": score.max_combo,
            "max_combo": beatmap_attributes.max_combo,
            "genki_counts": ("%d/%d/%d/%d" if score.mode == GameModeStr.STANDARD else "%d/%d/%d/%d/%d/%d") % genkis,
            "time_ago": format_date(score.created_at)
        })

    def get_compact_scores_message(self, scores):
        score_format = "{artist} - {title} [{diff}]{mods} {acc}% ({genki_counts}): {pp}pp | {time_ago} ago"
        message = ""
        for score in scores:
            message += "🌟" + score_format.format(**{
                "artist": score.beatmapset.artist,
                "title": score.beatmapset.title,
                "diff": score.beatmap.version,
                "mods": " +" + score.mods.to_readable_string() if score.mods else "",
                "acc": round(score.accuracy * 100, 2),
                "genki_counts": f"%d/%d/%d/%d" % (
                    score.statistics.count_300,
                    score.statistics.count_100,
                    score.statistics.count_50,
                    score.statistics.count_miss
                ),
                "pp": 0 if score.pp is None else round(score.pp, 2),
                "time_ago": format_date(score.created_at),
            })
        return message

    async def get_osu_user_id_from_args(self, ctx, args):
        user = await self.process_osu_user_arg(ctx, args)
        if user is None:
            return
        elif type(user) == str:
            return await self.get_osu_user_id_from_osu_username(ctx, user)
        return user

    def osu_username_from_id(self, user_id):
        return dict(map(reversed, self.user_id_cache.items()))[user_id]

    async def make_osu_request(self, request, use_lazer=False):
        await self.osu_lock.acquire()
        self.osu_client.http.use_lazer = use_lazer
        try:
            result = await request
        except:
            traceback.print_exc()
            result = None
        self.osu_lock.release()
        return result

    def get_map_cache(self, ctx):
        if ctx.reply:
            print(ctx.reply.msg_body)
            if ctx.reply.msg_body in self.recent_score_cache[ctx.channel]:
                return self.recent_score_cache[ctx.channel][ctx.reply.msg_body]
            return
        return list(self.recent_score_cache[ctx.channel].values())[-1]

    def add_recent_map(self, ctx, sent_message, beatmap, attributes):
        msg = " ".join(sent_message)
        if msg in self.recent_score_cache[ctx.channel]: del self.recent_score_cache[ctx.channel][msg]
        self.recent_score_cache[ctx.channel].update({msg: (beatmap, attributes)})
        while len(self.recent_score_cache) > 50:
            del self.recent_score_cache[ctx.channel][list(self.recent_score_cache[ctx.channel].keys())[0]]

    async def get_beatmap_from_arg(self, ctx, beatmap_link):
        beatmap_id = tuple(filter(lambda s: len(s.strip()) > 0, beatmap_link.split("/")))[-1]
        beatmap = await self.make_osu_request(self.osu_client.get_beatmap(beatmap_id))
        if beatmap is None:
            return await self.send_message(ctx.channel, "Failed to get beatmap from the provided link/id.")
        beatmap_attributes = await self.make_osu_request(self.osu_client.get_beatmap_attributes(beatmap_id))
        return beatmap, beatmap_attributes

    @command_manager.command("rs", cooldown=Cooldown(0, 3))
    async def recent_score(self, ctx):
        args = ctx.get_args('ascii')
        mode = await self.process_osu_mode_args(ctx, args)
        if mode is None:
            return
        index = await self.process_index_arg(ctx, args)
        if index is None:
            return
        if index == -1:
            index = 0
        best = self.process_arg("-b", args)

        user_id = await self.get_osu_user_id_from_args(ctx, args)
        if user_id is None:
            return
        username = self.osu_username_from_id(user_id)

        # Get recent score
        if not best:
            scores = await self.make_osu_request(
                self.osu_client.get_user_scores(
                    user_id, "recent", include_fails=1, mode=mode, limit=1, offset=index))
        else:
            scores = await self.make_osu_request(
                self.osu_client.get_user_scores(user_id, "best", mode=mode, limit=100))
            scores = sorted(scores, key=lambda x: x.created_at, reverse=True)
        if not scores:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} User {username} "
                                                        f"has no recent scores for {proper_mode_name[mode]} "
                                                        "or the index you specified is out of range.")

        score = scores[0 if not best else index]
        beatmap = await self.make_osu_request(
            self.osu_client.get_beatmap(score.beatmap.id))
        beatmap_attributes = await self.make_osu_request(
            self.osu_client.get_beatmap_attributes(beatmap.id, score.mods if score.mods else None, score.mode))
        sent_message = await self.send_message(ctx.channel, self.get_score_message(score, beatmap, beatmap_attributes))
        self.add_recent_map(ctx, sent_message, beatmap, beatmap_attributes)

    @command_manager.command("c", aliases=['compare'], cooldown=Cooldown(0, 3))
    async def compare_score(self, ctx):
        if not len(self.recent_score_cache[ctx.channel]):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} I don't have a cache of the last beatmap.")
        cache = self.get_map_cache(ctx)
        if cache is None: return
        await self.compare_score_func(ctx, False, cache)

    async def compare_score_func(self, ctx, most_recent=True, cache=None):
        beatmap, beatmap_attributes = list(self.recent_score_cache[ctx.channel].values())[-1] if most_recent else cache
        args = ctx.get_args('ascii')
        if '-m' not in map(str.lower, args):
            mode = None
        else:
            mode = await self.process_osu_mode_args(ctx, args)
            if mode is None:
                return

        user_id = await self.get_osu_user_id_from_args(ctx, args)
        if user_id is None:
            return
        username = self.osu_username_from_id(user_id)

        update_beatmap_attributes = mode is not None and mode != beatmap.mode.value
        if mode is None:
            mode = beatmap.mode.value

        try:
            scores = await self.make_osu_request(
                self.osu_client.get_user_beatmap_scores(beatmap.id, user_id, mode))
        except client_exceptions.ClientResponseError:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} This map does not have a leaderboard.")
        if not scores:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} User {username} has no "
                                                        f"scores on that beatmap for mode {proper_mode_name[mode]}.")

        # For max_combo attribute
        if update_beatmap_attributes:
            beatmap_attributes = await self.make_osu_request(self.osu_client.get_beatmap_attributes(beatmap.id, ruleset=mode))

        score_format = "{mods} {acc}% {combo}/{max_combo} | ({genki_counts}) | {pp} | {time_ago} ago"
        message = f"Scores for {username} on {beatmap.beatmapset.artist} - {beatmap.beatmapset.title} " \
                  f"[{beatmap.version}] ({beatmap.beatmapset.creator}): "
        for score in scores[:5]:
            # if score.pp is None and score.passed:
            #     score.pp = self.calculate_pp(score, beatmap, beatmap_attributes)
            # if_fc_acc, if_fc_pp = None, None
            # if score.max_combo != beatmap_attributes.max_combo and score.mode == GameModeStr.STANDARD:
            #     if_fc_acc, if_fc_pp = self.get_if_fc(score, beatmap, beatmap_attributes)
            message += "🌟" + score_format.format(**{
                "mods": score.mods.to_readable_string() if score.mods else "",
                "acc": round(score.accuracy * 100, 2),
                "combo": score.max_combo,
                "max_combo": beatmap_attributes.max_combo,
                "genki_counts": f"%d/%d/%d/%d" % (
                    score.statistics.count_300,
                    score.statistics.count_100,
                    score.statistics.count_50,
                    score.statistics.count_miss
                ),
                "pp": f"{round(score.pp, 2)}pp" if score.pp else "No pp",
                #"pp": f"{round(score.pp, 2)}pp" if score.pp and score.passed else "",
                #"if_fc_pp": f" ({round(if_fc_pp, 2)} for {round(if_fc_acc * 100, 2)}% FC)"
                #if if_fc_pp is not None else "",
                "time_ago": format_date(score.created_at)
            })
        sent_message = await self.send_message(ctx.channel, message)
        self.add_recent_map(ctx, sent_message, beatmap, beatmap_attributes)

    @command_manager.command("map", aliases=["m"])
    async def send_map(self, ctx):
        args = ctx.get_args('ascii')
        if len(args) > 0:
            result = await self.get_beatmap_from_arg(ctx, args[0])
            if result is None:
                return
            beatmap, beatmap_attributes = result
        elif len(self.recent_score_cache[ctx.channel]) == 0:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} I don't have a cache of the last beatmap.")
        else:
            beatmap, beatmap_attributes = self.get_map_cache(ctx)

        if beatmap is None or beatmap_attributes is None: return

        text = f"{beatmap.beatmapset.artist} - {beatmap.beatmapset.title} [{beatmap.version}] " \
               f"({beatmap.beatmapset.creator}, {round(beatmap_attributes.star_rating, 2)}*) https://osu.ppy.sh/b/{beatmap.id}"
        sent_message = await self.send_message(ctx.channel, f"@{ctx.user.display_name} {text}")
        self.add_recent_map(ctx, sent_message, beatmap, beatmap_attributes)

    @command_manager.command("osu", cooldown=Cooldown(0, 5))
    async def osu_profile(self, ctx):
        args = ctx.get_args('ascii')
        use_lazer = self.process_arg('-l', args)
        mode = await self.process_osu_mode_args(ctx, args)
        if mode is None:
            return
        user = await self.get_osu_user_id_from_args(ctx, args)
        if user is None:
            return
        username = self.osu_username_from_id(user)

        try:
            user = await self.make_osu_request(
                self.osu_client.get_user(user=user, mode=mode, key="username" if type(user) == str else "id"), use_lazer)
        except client_exceptions.ClientResponseError:
            return await self.send_message(ctx.channel, f"{ctx.user.display_name} A user with the name {username} "
                                                        "does not exist. If they did before it's possible they "
                                                        "got restricted or had their account deleted.")

        if user is None:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} User {username} not found.")

        stats = user.statistics

        total_medals = 280
        profile_layout = "{username}'s profile [{mode}]: #{global_rank} ({country}#{country_rank}) - {pp}pp; Peak (last 90 days): #{peak_rank} | " \
                         "{accuracy}% | {play_count} playcount ({play_time} hrs) | Medal count: {medal_count}/{total_medals} ({medal_completion}%) | " \
                         "Followers: {follower_count} | Mapping subs: {subscriber_count}"
        await self.send_message(ctx.channel, profile_layout.format(**{
            "username": user.username,
            "mode": proper_mode_name[mode],
            "global_rank": stats.global_rank,
            "country": user.country["code"],
            "country_rank": stats.country_rank,
            "pp": stats.pp,
            "peak_rank": min(user.rank_history["data"]),
            "accuracy": round(stats.hit_accuracy, 2),
            "play_count": stats.play_count,
            "play_time": stats.play_time//3600,
            "medal_count": len(user.user_achievements),
            "total_medals": total_medals,
            "medal_completion": round(len(user.user_achievements) / total_medals * 100, 2),
            "follower_count": user.follower_count,
            "subscriber_count": user.mapping_follower_count,
        }))

    @command_manager.command("osutop", cooldown=Cooldown(0, 5))
    async def osu_top(self, ctx):
        args = ctx.get_args('ascii')

        use_lazer = self.process_arg('-l', args)
        mode = await self.process_osu_mode_args(ctx, args)
        if mode is None:
            return
        recent_tops = self.process_arg("-r", args)
        index = await self.process_index_arg(ctx, args)
        if index is None:
            return

        user_id = await self.get_osu_user_id_from_args(ctx, args)
        if user_id is None:
            return
        username = self.osu_username_from_id(user_id)

        top_scores = await self.make_osu_request(
            self.osu_client.get_user_scores(user_id, "best", mode=mode, limit=100), use_lazer)
        if not top_scores:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} User {username} has no top scores for {proper_mode_name[mode]}.")
        if recent_tops:
            top_scores = sorted(top_scores, key=lambda x: x.created_at, reverse=True)
        top_scores = top_scores[:5] if index == -1 else [top_scores[index]]
        username = top_scores[0].user.username

        beatmap = None
        beatmap_attributes = None
        if len(top_scores) > 1:
            message = f"Top{' recent' if recent_tops else ''} {proper_mode_name[mode]} " \
                      f"scores for {username}: " if index == -1 else f"Top {index+1}{' recent' if recent_tops else ''} " \
                                                                     f"{proper_mode_name[mode]} score for {username}: "
            message += self.get_compact_scores_message(top_scores)
        else:
            score = top_scores[0]
            beatmap = await self.make_osu_request(self.osu_client.get_beatmap(score.beatmap.id))
            beatmap_attributes = await self.make_osu_request(self.osu_client.get_beatmap_attributes(beatmap.id, score.mods, score.mode))
            message = self.get_score_message(score, beatmap, beatmap_attributes,
                                             prefix=f"Top {index+1} score for {username}")

        sent_message = await self.send_message(ctx.channel, message)
        if beatmap_attributes is not None:
            self.add_recent_map(ctx, sent_message, beatmap, beatmap_attributes)

    @command_manager.command("link", cooldown=Cooldown(0, 2))
    async def link_osu_account(self, ctx):
        args = ctx.get_args('ascii')
        if len(args) == 0 or args[0].strip() == "":
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Please specify a username.")

        username = " ".join(args).strip()
        user = await self.make_osu_request(
            self.osu_client.get_user(user=username, key="username"))

        if user is None:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} User {username} not found.")

        if ctx.user.username in self.osu_data:
            self.database.update_osu_data(ctx.user.username, user.username, user.id)
        else:
            self.database.new_osu_data(ctx.user.username, user.username, user.id)
        self.osu_data[ctx.user.username] = {"username": user.username, "user_id": user.id}
        self.user_id_cache[user.username] = user.id

        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Linked {user.username} to your account.")

    @command_manager.command("simulate", cooldown=Cooldown(0, 2), aliases=["s"])
    async def simulate_score(self, ctx):
        if len(self.recent_score_cache[ctx.channel]) == 0:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} I don't have a cache of the last beatmap.")
        beatmap = self.get_map_cache(ctx)
        if beatmap is None: return
        beatmap = beatmap[0]
        if beatmap.mode != GameModeStr.STANDARD:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Sorry, but I only support pp calculation for osu!std at the moment.")

        args = ctx.get_args("ascii")
        if len(args) == 0:
            mods = None
        else:
            mods = args[0]
            if mods.startswith("+"):
                mods = mods[1:]
            mods = [mods[i*2:i*2+2] for i in range(len(mods)//2)]
            try:
                mods = list(map(Mods.get_from_abbreviation, mods))
            except KeyError:
                return await self.send_message(ctx.channel, f"{ctx.user.display_name} The mod combination you gave is invalid.")
            mods = Mods.get_from_list(mods)

        beatmap_difficulty = await self.make_osu_request(
            self.osu_client.get_beatmap_attributes(beatmap.id, mods))

        pp_values = []
        total_objects = beatmap.count_sliders + beatmap.count_circles + beatmap.count_spinners
        for acc in (1, 0.99, 0.98, 0.97, 0.96, 0.95):
            h2 = round((3/2)*total_objects*(1-acc))
            h1 = total_objects - h2
            actual_acc = (300*h1 + 100*h2)/(300*total_objects)
            score = OsuScoreAttributes()
            score.set_attributes({
                "mods": mods,
                "accuracy": actual_acc,
                "score_max_combo": beatmap_difficulty.max_combo,
                "count_great": h1,
                "count_ok": h2,
                "count_meh": 0,
                "count_miss": 0,
            })
            pp = self.calculate_pp(score, beatmap, beatmap_difficulty, False)
            pp_values.append(f"{int(acc*100)}% - {round(pp, 2)}")

        await self.send_message(ctx.channel, f"@{ctx.user.display_name} {mods.to_readable_string() if mods is not None else 'NM'}: {' | '.join(pp_values)}")

    @command_manager.command("score", aliases=["sc"])
    async def osu_score(self, ctx):
        args = ctx.get_args('ascii')

        if len(args) < 1:
            return await self.send_message(ctx.channel, "Must give a beatmap link or beatmap id.")
        result = await self.get_beatmap_from_arg(ctx, args[0])
        if result is None:
            return
        beatmap, beatmap_attributes = result

        i = ctx.message.index(args[0])
        ctx.message = (ctx.message[:i] + ctx.message[i+len(args[0])+1:]).strip()

        await self.compare_score_func(ctx, False, (beatmap, beatmap_attributes))

    @command_manager.command("validtz")
    async def valid_timezones(self, ctx):
        await self.send_message(ctx.channel, "Having trouble linking your timezone? Here's a list of valid timezones (use the text on the left column): "
                                "https://www.ibm.com/docs/en/cloudpakw3700/2.3.0.0?topic=SS6PD2_2.3.0/doc/psapsys_restapi/time_zone_list.html")

    @command_manager.command("linktz", cooldown=Cooldown(0, 3))
    async def link_timezone(self, ctx):
        args = ctx.get_args("ascii")
        if len(args) == 0 or args[0].strip() == "":
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Please specify a timezone to link.")

        tz = args[0].lower().strip()
        if tz.startswith("gmt"):
            tz = "Etc/" + tz
        elif tz.startswith("utc"):
            tz = "Etc/GMT" + tz[3:]
        lower_timezones = list(map(str.lower, all_timezones))
        if tz not in lower_timezones:
            if tz.upper() in self.tz_abbreviations:
                tz = self.tz_abbreviations[tz.upper()][0]
            else:
                return await self.send_message(ctx.channel, f"@{ctx.user.display_name} That's not a valid timezone. Do !validtz if you are having trouble.")
        else:
            tz = all_timezones[lower_timezones.index(tz)]
        if ctx.user_id in self.timezones:
            self.database.update_timezone(ctx.user_id, tz)
        else:
            self.database.add_timezone(ctx.user_id, tz)
        self.timezones[ctx.user_id] = timezone(tz)

        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Timezone has been linked!")

    @command_manager.command("utime", aliases=["usertime"], cooldown=Cooldown(1, 1))
    async def user_time(self, ctx):
        args = ctx.get_args("ascii")
        if len(args) == 0 or args[0].strip() == "":
            username = ctx.user.username
        else:
            username = args[0].lower().replace("@", "")

        if username not in self.userdata:
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} I don't recognize the user {username}")
        userid = self.userdata[username]["userid"]
        if userid not in list(self.timezones.keys()):
            return await self.send_message(ctx.channel, f"@{ctx.user.display_name} "
                                                        f"{'This user has' if username != ctx.user.username else 'You have'} "
                                                        f"not linked a timezone, which can be done with !linktz")

        return await self.send_message(ctx.channel, f"@{ctx.user.display_name} Time for {username}: "
                                                    f"{datetime.now().astimezone(self.timezones[userid]).strftime('%H:%M (%Z)')}")

    @command_manager.command("oct")
    async def offlinechattournament(self, ctx):
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Offline Chat Tournament "
                                             "(osu! tournament for offline chat) discord: https://discord.gg/tuE84PX9mx")

    @command_manager.command("refresh_emotes", cooldown=Cooldown(60, 0))
    async def refresh_emotes(self, ctx):
        self.emotes[ctx.channel] = sum(self.emote_requester.get_channel_emotes(channel), [])
        await self.send_message(ctx.channel, f"@{ctx.user.display_name} Emotes have been refreshed "
                                             f"(this command has a 1 minute cooldown).")


bot = Bot(command_manager)
bot.running = True
bot.loop.run_until_complete(bot.start())
