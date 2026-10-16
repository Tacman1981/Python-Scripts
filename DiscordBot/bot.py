import os
import sys
import math
import asyncio
import logging
import shutil
from typing import Optional
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# Suppress the prefix message_content warning since the bot uses Slash Commands
logging.getLogger("discord.ext.commands.bot").setLevel(logging.ERROR)

from music import MusicManager, Song
from playlist_manager import PlaylistStorage

# Force UTF-8 stdout on Windows to prevent UnicodeEncodeError with emojis/titles
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Ensure Opus audio library is loaded for Discord voice (cross-platform Windows & Linux)
def load_opus():
    if discord.opus.is_loaded():
        return True
    try:
        if discord.opus._load_default():
            print("✅ Opus library loaded via default loader.")
            return True
    except Exception:
        pass

    try:
        import ctypes.util
        lib = ctypes.util.find_library("opus")
        if lib:
            discord.opus.load_opus(lib)
            if discord.opus.is_loaded():
                print(f"✅ Opus library loaded via ctypes find_library: {lib}")
                return True
    except Exception:
        pass

    common_linux_paths = [
        "libopus.so.0",
        "libopus.so",
        "libopus.so.0.8.0",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib64/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/usr/local/lib/libopus.so",
    ]
    for p in common_linux_paths:
        try:
            discord.opus.load_opus(p)
            if discord.opus.is_loaded():
                print(f"✅ Opus library loaded from path: {p}")
                return True
        except Exception:
            continue

    print("⚠️ WARNING: Opus library is NOT loaded!")
    print("   On Linux, install it with: sudo apt install -y libopus0 libopus-dev (or sudo dnf install opus)")
    return False

load_opus()

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Server Whitelist Safeguard:
raw_allowed_guilds = os.getenv("GUILD_ID", "") or os.getenv("ALLOWED_GUILD_IDS", "")
ALLOWED_GUILD_IDS = [int(g.strip()) for g in raw_allowed_guilds.split(",") if g.strip().isdigit()]

# Voice Channel Auto-Join Configuration (Optional: set in .env or auto-detected by name):
raw_channel = os.getenv("AUTO_JOIN_VOICE_CHANNEL_ID", "").strip()
AUTO_JOIN_VOICE_CHANNEL_ID = int(raw_channel) if raw_channel.isdigit() else None

# Pure Slash Commands setup — requires ZERO privileged intents!
intents = discord.Intents.default()

bot = commands.Bot(command_prefix=(), intents=intents, help_command=None)
server_playlist = PlaylistStorage()
music_manager = MusicManager(bot, playlist_storage=server_playlist)


@bot.event
async def on_ready():
    print(f"==================================================")
    print(f"  DJ Pete Bot is online! Logged in as: {bot.user}")
    print(f"  Guild count: {len(bot.guilds)}")
    print(f"  Slash Commands: Ready (Zero Privileged Intents)")
    if ALLOWED_GUILD_IDS:
        print(f"  🔒 Server Whitelist: Active ({len(ALLOWED_GUILD_IDS)} allowed server(s))")
    else:
        print(f"  ℹ️ Server Whitelist: Open (Set GUILD_ID in .env to lock)")
    print(f"==================================================")

    # Safeguard 1: Leave any unauthorized servers on startup
    for guild in list(bot.guilds):
        if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
            print(f"  🚨 Leaving unauthorized server: {guild.name} ({guild.id})")
            await guild.leave()

    # Purge old global commands from Discord API to eliminate duplicate slash commands
    try:
        if bot.application_id:
            await bot.http.bulk_upsert_global_commands(bot.application_id, [])
            print("  🧹 Purged old global commands (eliminated duplicate slash commands).")
    except Exception as e:
        print(f"  Notice purging global commands: {e}")

    # Sync clean command list directly to authorized server(s)
    for guild in bot.guilds:
        if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
            continue
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"  ⚡ Clean synced {len(synced)} slash commands to: {guild.name}")
        except Exception as e:
            print(f"  ⚠️ Guild sync error for {guild.name}: {e}")

    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening,
        name=f"/play | /playlist list"
    ))

    # Silently auto-join designated voice channel on authorized servers
    for guild in bot.guilds:
        if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
            continue
        try:
            await auto_join_channel(guild)
        except Exception as e:
            print(f"  ⚠️ Error auto-joining voice in {guild.name}: {e}")

    # Start voice watchdog to guarantee 24/7 uptime and handle network drops
    if not voice_watchdog.is_running():
        voice_watchdog.start()


def get_auto_join_channel(guild: discord.Guild) -> Optional[discord.VoiceChannel]:
    """Finds the designated voice channel to auto-join silently."""
    if AUTO_JOIN_VOICE_CHANNEL_ID:
        ch = guild.get_channel(AUTO_JOIN_VOICE_CHANNEL_ID)
        if isinstance(ch, discord.VoiceChannel):
            return ch

    # Fallback 1: Look for channel named 'radio' (case-insensitive)
    for vc in guild.voice_channels:
        if "radio" in vc.name.lower():
            return vc

    # Fallback 2: Look for 'hangout', 'music', or 'stream'
    for vc in guild.voice_channels:
        if any(keyword in vc.name.lower() for keyword in ("hangout", "music", "stream")):
            return vc

    # Fallback 3: First accessible voice channel
    for vc in guild.voice_channels:
        perms = vc.permissions_for(guild.me)
        if perms.connect:
            return vc

    return None


async def start_server_playlist_internal(guild: discord.Guild, start_track: Optional[int] = None, requester: Optional[discord.abc.User] = None):
    """Starts the server playlist without requiring an active slash command context."""
    player = music_manager.get_player(guild)
    saved_tracks = server_playlist.load_tracks()
    if not saved_tracks:
        return False

    req = requester or bot.user
    songs = [
        Song(
            title=t["title"],
            webpage_url=t["webpage_url"],
            duration=t.get("duration"),
            thumbnail=t.get("thumbnail"),
            requester=req
        )
        for t in saved_tracks
    ]

    initial_song = songs[start_track - 1] if (start_track is not None and 1 <= start_track <= len(songs)) else None
    is_currently_playing = player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused())

    player.add_tracks(songs, is_playlist=True, initial_track=initial_song)
    player.true_random = True
    player.loop_single = False

    if initial_song and is_currently_playing:
        player.skip()

    return True


def count_human_listeners(channel: Optional[discord.VoiceChannel]) -> int:
    """Accurately counts human listeners without requiring privileged member intent cache."""
    if not channel:
        return 0
    bot_id = bot.user.id if bot.user else None
    return sum(
        1 for uid in channel.voice_states.keys()
        if uid != bot_id and not getattr(channel.guild.get_member(uid), "bot", False)
    )


async def auto_join_channel(guild: discord.Guild):
    """Silently connects to the designated voice channel on startup or recovery."""
    target_channel = get_auto_join_channel(guild)
    if not target_channel:
        return

    player = music_manager.get_player(guild)
    if getattr(player, "manual_leave", False):
        return

    voice_client = guild.voice_client
    try:
        if not voice_client or not voice_client.is_connected():
            print(f"  🔊 Silently auto-joining voice channel: '{target_channel.name}' ({target_channel.id})")
            voice_client = await target_channel.connect()
            await asyncio.sleep(0.5)
        elif voice_client.channel != target_channel:
            print(f"  🔊 Moving silently to voice channel: '{target_channel.name}' ({target_channel.id})")
            await voice_client.move_to(target_channel)
            await asyncio.sleep(0.3)
    except Exception as e:
        print(f"  ⚠️ Voice auto-join error in {guild.name}: {e}")
        return

    player.voice_client = voice_client
    if not player.text_channel:
        player.text_channel = target_channel

    # Check if listeners are already in the channel using voice_states
    listeners_count = count_human_listeners(target_channel)
    if listeners_count > 0:
        if not (voice_client.is_playing() or voice_client.is_paused()):
            print(f"  🎵 {listeners_count} listener(s) found in '{target_channel.name}'. Starting playback!")
            req = None
            for uid in target_channel.voice_states.keys():
                if bot.user and uid != bot.user.id:
                    m = guild.get_member(uid)
                    if m and not m.bot:
                        req = m
                        break
            await start_server_playlist_internal(guild, requester=req)
    else:
        print(f"  🤫 Channel is empty. Bot is sitting silently in '{target_channel.name}'.")


@tasks.loop(seconds=30)
async def voice_watchdog():
    """Background watchdog ensuring the bot remains connected to voice 24/7."""
    for guild in bot.guilds:
        if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
            continue
        player = music_manager.get_player(guild)
        if getattr(player, "manual_leave", False):
            continue

        target_channel = get_auto_join_channel(guild)
        if not target_channel:
            continue

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            try:
                print(f"🔄 [Watchdog] Reconnecting silently to '{target_channel.name}' on {guild.name}...")
                new_vc = await target_channel.connect()
                await asyncio.sleep(0.5)
                player.voice_client = new_vc
                if not player.text_channel:
                    player.text_channel = target_channel
                listeners_count = count_human_listeners(target_channel)
                if listeners_count > 0 and not (new_vc.is_playing() or new_vc.is_paused()):
                    req = None
                    for uid in target_channel.voice_states.keys():
                        if bot.user and uid != bot.user.id:
                            m = guild.get_member(uid)
                            if m and not m.bot:
                                req = m
                                break
                    await start_server_playlist_internal(guild, requester=req)
            except Exception as e:
                print(f"⚠️ [Watchdog] Reconnect error on {guild.name}: {e}")


@voice_watchdog.before_loop
async def before_voice_watchdog():
    await bot.wait_until_ready()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Safeguard 3: Auto-leave unauthorized servers immediately upon invite."""
    if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
        print(f"🚨 Bot was invited to unauthorized server '{guild.name}' ({guild.id}). Leaving immediately...")
        await guild.leave()
        return

    # Authorized server: sync commands
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print(f"⚡ Synced commands to authorized server: {guild.name} ({guild.id})")


@bot.check
async def block_unauthorized_guilds(ctx: commands.Context):
    """Safeguard 4: Block any commands invoked from unauthorized guilds."""
    if ALLOWED_GUILD_IDS and ctx.guild and ctx.guild.id not in ALLOWED_GUILD_IDS:
        return False
    return True


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """
    Handles automatic playback on member join and stops audio when empty.
    The bot NEVER disconnects automatically from the voice channel.
    """
    if member.bot:
        return

    guild = member.guild
    voice_client = guild.voice_client

    # If the bot is not connected but someone joined the designated auto-join channel, connect and play!
    if not voice_client or not voice_client.is_connected():
        target_channel = get_auto_join_channel(guild)
        if target_channel and after.channel == target_channel:
            player = music_manager.get_player(guild)
            player.manual_leave = False
            await auto_join_channel(guild)
        return

    bot_channel = voice_client.channel
    if not bot_channel:
        return

    player = music_manager.get_player(guild)
    if not player.text_channel:
        player.text_channel = bot_channel

    # Case 1: A human user joins or moves into the bot's channel
    if after.channel == bot_channel and before.channel != bot_channel:
        # If bot is not already playing music, start playing the server playlist!
        if not (voice_client.is_playing() or voice_client.is_paused()):
            print(f"🎵 {member.display_name} joined '{bot_channel.name}'. Starting server playlist!")
            await start_server_playlist_internal(guild, requester=member)

    # Case 2: A human user leaves or moves away from the bot's channel
    elif before.channel == bot_channel and after.channel != bot_channel:
        listeners_count = count_human_listeners(bot_channel)
        if listeners_count == 0:
            # 2-second grace period for quick reconnects or device switches
            await asyncio.sleep(2)
            if voice_client.is_connected() and voice_client.channel == bot_channel:
                remaining = count_human_listeners(bot_channel)
                if remaining == 0:
                    print(f"🔇 All listeners left '{bot_channel.name}'. Stopping playback and sitting silently.")
                    player.stop()  # Stops audio and resets track, but KEEPS voice connection intact!


async def ensure_voice(ctx: commands.Context) -> bool:
    """Ensures the author is in a voice channel and connects the bot."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You must be connected to a voice channel to use this command!")
        return False

    if not discord.opus.is_loaded():
        await ctx.send("❌ **Voice System Error:** The Opus audio library is not loaded on the host server.\n"
                       "*(On Linux, run: `sudo apt install -y libopus0 libopus-dev`)*")
        return False

    if not shutil.which("ffmpeg"):
        await ctx.send("❌ **Voice System Error:** `ffmpeg` is not installed or not found in system PATH on the host server.\n"
                       "*(On Linux, run: `sudo apt install -y ffmpeg`)*")
        return False

    user_channel = ctx.author.voice.channel
    voice_client = ctx.guild.voice_client

    if not voice_client:
        voice_client = await user_channel.connect()
        await asyncio.sleep(0.5)  # Allow Discord voice UDP handshake and NAT punch to stabilize
    elif voice_client.channel != user_channel:
        await voice_client.move_to(user_channel)
        await asyncio.sleep(0.3)

    player = music_manager.get_player(ctx.guild)
    player.manual_leave = False
    player.voice_client = voice_client
    player.text_channel = ctx.channel
    return True


async def start_server_playlist(ctx: commands.Context, start_track: Optional[int] = None):
    """Loads and starts the persistent server playlist in True Random continuous loop mode."""
    player = music_manager.get_player(ctx.guild)
    player.manual_leave = False
    saved_tracks = server_playlist.load_tracks()

    if not saved_tracks:
        await ctx.send("📭 The Server Playlist is empty!\nAdd your friend's music using `/playlist add <song/url>`.")
        return

    if start_track is not None:
        if start_track < 1 or start_track > len(saved_tracks):
            await ctx.send(f"❌ Invalid track number `{start_track}`. The playlist currently has **{len(saved_tracks)}** tracks. Use `/playlist list` to see all tracks.")
            return

    songs = [
        Song(
            title=t["title"],
            webpage_url=t["webpage_url"],
            duration=t.get("duration"),
            thumbnail=t.get("thumbnail"),
            requester=ctx.author
        )
        for t in saved_tracks
    ]

    initial_song = songs[start_track - 1] if start_track is not None else None
    is_currently_playing = player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused())

    player.add_tracks(songs, is_playlist=True, initial_track=initial_song)
    player.true_random = True
    player.loop_single = False

    if initial_song and is_currently_playing:
        player.skip()

    if initial_song:
        embed = discord.Embed(
            title=f"📻 Started Playlist at Track #{start_track}",
            description=f"Starting with: **[{initial_song.title}]({initial_song.webpage_url})** (`{initial_song.formatted_duration}`)\n"
                        f"🎲 **True Random Active:** Continuous memoryless shuffle across all **{len(songs)}** tracks afterwards!",
            color=discord.Color.gold()
        )
        if initial_song.thumbnail:
            embed.set_thumbnail(url=initial_song.thumbnail)
    else:
        embed = discord.Embed(
            title="📻 Started Server Playlist",
            description=f"Loaded **{len(songs)}** track(s) from the server's master list.\n"
                        f"🎲 **True Random Active:** Memoryless shuffle continuously across all tracks!",
            color=discord.Color.gold()
        )
    embed.set_footer(text=f"Started by {ctx.author.display_name}")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="play", description="Play a YouTube song/URL, track number, or leave empty for Server Playlist")
@app_commands.describe(query="Optional: YouTube URL, search terms, or track number from /playlist list!")
async def play(ctx: commands.Context, *, query: Optional[str] = None):
    """Plays audio from YouTube. If query is omitted or a track number, plays the persistent server playlist on continuous loop."""
    await ctx.defer()

    if not await ensure_voice(ctx):
        return

    # If no query provided, start the Server Playlist
    if not query:
        await start_server_playlist(ctx)
        return

    # If query is a track number (e.g. "/play 5"), start the playlist from that track!
    if query.strip().isdigit():
        await start_server_playlist(ctx, start_track=int(query.strip()))
        return

    player = music_manager.get_player(ctx.guild)

    try:
        songs, is_playlist, title = await music_manager.extract_query(query, ctx.author)
    except Exception as e:
        await ctx.send(f"❌ Failed to extract audio: {e}")
        return

    if not songs:
        await ctx.send("❌ Could not find any tracks matching your query.")
        return

    # Add tracks to player (playlist loops from start; single song stops when finished)
    player.add_tracks(songs, is_playlist=is_playlist)

    if is_playlist:
        embed = discord.Embed(
            title="📑 Playlist Queued & Loop Active",
            description=f"**[{title}]({query if query.startswith('http') else songs[0].webpage_url})**",
            color=discord.Color.blue()
        )
        embed.add_field(name="Total Tracks", value=str(len(songs)), inline=True)
        embed.add_field(name="Looping", value="🔁 Yes (Restarts from track 1 on end)", inline=True)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)
    else:
        song = songs[0]
        if player.current is None and len(player.queue) <= 1:
            embed = discord.Embed(
                title="▶️ Queued to Play",
                description=f"**[{song.title}]({song.webpage_url})**",
                color=discord.Color.green()
            )
            embed.add_field(name="Duration", value=song.formatted_duration, inline=True)
            embed.add_field(name="Behavior", value="⏹️ Single track (Stops when finished)", inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}")
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="📋 Added to Queue",
                description=f"**[{song.title}]({song.webpage_url})**",
                color=discord.Color.teal()
            )
            embed.add_field(name="Position in Queue", value=str(len(player.queue)), inline=True)
            embed.add_field(name="Duration", value=song.formatted_duration, inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}")
            await ctx.send(embed=embed)


# --- Dedicated Server Playlist Command Group ---
@bot.hybrid_group(name="playlist", description="Manage and play the server's master music playlist")
async def playlist(ctx: commands.Context):
    """Lists tracks in the server playlist by default."""
    if ctx.invoked_subcommand is None:
        await playlist_list(ctx)


@playlist.command(name="play", description="Start playing the server playlist on continuous loop")
@app_commands.describe(track="Optional track number from /playlist list to start playing immediately")
async def playlist_play(ctx: commands.Context, track: Optional[int] = None):
    await ctx.defer()
    if not await ensure_voice(ctx):
        return
    await start_server_playlist(ctx, start_track=track)


def is_admin_user(author: discord.User | discord.Member, guild: discord.Guild) -> bool:
    """Checks if the user has Administrator, Manage Server, or is the server owner."""
    if not isinstance(author, discord.Member):
        return False
    if author.id == guild.owner_id:
        return True
    return author.guild_permissions.administrator or author.guild_permissions.manage_guild


@playlist.command(name="add", description="[Admin Only] Add a song or YouTube playlist to the master playlist")
@app_commands.describe(query="YouTube video/playlist URL or search query to add")
@app_commands.default_permissions(manage_guild=True)
async def playlist_add(ctx: commands.Context, *, query: str):
    if not is_admin_user(ctx.author, ctx.guild):
        await ctx.send("⛔ **Admin Only:** Only server administrators can add songs to the Server Master Playlist.", ephemeral=True)
        return

    await ctx.defer()
    try:
        songs, is_pl, title = await music_manager.extract_query(query, ctx.author)
    except Exception as e:
        await ctx.send(f"❌ Failed to extract audio: {e}")
        return

    if not songs:
        await ctx.send("❌ No tracks found matching that query.")
        return

    if is_pl:
        new_tracks = [
            {
                "title": s.title,
                "webpage_url": s.webpage_url,
                "duration": s.duration,
                "thumbnail": s.thumbnail,
                "added_by": ctx.author.display_name
            }
            for s in songs
        ]
        total = server_playlist.add_tracks_bulk(new_tracks)
        music_manager.get_player(ctx.guild).sync_with_storage()
        embed = discord.Embed(
            title="✅ Added to Server Playlist",
            description=f"Added **{len(songs)}** tracks from playlist **[{title}]({query})**.\nTotal tracks in server playlist: **{total}**",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
    else:
        song = songs[0]
        total = server_playlist.add_track(
            title=song.title,
            webpage_url=song.webpage_url,
            duration=song.duration,
            thumbnail=song.thumbnail,
            added_by=ctx.author.display_name
        )
        music_manager.get_player(ctx.guild).sync_with_storage()
        embed = discord.Embed(
            title="✅ Added to Server Playlist",
            description=f"**[{song.title}]({song.webpage_url})** (`{song.formatted_duration}`)\n"
                        f"Track **#{total}** in the master playlist.",
            color=discord.Color.green()
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await ctx.send(embed=embed)


class PlaylistPaginationView(discord.ui.View):
    """Interactive paginated view for the master server playlist."""
    def __init__(self, tracks: list, page: int = 1, per_page: int = 10, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.tracks = tracks
        self.per_page = per_page
        self.total_pages = max(1, math.ceil(len(tracks) / per_page))
        self.current_page = max(1, min(page, self.total_pages))
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_button.disabled = (self.current_page <= 1)
        self.next_button.disabled = (self.current_page >= self.total_pages)

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎶 Server Master Playlist",
            color=discord.Color.purple()
        )
        start_idx = (self.current_page - 1) * self.per_page
        page_tracks = self.tracks[start_idx : start_idx + self.per_page]

        track_lines = []
        for i, t in enumerate(page_tracks, start=start_idx + 1):
            dur = "Unknown"
            if t.get("duration"):
                mins, secs = divmod(int(t["duration"]), 60)
                dur = f"{mins}:{secs:02d}"
            title = t.get("title", "Unknown Track")
            url = t.get("webpage_url", "")
            added = t.get("added_by", "Unknown")
            track_lines.append(f"`{i}.` [{title}]({url}) (`{dur}`) - *added by {added}*")

        desc = f"Total: **{len(self.tracks)}** song(s)\n\n" + "\n".join(track_lines)
        embed.description = desc[:4000]
        embed.set_footer(text=f"Page {self.current_page} of {self.total_pages} | Use '/playlist play' to start!")
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            self._sync_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            self._sync_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)


@playlist.command(name="list", description="Show all songs currently saved in the server playlist with pagination")
@app_commands.describe(page="Optional: Page number to jump to (10 songs per page)")
async def playlist_list(ctx: commands.Context, page: int = 1):
    tracks = server_playlist.load_tracks()
    if not tracks:
        await ctx.send("📭 The Server Playlist is empty!\nAdmins can use `/playlist add <song/url>` to add tracks.")
        return

    view = PlaylistPaginationView(tracks, page=page)
    embed = view.get_embed()
    if view.total_pages > 1:
        await ctx.send(embed=embed, view=view)
    else:
        await ctx.send(embed=embed)


@playlist.command(name="remove", description="[Admin Only] Remove a track from the server playlist by its number")
@app_commands.describe(number="The track number to remove (see /playlist list)")
@app_commands.default_permissions(manage_guild=True)
async def playlist_remove(ctx: commands.Context, number: int):
    if not is_admin_user(ctx.author, ctx.guild):
        await ctx.send("⛔ **Admin Only:** Only server administrators can remove songs from the Server Master Playlist.", ephemeral=True)
        return

    removed = server_playlist.remove_track(number)
    if removed:
        music_manager.get_player(ctx.guild).sync_with_storage()
        await ctx.send(f"🗑️ Removed track `{number}`: **{removed['title']}** from the server playlist.")
    else:
        await ctx.send(f"❌ Invalid track number `{number}`. Check `/playlist list` for track numbers.")


@playlist.command(name="clear", description="[Admin Only] Clear all songs from the server playlist")
@app_commands.default_permissions(manage_guild=True)
async def playlist_clear(ctx: commands.Context):
    if not is_admin_user(ctx.author, ctx.guild):
        await ctx.send("⛔ **Admin Only:** Only server administrators can clear the Server Master Playlist.", ephemeral=True)
        return

    server_playlist.clear()
    music_manager.get_player(ctx.guild).sync_with_storage()
    await ctx.send("🗑️ Cleared all songs from the server playlist.")


@bot.command(name="sync")
async def force_sync(ctx: commands.Context):
    """Admin command to force instant re-sync of slash commands in this server."""
    if not is_admin_user(ctx.author, ctx.guild):
        await ctx.send("⛔ Admin only.")
        return
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"⚡ Instantly synced **{len(synced)}** slash commands to this server!")


# --- Audio Controls ---
@bot.hybrid_command(name="pause", description="Pause the currently playing song")
async def pause(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        await ctx.send("⏸️ **Playback paused.**")
    else:
        await ctx.send("⚠️ Nothing is currently playing.")


@bot.hybrid_command(name="resume", description="Resume playback if paused")
async def resume(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        await ctx.send("▶️ **Playback resumed.**")
    else:
        await ctx.send("⚠️ Audio is not paused.")


@bot.hybrid_command(name="skip", description="Skip to the next song in the queue or playlist")
async def skip(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
        player.skip()
        await ctx.send("⏭️ **Skipped track.**")
    else:
        await ctx.send("⚠️ Nothing is currently playing to skip.")


@bot.hybrid_command(name="stop", description="Stop playback, clear the queue, and reset loop")
async def stop(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    player.stop()
    await ctx.send("⏹️ **Playback stopped and queue cleared.**")


@bot.hybrid_command(name="queue", description="Show the current music queue and loop settings")
async def queue(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    if not player.current and not player.queue:
        await ctx.send("📭 The queue is currently empty.")
        return

    embed = discord.Embed(title="🎶 DJ Pete Queue", color=discord.Color.purple())

    if player.current:
        if player.loop_single:
            loop_indicator = "🔂 Single Loop"
        elif player.playlist_loop and player.true_random:
            loop_indicator = "🎲 True Random"
        elif player.playlist_loop:
            loop_indicator = "🔁 Sequential Playlist"
        else:
            loop_indicator = "⏹️ Stop on finish"

        embed.add_field(
            name="Now Playing",
            value=f"[{player.current.title}]({player.current.webpage_url}) (`{player.current.formatted_duration}`)\nMode: **{loop_indicator}**",
            inline=False
        )

    if player.playlist_loop and player.true_random and player.playlist_items:
        embed.add_field(
            name="Up Next",
            value=f"🎲 *True Random active ({len(player.playlist_items)} tracks in rotation). Each track is chosen independently with replacement.*",
            inline=False
        )
    elif player.queue:
        queue_lines = []
        char_count = 0
        for i, song in enumerate(player.queue[:10], start=1):
            title = song.title[:45] + "..." if len(song.title) > 45 else song.title
            line = f"`{i}.` [{title}]({song.webpage_url}) (`{song.formatted_duration}`) - {song.requester.mention}\n"
            if char_count + len(line) > 850:
                break
            queue_lines.append(line)
            char_count += len(line)

        remaining = len(player.queue) - len(queue_lines)
        queue_text = "".join(queue_lines)
        if remaining > 0:
            queue_text += f"\n*...and {remaining} more track(s)*"

        embed.add_field(name="Up Next", value=queue_text, inline=False)
    else:
        if player.playlist_loop and player.playlist_items:
            embed.add_field(
                name="Up Next",
                value="🔁 *End of playlist reached. Will restart from track 1 after this song.*",
                inline=False
            )
        else:
            embed.add_field(
                name="Up Next",
                value="⏹️ *No more songs queued. Player will stop when current song ends.*",
                inline=False
            )

    embed.set_footer(text=f"Total in queue: {len(player.queue)} | Volume: {int(player.volume * 100)}%")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="nowplaying", description="Show details about the currently playing track")
async def nowplaying(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    if not player.current:
        await ctx.send("⚠️ Nothing is currently playing.")
        return

    song = player.current
    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"**[{song.title}]({song.webpage_url})**",
        color=discord.Color.green()
    )
    embed.add_field(name="Duration", value=song.formatted_duration, inline=True)
    embed.add_field(name="Requested By", value=song.requester.mention, inline=True)
    if player.loop_single:
        loop_status = "🔂 Single Repeat"
    elif player.playlist_loop and player.true_random:
        loop_status = "🎲 True Random"
    elif player.playlist_loop:
        loop_status = "🔁 Sequential Playlist"
    else:
        loop_status = "⏹️ Off (Stops after this)"
    embed.add_field(name="Loop Status", value=loop_status, inline=True)
    embed.add_field(name="Volume", value=f"{int(player.volume * 100)}%", inline=True)

    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    await ctx.send(embed=embed)


@bot.hybrid_command(name="shuffle", description="Randomize the queue order once for this run")
async def shuffle(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    count = player.shuffle_queue()
    if count >= 2:
        await ctx.send(f"🔀 **Queue shuffled!** ({count} tracks randomized for this run).")
    else:
        await ctx.send("📭 Not enough songs in the queue to shuffle! (Need at least 2 songs in queue).")


@bot.hybrid_command(name="loop", description="Set loop mode: True Random, Sequential, Single Track, or Off")
@app_commands.describe(mode="Choose loop mode: True Random, Sequential, Single, or Off")
@app_commands.choices(mode=[
    app_commands.Choice(name="True Random (Memoryless random shuffle)", value="random"),
    app_commands.Choice(name="Sequential (Repeat playlist in order)", value="sequential"),
    app_commands.Choice(name="Single (Repeat current track)", value="single"),
    app_commands.Choice(name="Off (Stop after playing)", value="off")
])
async def loop(ctx: commands.Context, mode: Optional[str] = None):
    player = music_manager.get_player(ctx.guild)

    if not mode:
        if player.loop_single:
            current_mode = "🔂 **Single Track Repeat**"
        elif player.playlist_loop and player.true_random:
            current_mode = "🎲 **True Random Playlist Loop**"
        elif player.playlist_loop:
            current_mode = "🔁 **Sequential Playlist Loop**"
        else:
            current_mode = "⏹️ **Off (No Loop)**"
        await ctx.send(f"Current loop status: {current_mode}\nUse `/loop mode:<choice>` to change.")
        return

    mode = mode.lower()

    if mode == "random":
        if not player.playlist_items:
            saved = server_playlist.load_tracks()
            if saved:
                player.playlist_items = [
                    Song(t["title"], t["webpage_url"], t.get("duration"), t.get("thumbnail"), ctx.author)
                    for t in saved
                ]
            elif player.queue:
                player.playlist_items = ([player.current] if player.current else []) + list(player.queue)
        player.playlist_loop = True
        player.true_random = True
        player.loop_single = False
        await ctx.send("🎲 **True Random playlist loop enabled!** Tracks are picked randomly with replacement (memoryless).")
    elif mode in ("sequential", "playlist"):
        if not player.playlist_items:
            saved = server_playlist.load_tracks()
            if saved:
                player.playlist_items = [
                    Song(t["title"], t["webpage_url"], t.get("duration"), t.get("thumbnail"), ctx.author)
                    for t in saved
                ]
            elif player.queue:
                player.playlist_items = ([player.current] if player.current else []) + list(player.queue)
        player.playlist_loop = True
        player.true_random = False
        player.loop_single = False
        await ctx.send("🔁 **Sequential playlist loop enabled!** Plays in order and restarts from track 1 on end.")
    elif mode == "single":
        player.loop_single = True
        player.playlist_loop = False
        await ctx.send("🔂 **Single track loop enabled!** Currently playing track will repeat.")
    elif mode == "off":
        player.playlist_loop = False
        player.loop_single = False
        await ctx.send("⏹️ **Loop disabled!** Bot will stop when songs finish.")
    else:
        await ctx.send("❌ Invalid mode. Choose `random`, `sequential`, `single`, or `off`.")


@bot.hybrid_command(name="volume", description="Change playback volume (1-100)")
@app_commands.describe(level="Volume percentage from 1 to 100")
async def volume(ctx: commands.Context, level: int):
    if level < 1 or level > 100:
        await ctx.send("❌ Volume must be between 1 and 100.")
        return

    player = music_manager.get_player(ctx.guild)
    player.set_volume(level / 100.0)
    await ctx.send(f"🔊 **Volume set to {level}%**")


@bot.hybrid_command(name="autovolume", description="Toggle automatic volume leveling (boosts quiet songs automatically)")
async def autovolume(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    player.auto_volume = not player.auto_volume
    if player.auto_volume:
        await ctx.send("🔊 **Auto Volume Leveling is now ON!** (Quieter songs will be dynamically amplified to match standard loudness).")
    else:
        await ctx.send("⏹️ **Auto Volume Leveling is now OFF.** (Songs will play at their original unadjusted loudness).")


@bot.hybrid_command(name="join", description="Connect the bot to your current voice channel")
async def join(ctx: commands.Context):
    if await ensure_voice(ctx):
        player = music_manager.get_player(ctx.guild)
        player.manual_leave = False
        await ctx.send(f"🔊 Joined **{ctx.author.voice.channel.name}**!")


@bot.hybrid_command(name="leave", description="Disconnect the bot from the voice channel")
async def leave(ctx: commands.Context):
    player = music_manager.get_player(ctx.guild)
    player.manual_leave = True
    await player.cleanup()
    await ctx.send("👋 Disconnected from voice channel.")


@bot.hybrid_command(name="help", description="Show DJ Pete commands and guide")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="🎧 DJ Pete - Discord Music Bot",
        description="Plays your friend's music & great tunes with automatic playlist looping!",
        color=discord.Color.gold()
    )
    embed.add_field(name="/play", value="Play the Server Playlist on continuous loop (or `/play <song>` for a single track)", inline=False)
    embed.add_field(name="/playlist play", value="Start playing the Server Playlist on loop", inline=True)
    embed.add_field(name="/playlist add <url/name>", value="Add a tune or YouTube playlist to the server playlist", inline=True)
    embed.add_field(name="/playlist list", value="View all tracks in the server playlist", inline=True)
    embed.add_field(name="/playlist remove <#>", value="Remove a track from the server playlist", inline=True)
    embed.add_field(name="/skip", value="Skip current song", inline=True)
    embed.add_field(name="/pause & /resume", value="Pause or resume playback", inline=True)
    embed.add_field(name="/stop", value="Stop playback & clear queue", inline=True)
    embed.add_field(name="/queue", value="Show upcoming songs", inline=True)
    embed.add_field(name="/nowplaying", value="Show current track info", inline=True)
    embed.add_field(name="/volume <1-100>", value="Adjust bot volume", inline=True)
    embed.add_field(name="/join & /leave", value="Connect or disconnect bot", inline=True)
    await ctx.send(embed=embed)


def main():
    if not TOKEN:
        print("\n" + "=" * 60)
        print("ERROR: DISCORD_TOKEN is missing!")
        print("Please configure DISCORD_TOKEN in .env")
        print("=" * 60 + "\n")
        sys.exit(1)

    print("Starting DJ Pete...")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
