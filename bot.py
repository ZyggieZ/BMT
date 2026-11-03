import atexit
import asyncio
import datetime
import json
import os
import random
import re
import struct
import sys
import discord
from discord import app_commands
from discord.ext import commands
from discord.gateway import DiscordVoiceWebSocket
import imageio_ffmpeg
import yt_dlp

# Set UTF-8 encoding for stdout
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==========================================
# SINGLE INSTANCE PROCESS LOCK
# ==========================================
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
_lock_handle = None

def acquire_single_instance_lock() -> bool:
    """Enforces that only one instance of the bot can run at any given time."""
    global _lock_handle
    try:
        _lock_handle = open(LOCK_FILE, "a+", encoding="utf-8")
        if sys.platform == "win32":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        _lock_handle.seek(0)
        _lock_handle.truncate()
        _lock_handle.write(f"PID: {os.getpid()}\nStarted: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n")
        _lock_handle.flush()
        return True
    except (BlockingIOError, OSError, IOError):
        return False

def _release_lock():
    global _lock_handle
    if _lock_handle:
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    _lock_handle.seek(0)
                    msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            _lock_handle.close()
        except Exception:
            pass
        _lock_handle = None
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass

atexit.register(_release_lock)

# ==========================================
# OPUS & DAVE E2EE PROTOCOL CONFIGURATION
# ==========================================
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# Load Opus encoder on Windows
if not discord.opus.is_loaded():
    bin_dir = os.path.join(os.path.dirname(discord.__file__), "bin")
    dll_name = "libopus-0.x64.dll" if sys.maxsize > 2**32 else "libopus-0.x86.dll"
    dll_path = os.path.join(bin_dir, dll_name)
    if os.path.exists(dll_path):
        discord.opus.load_opus(dll_path)
        print(f"Loaded bundled Opus library: {dll_path}", flush=True)
    else:
        try:
            discord.opus.load_opus("libopus-0.x64.dll")
            print("Loaded Opus library globally.", flush=True)
        except Exception as e:
            print(f"Failed to load Opus: {e}", flush=True)

# Patch discord.py's DAVE protocol handler so MLS key transition executes properly
import discord.voice_state
orig_received_binary = DiscordVoiceWebSocket.received_binary_message
orig_execute_transition = discord.voice_state.VoiceConnectionState._execute_transition

async def patched_received_binary(self, msg: bytes):
    op = msg[2]
    state = self._connection
    if op == self.MLS_ANNOUNCE_COMMIT_TRANSITION:
        transition_id = struct.unpack_from(">H", msg, 3)[0]
        try:
            state.dave_session.process_commit(msg[5:])
            state.dave_session.prepare_transition(transition_id, state.dave_protocol_version)
            state.dave_session.execute_transition(transition_id)
            if transition_id != 0:
                state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                await self.send_transition_ready(transition_id)
            print(f"DAVE E2EE Handshake (Commit) Complete! can_encrypt={state.dave_session.can_encrypt}", flush=True)
        except Exception as e:
            print(f"DAVE commit error: {e}", flush=True)
        return
    elif op == self.MLS_WELCOME:
        transition_id = struct.unpack_from(">H", msg, 3)[0]
        try:
            state.dave_session.process_welcome(msg[5:])
            state.dave_session.prepare_transition(transition_id, state.dave_protocol_version)
            state.dave_session.execute_transition(transition_id)
            if transition_id != 0:
                state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                await self.send_transition_ready(transition_id)
            print(f"DAVE E2EE Handshake (Welcome) Complete! can_encrypt={state.dave_session.can_encrypt}", flush=True)
        except Exception as e:
            print(f"DAVE welcome error: {e}", flush=True)
        return

    await orig_received_binary(self, msg)

async def patched_execute_transition(self, transition_id: int):
    if self.dave_session:
        try:
            self.dave_session.prepare_transition(transition_id, self.dave_protocol_version)
            self.dave_session.execute_transition(transition_id)
            print(f"DAVE _execute_transition Complete! can_encrypt={self.dave_session.can_encrypt}", flush=True)
        except Exception as e:
            pass
    await orig_execute_transition(self, transition_id)

DiscordVoiceWebSocket.received_binary_message = patched_received_binary
discord.voice_state.VoiceConnectionState._execute_transition = patched_execute_transition

# Load token from .env if present
token = os.getenv("DISCORD_BOT_TOKEN")
if not token and os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not token:
    token = input("Enter your Discord Bot Token: ").strip()

if not token:
    print("Error: No bot token provided. Exiting.", flush=True)
    sys.exit(1)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

async def reply_safe(interaction: discord.Interaction, content=None, embed=None, view=None, ephemeral=False):
    """Safely responds to an interaction; falls back to channel.send if interaction token expired."""
    kwargs = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        else:
            return await interaction.response.send_message(**kwargs)
    except (discord.NotFound, discord.HTTPException) as e:
        print(f"Interaction send failed ({e}), falling back to channel.send", flush=True)
        kwargs.pop("ephemeral", None)
        try:
            return await interaction.channel.send(**kwargs)
        except Exception as e2:
            print(f"Failed fallback channel send: {e2}", flush=True)

# Global app command error handler
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.NotFound):
        if error.original.code == 10062:
            print(f"Interaction for {interaction.command} was closed by Discord before reply. Ignored.", flush=True)
            return

    print(f"Command error in {interaction.command}: {error}", flush=True)
    error_msg = f"❌ Error: {error}"
    await reply_safe(interaction, content=error_msg, ephemeral=True)

# ==========================================
# TIMERS MANAGEMENT (with persistence)
# ==========================================
active_timers: dict[int, list[dict]] = {}
TIMERS_FILE = "timers.json"

def save_timers():
    """Saves non-expired timers to disk."""
    data = {}
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for user_id, timer_list in active_timers.items():
        saved_list = []
        for t in timer_list:
            if t["end_timestamp"] > now_ts:
                saved_list.append({
                    "id": t["id"],
                    "duration_str": t["duration_str"],
                    "end_timestamp": t["end_timestamp"],
                    "channel_id": t["channel_id"]
                })
        if saved_list:
            data[str(user_id)] = saved_list
    with open(TIMERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def parse_duration(text: str) -> int | None:
    text = text.strip().lower()
    matches = re.findall(r"(\d+)\s*([dhms])", text)
    if not matches:
        if text.isdigit():
            return int(text)
        return None

    total = 0
    for val, unit in matches:
        num = int(val)
        if unit == "d":
            total += num * 86400
        elif unit == "h":
            total += num * 3600
        elif unit == "m":
            total += num * 60
        elif unit == "s":
            total += num
    return total

async def run_timer(user_id: int, timer_data: dict):
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    remaining = timer_data["end_timestamp"] - now_ts
    if remaining > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

    try:
        channel = bot.get_channel(timer_data["channel_id"])
        if not channel:
            channel = await bot.fetch_channel(timer_data["channel_id"])

        user_mention = f"<@{user_id}>"
        duration_str = timer_data["duration_str"]

        embed = discord.Embed(
            title="⏰ Time's Up!",
            description=f"{user_mention}, your timer for **{duration_str}** is finished!",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        await channel.send(content=user_mention, embed=embed)
        print(f"Timer alert sent to user {user_id} in channel {timer_data['channel_id']}", flush=True)
    except Exception as e:
        print(f"Error alerting timer: {e}", flush=True)
    finally:
        if user_id in active_timers:
            active_timers[user_id] = [t for t in active_timers[user_id] if t["id"] != timer_data["id"]]
            if not active_timers[user_id]:
                del active_timers[user_id]
        save_timers()

def load_saved_timers():
    """Restores saved timers from file upon startup."""
    if not os.path.exists(TIMERS_FILE):
        return
    try:
        with open(TIMERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uid_str, timer_list in data.items():
            uid = int(uid_str)
            active_timers[uid] = []
            for item in timer_list:
                item["task"] = asyncio.create_task(run_timer(uid, item))
                active_timers[uid].append(item)
        if active_timers:
            print(f"Restored active timers for {len(active_timers)} user(s).", flush=True)
    except Exception as e:
        print(f"Error restoring timers: {e}", flush=True)

# ==========================================
# SMART REMINDERS (with notes & DM support)
# ==========================================
active_reminders: dict[int, list[dict]] = {}
REMINDERS_FILE = "reminders.json"

def save_reminders():
    """Saves non-expired reminders to disk."""
    data = {}
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for user_id, reminder_list in active_reminders.items():
        saved_list = []
        for r in reminder_list:
            if r["end_timestamp"] > now_ts:
                saved_list.append({
                    "id": r["id"],
                    "duration_str": r["duration_str"],
                    "note": r["note"],
                    "end_timestamp": r["end_timestamp"],
                    "channel_id": r["channel_id"],
                    "send_dm": r.get("send_dm", False)
                })
        if saved_list:
            data[str(user_id)] = saved_list
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

async def run_reminder(user_id: int, rem: dict):
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    remaining = rem["end_timestamp"] - now_ts
    if remaining > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

    embed = discord.Embed(
        title="🔔 Reminder Alert!",
        description=f"**Note:** {rem['note']}\n*(Set for {rem['duration_str']})*",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    delivered = False
    if rem.get("send_dm", False):
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if user:
                await user.send(embed=embed)
                delivered = True
                print(f"Reminder delivered via DM to user {user_id}", flush=True)
        except Exception as e:
            print(f"Could not deliver reminder via DM, falling back to channel: {e}", flush=True)

    if not delivered:
        try:
            channel = bot.get_channel(rem["channel_id"]) or await bot.fetch_channel(rem["channel_id"])
            if channel:
                await channel.send(content=f"<@{user_id}>", embed=embed)
                print(f"Reminder delivered to channel {rem['channel_id']} for user {user_id}", flush=True)
        except Exception as e:
            print(f"Error alerting reminder in channel: {e}", flush=True)

    if user_id in active_reminders:
        active_reminders[user_id] = [r for r in active_reminders[user_id] if r["id"] != rem["id"]]
        if not active_reminders[user_id]:
            del active_reminders[user_id]
    save_reminders()

def load_saved_reminders():
    """Restores saved reminders upon startup."""
    if not os.path.exists(REMINDERS_FILE):
        return
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uid_str, rem_list in data.items():
            uid = int(uid_str)
            active_reminders[uid] = []
            for item in rem_list:
                item["task"] = asyncio.create_task(run_reminder(uid, item))
                active_reminders[uid].append(item)
        if active_reminders:
            print(f"Restored active reminders for {len(active_reminders)} user(s).", flush=True)
    except Exception as e:
        print(f"Error restoring reminders: {e}", flush=True)

# ==========================================
# MUSIC SYSTEM & INTERACTIVE CONTROLLER
# ==========================================
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

music_queues: dict[int, list[dict]] = {}
current_songs: dict[int, dict] = {}
guild_volumes: dict[int, float] = {}

def get_audio_info(query: str) -> dict | None:
    """Extracts streamable audio info using yt-dlp."""
    try:
        info = ytdl.extract_info(query, download=False)
        if not info:
            return None
        if "entries" in info:
            entry = info["entries"][0]
        else:
            entry = info
        return {
            "title": entry.get("title", "Unknown Title"),
            "url": entry.get("url"),
            "webpage_url": entry.get("webpage_url", query),
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail"),
        }
    except Exception as e:
        print(f"yt-dlp extract error: {e}", flush=True)
        return None

class MusicControlView(discord.ui.View):
    """Interactive Discord buttons for music playback control."""
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Pause/Resume", emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="m_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("❌ The bot is not in a voice channel.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Playback paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Playback resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="m_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭️ Skipped current song.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="m_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = music_queues.get(interaction.guild.id, [])
        if len(queue) > 1:
            random.shuffle(queue)
            await interaction.response.send_message(f"🔀 Shuffled **{len(queue)}** songs in the queue!", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Need at least 2 songs in the queue to shuffle.", ephemeral=True)

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="m_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = current_songs.get(interaction.guild.id)
        queue = music_queues.get(interaction.guild.id, [])
        if not current and not queue:
            await interaction.response.send_message("ℹ️ The music queue is currently empty.", ephemeral=True)
            return

        embed = discord.Embed(title="🎶 Current Music Queue", color=discord.Color.purple())
        if current:
            embed.add_field(
                name="Now Playing",
                value=f"[{current['title']}]({current['webpage_url']}) (Req by {current['requester']})",
                inline=False
            )
        if queue:
            q_text = "\n".join(
                f"**{i}.** [{s['title']}]({s['webpage_url']}) — {s['requester']}"
                for i, s in enumerate(queue[:8], 1)
            )
            if len(queue) > 8:
                q_text += f"\n*...and {len(queue) - 8} more*"
            embed.add_field(name="Up Next", value=q_text, inline=False)
        else:
            embed.add_field(name="Up Next", value="*No more songs in queue*", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="m_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            music_queues[interaction.guild.id] = []
            current_songs.pop(interaction.guild.id, None)
            await vc.disconnect()
            await interaction.response.send_message("⏹️ Playback stopped and left voice channel.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ The bot is not in a voice channel.", ephemeral=True)

    @discord.ui.button(label="Vol -", emoji="🔉", style=discord.ButtonStyle.secondary, row=1, custom_id="m_vol_down")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_vol = guild_volumes.get(interaction.guild.id, 0.8)
        new_vol = max(0.1, round(current_vol - 0.1, 1))
        guild_volumes[interaction.guild.id] = new_vol
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = new_vol
        await interaction.response.send_message(f"🔉 Volume decreased to **{int(new_vol * 100)}%**", ephemeral=True)

    @discord.ui.button(label="Vol +", emoji="🔊", style=discord.ButtonStyle.secondary, row=1, custom_id="m_vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_vol = guild_volumes.get(interaction.guild.id, 0.8)
        new_vol = min(1.5, round(current_vol + 0.1, 1))
        guild_volumes[interaction.guild.id] = new_vol
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = new_vol
        await interaction.response.send_message(f"🔊 Volume increased to **{int(new_vol * 100)}%**", ephemeral=True)

def play_next_song(guild: discord.Guild, channel: discord.TextChannel):
    """Plays the next song in the guild queue, or disconnects if queue is empty."""
    voice_client = guild.voice_client
    if not voice_client:
        return

    queue = music_queues.get(guild.id, [])
    if not queue:
        current_songs.pop(guild.id, None)
        print("Queue is empty, waiting for new songs.", flush=True)
        return

    song = queue.pop(0)
    current_songs[guild.id] = song

    try:
        vol = guild_volumes.get(guild.id, 0.8)
        raw_source = discord.FFmpegPCMAudio(
            song["url"],
            executable=FFMPEG_EXE,
            **FFMPEG_OPTIONS
        )
        source = discord.PCMVolumeTransformer(raw_source, volume=vol)

        def after_playing(err):
            if err:
                print(f"Playback error: {err}", flush=True)
            else:
                print(f"Finished playing: {song['title']}", flush=True)
            bot.loop.call_soon_threadsafe(play_next_song, guild, channel)

        voice_client.play(source, after=after_playing)
        print(f"Now playing audio for: {song['title']}", flush=True)

        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"[{song['title']}]({song['webpage_url']})",
            color=discord.Color.purple()
        )
        if song.get("thumbnail"):
            embed.set_thumbnail(url=song["thumbnail"])
        if song.get("duration"):
            mins, secs = divmod(song["duration"], 60)
            embed.set_footer(text=f"Duration: {mins}:{secs:02d} • Requested by {song['requester']} • Vol: {int(vol * 100)}%")

        view = MusicControlView(guild.id)
        asyncio.run_coroutine_threadsafe(channel.send(embed=embed, view=view), bot.loop)
    except Exception as e:
        print(f"Error starting playback: {e}", flush=True)
        asyncio.run_coroutine_threadsafe(
            channel.send(f"❌ Error playing **{song['title']}**: {e}"),
            bot.loop
        )
        play_next_song(guild, channel)

# ==========================================
# VOICE SOUNDBOARD & MEME AUDIO FX
# ==========================================
SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

AVAILABLE_SOUNDS = {
    "vine_boom": {"name": "Vine Boom", "emoji": "💥", "file": "vine_boom.mp3"},
    "airhorn": {"name": "MLG Airhorn", "emoji": "📢", "file": "airhorn.mp3"},
    "bruh": {"name": "Bruh", "emoji": "🗿", "file": "bruh.mp3"},
    "metal_pipe": {"name": "Metal Pipe", "emoji": "🥢", "file": "metal_pipe.mp3"},
    "sad_violin": {"name": "Sad Violin", "emoji": "🎻", "file": "sad_violin.mp3"},
    "drumroll": {"name": "Drum Roll", "emoji": "🥁", "file": "drumroll.mp3"},
    "victory": {"name": "Victory Fanfare", "emoji": "🎺", "file": "victory.mp3"},
    "sheesh": {"name": "Sheesh", "emoji": "🥶", "file": "sheesh.mp3"},
}

async def play_sound_effect(guild: discord.Guild, sound_key: str, voice_channel: discord.VoiceChannel) -> bool:
    """Plays a short sound effect in voice, pausing active music temporarily and resuming after."""
    sound_data = AVAILABLE_SOUNDS.get(sound_key)
    if not sound_data:
        return False

    file_path = os.path.join(SOUNDS_DIR, sound_data["file"])
    if not os.path.exists(file_path):
        return False

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        vc = await voice_channel.connect(reconnect=True)
        for _ in range(15):
            if vc._connection and vc._connection.can_encrypt:
                break
            await asyncio.sleep(0.4)
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    was_playing = vc.is_playing()
    if was_playing:
        vc.pause()

    def after_sound(err):
        if was_playing and vc.is_connected() and not vc.is_playing():
            vc.resume()

    vol = guild_volumes.get(guild.id, 0.8)
    raw = discord.FFmpegPCMAudio(file_path, executable=FFMPEG_EXE)
    src = discord.PCMVolumeTransformer(raw, volume=min(1.2, vol + 0.1))
    vc.play(src, after=after_sound)
    return True

class SoundSelectDropdown(discord.ui.Select):
    """Dropdown selector for all available soundboard audio clips."""
    def __init__(self):
        options = [
            discord.SelectOption(
                label=data["name"],
                value=key,
                emoji=data["emoji"],
                description=f"Play {data['name']} effect"
            )
            for key, data in AVAILABLE_SOUNDS.items()
        ]
        super().__init__(placeholder="🔊 Select a sound to play in voice...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ You must be in a voice channel to play sounds!", ephemeral=True)
            return

        sound_key = self.values[0]
        sound_data = AVAILABLE_SOUNDS.get(sound_key)
        if not sound_data:
            await interaction.response.send_message("❌ Sound effect not found.", ephemeral=True)
            return

        success = await play_sound_effect(interaction.guild, sound_key, interaction.user.voice.channel)
        if success:
            await interaction.response.send_message(
                f"{sound_data['emoji']} Played **{sound_data['name']}** in {interaction.user.voice.channel.name}!",
                ephemeral=False
            )
        else:
            await interaction.response.send_message("❌ Failed to play sound effect.", ephemeral=True)

class SoundboardView(discord.ui.View):
    """Soundboard interactive view containing dropdown and quick buttons."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SoundSelectDropdown())

    @discord.ui.button(label="Vine Boom", emoji="💥", style=discord.ButtonStyle.danger, row=1)
    async def btn_vine_boom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play_quick(interaction, "vine_boom")

    @discord.ui.button(label="MLG Airhorn", emoji="📢", style=discord.ButtonStyle.primary, row=1)
    async def btn_airhorn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play_quick(interaction, "airhorn")

    @discord.ui.button(label="Bruh", emoji="🗿", style=discord.ButtonStyle.secondary, row=1)
    async def btn_bruh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play_quick(interaction, "bruh")

    @discord.ui.button(label="Metal Pipe", emoji="🥢", style=discord.ButtonStyle.secondary, row=1)
    async def btn_metal_pipe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play_quick(interaction, "metal_pipe")

    async def _play_quick(self, interaction: discord.Interaction, sound_key: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ You must be in a voice channel to use the soundboard!", ephemeral=True)
            return
        sound_data = AVAILABLE_SOUNDS[sound_key]
        success = await play_sound_effect(interaction.guild, sound_key, interaction.user.voice.channel)
        if success:
            await interaction.response.send_message(
                f"{sound_data['emoji']} Played **{sound_data['name']}**!",
                ephemeral=False
            )
        else:
            await interaction.response.send_message("❌ Failed to play sound effect.", ephemeral=True)

# ==========================================
# EVENTS & COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"\n=======================================================", flush=True)
    print(f"Logged in as: {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"Opus Loaded: {discord.opus.is_loaded()}", flush=True)
    print(f"=======================================================", flush=True)

    load_saved_timers()
    load_saved_reminders()

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s) globally: {[c.name for c in synced]}", flush=True)
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Synced commands to guild '{guild.name}' ({guild.id})", flush=True)
        print("Ready for timers, reminders, music, and soundboard!", flush=True)
    except Exception as e:
        print(f"Failed to sync slash commands: {e}", flush=True)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Automatically disconnects bot if left alone in voice channel for over 60 seconds."""
    guild = member.guild
    vc = guild.voice_client
    if not vc or not vc.channel:
        return

    # Check if there are any humans left in the voice channel
    humans = [m for m in vc.channel.members if not m.bot]
    if len(humans) == 0:
        await asyncio.sleep(60)
        # Re-check after 60 seconds
        if vc.is_connected() and vc.channel:
            humans_now = [m for m in vc.channel.members if not m.bot]
            if len(humans_now) == 0:
                music_queues[guild.id] = []
                current_songs.pop(guild.id, None)
                await vc.disconnect()
                print(f"Auto-disconnected from {vc.channel.name} due to inactivity.", flush=True)

# --- TIMER COMMANDS ---
@bot.tree.command(name="timer", description="Start a countdown timer (e.g. 15m30s, 1h, 45s).")
@app_commands.describe(duration="The duration for the timer (e.g. 15m30s, 1h, 45s).")
async def timer_command(interaction: discord.Interaction, duration: str):
    seconds = parse_duration(duration)
    if seconds is None or seconds <= 0:
        await reply_safe(interaction, "❌ **Invalid duration!** Use formats like `/timer 15m30s` or `/timer 1h`.", ephemeral=True)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    end_time = now + datetime.timedelta(seconds=seconds)
    end_timestamp = end_time.timestamp()

    timer_id = f"{int(now.timestamp())}_{len(active_timers.get(interaction.user.id, [])) + 1}"
    timer_data = {
        "id": timer_id,
        "duration_str": duration,
        "end_timestamp": end_timestamp,
        "channel_id": interaction.channel_id,
        "task": None
    }

    task = asyncio.create_task(run_timer(interaction.user.id, timer_data))
    timer_data["task"] = task

    if interaction.user.id not in active_timers:
        active_timers[interaction.user.id] = []
    active_timers[interaction.user.id].append(timer_data)
    save_timers()

    embed = discord.Embed(
        title="⏳ Timer Started",
        description=(
            f"Timer set for **{duration}**.\n\n"
            f"🔔 Ending **<t:{int(end_timestamp)}:R>**\n"
            f"📅 Target time: **<t:{int(end_timestamp)}:T>**"
        ),
        color=discord.Color.blue()
    )
    await reply_safe(interaction, embed=embed)
    print(f"Timer started for {interaction.user} ({duration})", flush=True)

@bot.tree.command(name="time", description="Check the remaining time on your active timers.")
async def time_command(interaction: discord.Interaction):
    user_timers = active_timers.get(interaction.user.id, [])
    if not user_timers:
        await reply_safe(interaction, "ℹ️ You have no active timers right now.", ephemeral=True)
        return

    embed = discord.Embed(title="⏱️ Your Active Timers", color=discord.Color.gold())
    for idx, t in enumerate(user_timers, 1):
        end_ts = int(t["end_timestamp"])
        embed.add_field(
            name=f"Timer #{idx} ({t['duration_str']})",
            value=f"• Ends: <t:{end_ts}:R>\n• Target: <t:{end_ts}:T>",
            inline=False
        )
    await reply_safe(interaction, embed=embed)

@bot.tree.command(name="cancel_timer", description="Cancel your active timer(s).")
async def cancel_timer_command(interaction: discord.Interaction):
    user_timers = active_timers.get(interaction.user.id, [])
    if not user_timers:
        await reply_safe(interaction, "ℹ️ You have no active timers to cancel.", ephemeral=True)
        return

    count = len(user_timers)
    for t in user_timers:
        if t.get("task") and not t["task"].done():
            t["task"].cancel()
    del active_timers[interaction.user.id]
    save_timers()
    await reply_safe(interaction, f"✅ Cancelled **{count}** active timer(s).", ephemeral=True)

# --- SMART REMINDER COMMANDS ---
@bot.tree.command(name="remind", description="Set a reminder with a custom note and optional DM notification.")
@app_commands.describe(
    duration="When to remind you (e.g. 15m, 2h30m, 1d).",
    note="What you want to be reminded about.",
    dm="Whether to send the reminder via direct message (default: false)."
)
async def remind_command(interaction: discord.Interaction, duration: str, note: str, dm: bool = False):
    seconds = parse_duration(duration)
    if seconds is None or seconds <= 0:
        await reply_safe(interaction, "❌ **Invalid duration!** Use formats like `/remind duration:2h30m note:Check oven`.", ephemeral=True)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    end_time = now + datetime.timedelta(seconds=seconds)
    end_timestamp = end_time.timestamp()

    rem_id = f"{int(now.timestamp())}_{len(active_reminders.get(interaction.user.id, [])) + 1}"
    rem_data = {
        "id": rem_id,
        "duration_str": duration,
        "note": note,
        "end_timestamp": end_timestamp,
        "channel_id": interaction.channel_id,
        "send_dm": dm,
        "task": None
    }

    task = asyncio.create_task(run_reminder(interaction.user.id, rem_data))
    rem_data["task"] = task

    if interaction.user.id not in active_reminders:
        active_reminders[interaction.user.id] = []
    active_reminders[interaction.user.id].append(rem_data)
    save_reminders()

    delivery_text = "📬 Direct Message (DM)" if dm else f"💬 <#{interaction.channel_id}>"
    embed = discord.Embed(
        title="🔔 Reminder Scheduled",
        description=(
            f"**Note:** {note}\n\n"
            f"⏱️ **Duration:** {duration}\n"
            f"🔔 **Alerting:** <t:{int(end_timestamp)}:R> (<t:{int(end_timestamp)}:T>)\n"
            f"📍 **Delivery:** {delivery_text}"
        ),
        color=discord.Color.teal()
    )
    await reply_safe(interaction, embed=embed)
    print(f"Reminder set for {interaction.user} ({duration}): {note}", flush=True)

@bot.tree.command(name="reminders", description="View all your active scheduled reminders.")
async def list_reminders_command(interaction: discord.Interaction):
    user_rems = active_reminders.get(interaction.user.id, [])
    if not user_rems:
        await reply_safe(interaction, "ℹ️ You have no active reminders right now.", ephemeral=True)
        return

    embed = discord.Embed(title="📋 Your Active Reminders", color=discord.Color.teal())
    for idx, r in enumerate(user_rems, 1):
        end_ts = int(r["end_timestamp"])
        delivery = "DM" if r.get("send_dm") else f"<#{r['channel_id']}>"
        embed.add_field(
            name=f"#{idx}: {r['note'][:40]}",
            value=f"• Due: <t:{end_ts}:R> (<t:{end_ts}:T>)\n• Delivery: {delivery}\n• ID: `{r['id']}`",
            inline=False
        )
    await reply_safe(interaction, embed=embed)

@bot.tree.command(name="cancel_reminder", description="Cancel one or all of your active reminders.")
@app_commands.describe(reminder_id="Optional: specific reminder ID to cancel (leave empty to cancel all).")
async def cancel_reminder_command(interaction: discord.Interaction, reminder_id: str | None = None):
    user_rems = active_reminders.get(interaction.user.id, [])
    if not user_rems:
        await reply_safe(interaction, "ℹ️ You have no active reminders to cancel.", ephemeral=True)
        return

    if reminder_id:
        target = next((r for r in user_rems if r["id"] == reminder_id), None)
        if not target:
            await reply_safe(interaction, f"❌ Reminder with ID `{reminder_id}` was not found.", ephemeral=True)
            return
        if target.get("task") and not target["task"].done():
            target["task"].cancel()
        active_reminders[interaction.user.id] = [r for r in user_rems if r["id"] != reminder_id]
        save_reminders()
        await reply_safe(interaction, f"✅ Cancelled reminder: **{target['note']}**", ephemeral=True)
    else:
        count = len(user_rems)
        for r in user_rems:
            if r.get("task") and not r["task"].done():
                r["task"].cancel()
        del active_reminders[interaction.user.id]
        save_reminders()
        await reply_safe(interaction, f"✅ Cancelled all **{count}** active reminder(s).", ephemeral=True)

# --- MUSIC COMMANDS ---
@bot.tree.command(name="play", description="Play audio from a YouTube link or search query in voice.")
@app_commands.describe(query="YouTube URL or song name to search.")
async def play_command(interaction: discord.Interaction, query: str):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
    except Exception as e:
        print(f"Notice: Initial defer failed: {e}", flush=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        await reply_safe(interaction, "❌ You must be in a voice channel to use this command!")
        return

    try:
        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        perms = voice_channel.permissions_for(interaction.guild.me)
        if not perms.connect:
            await reply_safe(interaction, f"❌ I don't have permission to **Connect** to {voice_channel.name}!")
            return
        if not perms.speak:
            await reply_safe(interaction, f"❌ I don't have permission to **Speak** in {voice_channel.name}! Please allow Speak in channel permissions.")
            return

        if not voice_client or not voice_client.is_connected():
            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass
            voice_client = await voice_channel.connect(reconnect=True)
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        for _ in range(15):
            if voice_client._connection and voice_client._connection.can_encrypt:
                break
            await asyncio.sleep(0.4)

        if not voice_client._connection.can_encrypt:
            print("Notice: can_encrypt is False after initial wait, refreshing voice connection...", flush=True)
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass
            voice_client = await voice_channel.connect(reconnect=True)
            for _ in range(15):
                if voice_client._connection and voice_client._connection.can_encrypt:
                    break
                await asyncio.sleep(0.4)

        song_info = await bot.loop.run_in_executor(None, lambda: get_audio_info(query))
        if not song_info or not song_info.get("url"):
            await reply_safe(interaction, "❌ Could not find or extract audio from that query.")
            return

        song_info["requester"] = interaction.user.display_name

        if interaction.guild.id not in music_queues:
            music_queues[interaction.guild.id] = []

        if voice_client.is_playing() or voice_client.is_paused():
            music_queues[interaction.guild.id].append(song_info)
            embed = discord.Embed(
                title="📋 Added to Queue",
                description=f"[{song_info['title']}]({song_info['webpage_url']})",
                color=discord.Color.teal()
            )
            if song_info.get("thumbnail"):
                embed.set_thumbnail(url=song_info["thumbnail"])
            embed.set_footer(text=f"Position #{len(music_queues[interaction.guild.id])} in queue")
            await reply_safe(interaction, embed=embed)
        else:
            music_queues[interaction.guild.id].append(song_info)
            await reply_safe(interaction, content="🎶 Starting playback...")
            play_next_song(interaction.guild, interaction.channel)
    except Exception as e:
        print(f"Error in play_command: {e}", flush=True)
        await reply_safe(interaction, content=f"❌ Failed to play: {e}")

@bot.tree.command(name="controller", description="Display the interactive Music Controller buttons.")
async def controller_command(interaction: discord.Interaction):
    current = current_songs.get(interaction.guild.id)
    vol = guild_volumes.get(interaction.guild.id, 0.8)

    embed = discord.Embed(
        title="🎛️ Music Controller Panel",
        color=discord.Color.purple()
    )
    if current:
        embed.description = f"**Now Playing:** [{current['title']}]({current['webpage_url']})\nRequested by: {current['requester']}"
        if current.get("thumbnail"):
            embed.set_thumbnail(url=current["thumbnail"])
    else:
        embed.description = "*Nothing currently playing. Use `/play` to start!*"

    embed.set_footer(text=f"Current Volume: {int(vol * 100)}%")
    view = MusicControlView(interaction.guild.id)
    await reply_safe(interaction, embed=embed, view=view)

@bot.tree.command(name="pause", description="Pause current music playback.")
async def pause_command(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await reply_safe(interaction, "⏸️ Playback paused.")
    else:
        await reply_safe(interaction, "❌ Nothing is currently playing.", ephemeral=True)

@bot.tree.command(name="resume", description="Resume paused music playback.")
async def resume_command(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await reply_safe(interaction, "▶️ Playback resumed.")
    else:
        await reply_safe(interaction, "❌ Music is not paused.", ephemeral=True)

@bot.tree.command(name="skip", description="Skip the currently playing song.")
async def skip_command(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await reply_safe(interaction, "⏭️ Skipped current song.")
    else:
        await reply_safe(interaction, "❌ Nothing is playing right now.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop music, clear the queue, and leave voice.")
async def stop_command(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        music_queues[interaction.guild.id] = []
        current_songs.pop(interaction.guild.id, None)
        await vc.disconnect()
        await reply_safe(interaction, "⏹️ Stopped playback and left the voice channel.")
    else:
        await reply_safe(interaction, "❌ The bot is not in a voice channel.", ephemeral=True)

@bot.tree.command(name="queue", description="Display the current song and upcoming music queue.")
async def queue_command(interaction: discord.Interaction):
    current = current_songs.get(interaction.guild.id)
    queue = music_queues.get(interaction.guild.id, [])

    if not current and not queue:
        await reply_safe(interaction, "ℹ️ The music queue is currently empty.", ephemeral=True)
        return

    embed = discord.Embed(title="🎶 Music Queue", color=discord.Color.purple())
    if current:
        embed.add_field(
            name="Now Playing",
            value=f"[{current['title']}]({current['webpage_url']}) (Requested by {current['requester']})",
            inline=False
        )

    if queue:
        queue_text = "\n".join(
            f"**{i}.** [{s['title']}]({s['webpage_url']}) — {s['requester']}"
            for i, s in enumerate(queue[:10], 1)
        )
        if len(queue) > 10:
            queue_text += f"\n*...and {len(queue) - 10} more*"
        embed.add_field(name="Up Next", value=queue_text, inline=False)
    else:
        embed.add_field(name="Up Next", value="*No more songs in queue*", inline=False)

    await reply_safe(interaction, embed=embed)

# --- SOUNDBOARD COMMANDS ---
@bot.tree.command(name="soundboard", description="Open the interactive meme soundboard panel.")
async def soundboard_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔊 Voice Soundboard",
        description=(
            "Click a button below or select a meme sound from the dropdown menu to play it in your voice channel!\n\n"
            "✨ *If music is currently playing, it will pause for the sound effect and automatically resume!*"
        ),
        color=discord.Color.orange()
    )
    view = SoundboardView()
    await reply_safe(interaction, embed=embed, view=view)

@bot.tree.command(name="sound", description="Play a meme sound effect in your voice channel.")
@app_commands.describe(name="The sound effect to play.")
@app_commands.choices(
    name=[
        app_commands.Choice(name=f"{v['emoji']} {v['name']}", value=k)
        for k, v in AVAILABLE_SOUNDS.items()
    ]
)
async def sound_command(interaction: discord.Interaction, name: app_commands.Choice[str]):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await reply_safe(interaction, "❌ You must be in a voice channel to use this command!", ephemeral=True)
        return

    sound_key = name.value
    sound_data = AVAILABLE_SOUNDS[sound_key]
    voice_channel = interaction.user.voice.channel

    try:
        success = await play_sound_effect(interaction.guild, sound_key, voice_channel)
        if success:
            await reply_safe(
                interaction,
                f"{sound_data['emoji']} Played **{sound_data['name']}** in {voice_channel.name}!"
            )
        else:
            await reply_safe(interaction, "❌ Failed to play sound effect.", ephemeral=True)
    except Exception as e:
        print(f"Error in sound_command: {e}", flush=True)
        await reply_safe(interaction, f"❌ Error: {e}", ephemeral=True)

if __name__ == "__main__":
    if not acquire_single_instance_lock():
        print(f"\n=======================================================", flush=True)
        print(f"[ERROR] Another instance of the bot is already running!", flush=True)
        print(f"Check {LOCK_FILE} or Task Manager for existing python processes.", flush=True)
        print(f"Aborting startup to prevent duplicate interaction conflicts.", flush=True)
        print(f"=======================================================\n", flush=True)
        sys.exit(1)

    try:
        bot.run(token)
    finally:
        _release_lock()
