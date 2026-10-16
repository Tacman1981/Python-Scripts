import sys
import shutil
import subprocess
import asyncio
import collections
import threading
import queue
import time
import random
import discord
import discord.player
from discord.enums import SpeakingState
import yt_dlp
from typing import Optional, List, Dict, Any

# yt-dlp extraction configuration
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # Bind to IPv4 to prevent IPv6 YouTube 429 errors
    'remote_components': ['ejs:github'], # Download JS challenge solver script
    'js_runtimes': {'node': {}},   # Use Node.js to solve YouTube n-challenge and eliminate throttling
}

# FFmpeg streaming configuration with User-Agent and stream reconnects
FFMPEG_OPTIONS = {
    'before_options': '-user_agent Mozilla/5.0 -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# Silence yt-dlp version deprecation warnings
try:
    yt_dlp.YoutubeDL.deprecated_feature = lambda self, *args, **kwargs: None
except Exception:
    pass

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# --- Discord AudioPlayer Jitter & Burst Fix ---
# By default, discord.py starts streaming audio before Discord's gateway registers the
# speaking state, and if any frame is delayed, its timer falls to 0ms delay, bursting
# packets and causing Discord's WebRTC NetEq to stretch/warp playback speed.
# This patch primes Discord's jitter buffer with silence and guarantees steady 20ms pacing.
def _smooth_do_run(self):
    self.loops = 0
    client = self.client
    play_audio = client.send_audio_packet

    # 1. Announce speaking state to Discord gateway
    self._speak(SpeakingState.voice)

    # 2. Send 8 frames (160ms) of standard Opus silence (0xF8, 0xFF, 0xFE).
    # This primes the listener's WebRTC NetEq buffer so initial connection calibration
    # happens on silence rather than eating into or time-stretching the actual song!
    silence_frame = b'\xf8\xff\xfe'
    for _ in range(8):
        if self._end.is_set():
            return
        play_audio(silence_frame, encode=False)
        time.sleep(0.02)

    # 3. Synchronize timer to real-time starting right now
    self._start = time.perf_counter()
    self.loops = 0

    while not self._end.is_set():
        if not self._resumed.is_set():
            self.send_silence()
            self._resumed.wait()
            self._start = time.perf_counter()
            self.loops = 0
            continue

        data = self.source.read()

        if not data:
            if self._current_error is None:
                source_error = getattr(self.source, '_current_error', None)
                if source_error:
                    self._current_error = source_error
            self.stop()
            break

        if not client.is_connected():
            connected = client.wait_until_connected(client.timeout)
            if self._end.is_set() or not connected:
                return
            self._speak(SpeakingState.voice)
            self.loops = 0
            self._start = time.perf_counter()

        play_audio(data, encode=not self.source.is_opus())
        self.loops += 1

        # Smooth pacing: sleep until next 20ms boundary (drift-locked with anti-burst bounds)
        next_time = self._start + self.DELAY * self.loops
        target_sleep = next_time - time.perf_counter()
        delay = max(0.010, min(0.030, target_sleep))
        time.sleep(delay)

    if client.is_connected():
        self.send_silence()

discord.player.AudioPlayer._do_run = _smooth_do_run

# --- RFC 3550 RTP Timestamp Gap Fix (Fixes Song 2+ Stutter/Warp) ---
# In discord.py, when Song 1 ends and a pause occurs (the 2-3s gap between tracks),
# VoiceClient freezes the RTP timestamp. When Song 2 starts, it resumes with the frozen
# timestamp instead of advancing it by elapsed wall-clock time.
# Discord's WebRTC NetEq detects this 3-second timestamp freeze as a massive delay,
# causing Song 1 to play fine (fresh connection), but all subsequent songs to warp
# (dead slow initially, then fast forward to catch up).
# This patch advances the RTP timestamp by real elapsed wall-clock time according to RFC 3550.
_orig_send_audio_packet = discord.VoiceClient.send_audio_packet

def _rfc3550_send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
    now = time.perf_counter()
    last_time = getattr(self, '_last_packet_perf_time', None)
    if last_time is not None:
        gap = now - last_time
        # If transmission was paused for more than 40ms (the gap between tracks):
        if gap > 0.040:
            gap_samples = int(gap * 48000)
            self.checked_add('timestamp', gap_samples, 4294967295)
    self._last_packet_perf_time = now
    return _orig_send_audio_packet(self, data, encode=encode)

discord.VoiceClient.send_audio_packet = _rfc3550_send_audio_packet


class LowPriorityFFmpegAudio(discord.FFmpegPCMAudio):
    """FFmpeg audio stream running on 1 thread with lowered priority to prevent system, game, or desktop lag."""
    def _spawn_process(self, args: Any, **subprocess_kwargs: Any) -> subprocess.Popen:
        if sys.platform == "win32":
            flags = 0x08000000 | 0x00004000  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
            return subprocess.Popen(args, creationflags=flags, **subprocess_kwargs)
        else:
            # On Linux (GNOME / Mutter / Wayland / servers):
            # Run with mild 'nice -n 5' priority to safeguard game servers without starving audio decoding.
            # Avoid preexec_fn as it causes fork() deadlocks in multithreaded Python applications.
            nice_bin = shutil.which("nice")
            if nice_bin and isinstance(args, (list, tuple)):
                args = [nice_bin, "-n", "5"] + list(args)
            return subprocess.Popen(args, **subprocess_kwargs)


class BufferedAudioSource(discord.AudioSource):
    """
    Decouples real-time Discord voice playback from FFmpeg/network I/O.
    A dedicated background thread continuously decodes from FFmpeg into a 
    memory buffer (up to 5 seconds ahead).
    
    This guarantees source.read() returns instantly in 0.001ms, eliminating
    all WebRTC jitter, packet stalls, and speed-warping (slow-down / speed-up).
    """
    def __init__(self, original: discord.AudioSource, buffer_seconds: float = 5.0):
        self.original = original
        max_frames = max(50, int(buffer_seconds / 0.02))  # 5 seconds = 250 frames (at 20ms/frame)
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)
        self._stopped = threading.Event()
        self._error = None

        # Start the background feeder thread
        self.worker = threading.Thread(target=self._fill_buffer, daemon=True, name="AudioBufferFeeder")
        self.worker.start()

        # Wait until we have a healthy initial buffer (50 frames = 1 second) or EOF before beginning
        initial_target = min(50, max_frames)
        start_wait = time.perf_counter()
        while not self._stopped.is_set() and self.queue.qsize() < initial_target:
            if not self.worker.is_alive():
                break
            if time.perf_counter() - start_wait > 4.0:
                break
            time.sleep(0.01)

    def _fill_buffer(self):
        while not self._stopped.is_set():
            try:
                frame = self.original.read()
            except Exception as e:
                self._error = e
                break

            if not frame:
                # Stream reached EOF
                break

            # Put frame in queue; wait with timeout so we can exit cleanly if playback is stopped
            while not self._stopped.is_set():
                try:
                    self.queue.put(frame, timeout=0.1)
                    break
                except queue.Full:
                    continue

        self._stopped.set()

    def read(self) -> bytes:
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            if self._stopped.is_set():
                if self._error:
                    raise self._error
                return b''
            try:
                return self.queue.get(timeout=0.05)
            except queue.Empty:
                return b''

    def is_opus(self) -> bool:
        return self.original.is_opus()

    def cleanup(self) -> None:
        self._stopped.set()
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        self.original.cleanup()


class Song:
    """Represents a playable song."""
    def __init__(self, title: str, webpage_url: str, duration: Optional[int], thumbnail: Optional[str], requester: discord.User | discord.Member):
        self.title = title
        self.webpage_url = webpage_url
        self.duration = duration
        self.thumbnail = thumbnail
        self.requester = requester

    @classmethod
    def from_info_dict(cls, data: Dict[str, Any], requester: discord.User | discord.Member) -> "Song":
        title = data.get('title') or "Unknown Title"
        url = data.get('webpage_url') or data.get('url') or f"https://www.youtube.com/watch?v={data.get('id', '')}"
        duration = data.get('duration')
        thumbnail = data.get('thumbnail')
        return cls(title=title, webpage_url=url, duration=duration, thumbnail=thumbnail, requester=requester)

    async def get_audio_source(self, volume: float = 0.5, auto_volume: bool = True) -> discord.PCMVolumeTransformer:
        """Resolves a fresh streaming audio URL and returns an FFmpeg audio source."""
        loop = asyncio.get_running_loop()
        extract_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'source_address': '0.0.0.0',
            'remote_components': ['ejs:github'],
            'js_runtimes': {'node': {}},
        }
        stream_ytdl = yt_dlp.YoutubeDL(extract_opts)
        data = await loop.run_in_executor(None, lambda: stream_ytdl.extract_info(self.webpage_url, download=False))
        if 'entries' in data and data['entries']:
            data = data['entries'][0]

        audio_url = data.get('url')
        if not audio_url:
            raise RuntimeError("Could not extract streamable audio URL.")

        headers = data.get('http_headers') or {}
        ua = headers.get('User-Agent') or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
        
        # High-performance, low-latency FFmpeg flags:
        # -nostdin: disables terminal keyboard polling (fixes Windows UI/Task Manager lag)
        # -threads 1: single lightweight thread (virtually 0 CPU usage)
        # -probesize 64k -analyzeduration 0: instant playback start (eliminates slow-motion audio start)
        # -reconnect 1 -reconnect_streamed 1: resilience without false EOF stalls
        before_options = (
            f'-nostdin -threads 1 -user_agent "{ua}" '
            f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
            f'-probesize 64k -analyzeduration 0'
        )

        options = '-vn'
        if auto_volume:
            # Dynamic Audio Normalizer: boosts quiet songs automatically without clipping or distortion
            options += ' -af dynaudnorm=f=150:g=15:p=0.95:m=10'

        def _create_source() -> BufferedAudioSource:
            raw_source = LowPriorityFFmpegAudio(audio_url, before_options=before_options, options=options)
            buffered = BufferedAudioSource(raw_source, buffer_seconds=5.0)
            if buffered.queue.empty() and getattr(raw_source, '_current_error', None):
                buffered.cleanup()
                raise raw_source._current_error
            return buffered

        buffered_source = await loop.run_in_executor(None, _create_source)
        return discord.PCMVolumeTransformer(buffered_source, volume=volume)

    @property
    def formatted_duration(self) -> str:
        if not self.duration:
            return "Live Stream / Unknown"
        mins, secs = divmod(int(self.duration), 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{hours}:{mins:02d}:{secs:02d}"
        return f"{mins}:{secs:02d}"


class MusicPlayer:
    """Guild-specific music player managing queue, loop state, and voice playback."""
    def __init__(self, bot, guild: discord.Guild, playlist_storage=None):
        self.bot = bot
        self.guild = guild
        self.playlist_storage = playlist_storage
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.abc.Messageable] = None

        self.queue: List[Song] = []
        self.playlist_items: List[Song] = []
        self.playlist_loop: bool = False
        self.loop_single: bool = False
        self.true_random: bool = True  # Memoryless true random with replacement for playlists
        self.auto_volume: bool = True  # Automatic volume leveling (boosts quiet songs)

        self.current: Optional[Song] = None
        self.initial_track: Optional[Song] = None
        self.volume: float = 0.5

        self.last_playback_error = None
        self.next_event = asyncio.Event()
        self.song_added_event = asyncio.Event()
        self.destroyed = False
        self.manual_leave = False
        self.player_task = self.bot.loop.create_task(self.player_loop())

        def _task_done(t):
            if not t.cancelled() and t.exception():
                import traceback
                print(f"[Guild {self.guild.id}] CRITICAL: player_loop error:")
                traceback.print_exception(t.exception())
        self.player_task.add_done_callback(_task_done)

    async def send_message(self, content: str = None, embed: discord.Embed = None):
        """Sends a message to the active text channel if available."""
        if self.text_channel:
            try:
                await self.text_channel.send(content=content, embed=embed)
            except Exception:
                pass

    def play_next(self, error=None):
        """Callback invoked when a track completes or stops."""
        if error:
            print(f"[Guild {self.guild.id}] Playback error: {error}")
            self.last_playback_error = error
        else:
            self.last_playback_error = None
        self.bot.loop.call_soon_threadsafe(self.next_event.set)

    async def player_loop(self):
        """Continuous loop handling track progression, playlist repetition, and timeouts."""
        await self.bot.wait_until_ready()

        while not self.destroyed:
            self.next_event.clear()

            if not self.voice_client or not self.voice_client.is_connected():
                # Wait until connected or song added
                try:
                    await asyncio.wait_for(self.song_added_event.wait(), timeout=10)
                    self.song_added_event.clear()
                    continue
                except asyncio.TimeoutError:
                    if self.destroyed:
                        break

            # If voice channel has no human listeners, stop audio and wait silently (do NOT disconnect!)
            if self.voice_client and self.voice_client.channel:
                channel = self.voice_client.channel
                bot_id = self.bot.user.id if self.bot.user else None
                human_count = sum(
                    1 for uid in channel.voice_states.keys()
                    if uid != bot_id and not getattr(channel.guild.get_member(uid), "bot", False)
                )
                if human_count == 0:
                    self.current = None
                    if self.voice_client.is_playing() or self.voice_client.is_paused():
                        self.voice_client.stop()
                    try:
                        await asyncio.wait_for(self.song_added_event.wait(), timeout=5)
                        self.song_added_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

            # Determine next song:
            if self.loop_single and self.current:
                # Repeat the exact same song
                song = self.current
            elif self.initial_track:
                # Specific track requested by user to start playlist
                song = self.initial_track
                self.initial_track = None
                self.current = song
            elif self.playlist_loop and self.true_random:
                # TRUE RANDOM WITH REPLACEMENT:
                # Always load fresh from storage so newly added/removed tracks reflect live!
                if self.playlist_storage:
                    saved_tracks = self.playlist_storage.load_tracks()
                    if saved_tracks:
                        self.playlist_items = [
                            Song(t["title"], t["webpage_url"], t.get("duration"), t.get("thumbnail"), self.bot.user)
                            for t in saved_tracks
                        ]
                    else:
                        self.playlist_items = []

                if not self.playlist_items:
                    self.current = None
                    try:
                        await asyncio.wait_for(self.song_added_event.wait(), timeout=60)
                        self.song_added_event.clear()
                        continue
                    except asyncio.TimeoutError:
                        continue

                song = random.choice(self.playlist_items)
                self.current = song
            else:
                # Sequential queue playback (for single tracks or sequential playlist loop)
                if not self.queue:
                    if self.playlist_loop:
                        if self.playlist_storage:
                            saved_tracks = self.playlist_storage.load_tracks()
                            if saved_tracks:
                                self.playlist_items = [
                                    Song(t["title"], t["webpage_url"], t.get("duration"), t.get("thumbnail"), self.bot.user)
                                    for t in saved_tracks
                                ]
                        if self.playlist_items:
                            # End of sequential playlist reached: loop back from the beginning with latest tracks!
                            self.queue.extend(list(self.playlist_items))
                            embed = discord.Embed(
                                description="🔁 **End of playlist reached. Looping back from track 1!**",
                                color=discord.Color.blue()
                            )
                            await self.send_message(embed=embed)
                    else:
                        # Single song (or queue with loop off) finished: sit idle in channel (do NOT disconnect!)
                        self.current = None
                        try:
                            await asyncio.wait_for(self.song_added_event.wait(), timeout=60)
                            self.song_added_event.clear()
                            continue
                        except asyncio.TimeoutError:
                            continue

                song = self.queue.pop(0)
                self.current = song

            try:
                source = await song.get_audio_source(self.volume, self.auto_volume)
            except Exception as e:
                embed = discord.Embed(
                    title="⚠️ Failed to load track",
                    description=f"[{song.title}]({song.webpage_url})\n**Error:** {e}\n*Skipping to next song...*",
                    color=discord.Color.red()
                )
                await self.send_message(embed=embed)
                continue

            if not self.voice_client or not self.voice_client.is_connected():
                continue

            try:
                if self.voice_client.is_playing() or self.voice_client.is_paused():
                    self.voice_client.stop()
                    await asyncio.sleep(0.1)

                self.voice_client.play(source, after=self.play_next)
            except Exception as e:
                print(f"[Guild {self.guild.id}] Error starting voice stream: {e}")
                err_str = str(e)
                if "403" in err_str or "Forbidden" in err_str:
                    advice = "*(YouTube 403 Forbidden: Run `pip install -U yt-dlp` and `sudo apt install -y nodejs` on the Linux host)*"
                else:
                    advice = "*(On Linux, ensure `libopus0` and `ffmpeg` are installed: `sudo apt install -y libopus0 ffmpeg`)*"
                embed = discord.Embed(
                    title="⚠️ Broadcast Error",
                    description=f"Failed to broadcast audio: `{e}`\n{advice}",
                    color=discord.Color.red()
                )
                await self.send_message(embed=embed)
                await asyncio.sleep(1)
                continue

            # Send Now Playing embed
            embed = discord.Embed(
                title="🎶 Now Playing",
                description=f"[{song.title}]({song.webpage_url})",
                color=discord.Color.green()
            )
            embed.add_field(name="Duration", value=song.formatted_duration, inline=True)
            embed.add_field(name="Requested by", value=song.requester.mention, inline=True)
            if self.loop_single:
                loop_status = "🔂 Single Repeat"
            elif self.playlist_loop and self.true_random:
                loop_status = "🎲 True Random"
            elif self.playlist_loop:
                loop_status = "🔁 Sequential Playlist"
            else:
                loop_status = "⏹️ Off (Single track)"
            embed.add_field(name="Loop Mode", value=loop_status, inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            await self.send_message(embed=embed)

            # Wait until playback finishes or is skipped
            await self.next_event.wait()

    def add_tracks(self, songs: List[Song], is_playlist: bool, initial_track: Optional[Song] = None):
        """Adds songs to the queue. If it's a playlist, enables playlist loop mode."""
        self.initial_track = initial_track
        if is_playlist:
            self.playlist_loop = True
            self.true_random = True  # Default play style is ALWAYS True Random!
            self.loop_single = False
            self.playlist_items = list(songs)
            self.queue = list(songs)
        else:
            # Single song or normal queue
            # If nothing was playing and queue was empty, make sure playlist loop is false
            if not self.current and not self.queue:
                self.playlist_loop = False
                self.playlist_items = []
            self.queue.extend(songs)

        self.song_added_event.set()

    def sync_with_storage(self):
        """Immediately syncs in-memory playlist tracks and upcoming queue with storage."""
        if not self.playlist_storage:
            return
        saved = self.playlist_storage.load_tracks()
        self.playlist_items = [
            Song(t["title"], t["webpage_url"], t.get("duration"), t.get("thumbnail"), self.bot.user)
            for t in saved
        ]
        if self.playlist_loop and not self.true_random:
            saved_urls = {t["webpage_url"] for t in saved}
            self.queue = [s for s in self.queue if s.webpage_url in saved_urls]

    def shuffle_queue(self) -> int:
        """Shuffles the current queue order once (Fisher-Yates shuffle). Returns number of tracks shuffled."""
        if len(self.queue) < 2 and self.playlist_items:
            other_tracks = [t for t in self.playlist_items if not self.current or t.webpage_url != self.current.webpage_url]
            self.queue = other_tracks if other_tracks else list(self.playlist_items)

        if len(self.queue) >= 2:
            random.shuffle(self.queue)
            self.true_random = False  # Switch to playing through the shuffled queue sequentially
            return len(self.queue)
        return 0

    def skip(self):
        """Skips the currently playing track."""
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            # If skipping during single song loop, turn off single loop to advance
            if self.loop_single:
                self.loop_single = False
            self.voice_client.stop()

    def stop(self):
        """Stops audio, clears the queue and playlist loop."""
        self.initial_track = None
        self.queue.clear()
        self.playlist_items.clear()
        self.playlist_loop = False
        self.loop_single = False
        self.current = None
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()

    def set_volume(self, volume: float):
        """Sets playback volume between 0.0 and 1.0."""
        self.volume = max(0.0, min(1.0, volume))
        if self.voice_client and self.voice_client.source:
            if isinstance(self.voice_client.source, discord.PCMVolumeTransformer):
                self.voice_client.source.volume = self.volume

    async def cleanup(self):
        """Cleans up the player and disconnects from voice."""
        self.destroyed = True
        self.stop()
        if self.player_task:
            self.player_task.cancel()
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None


class MusicManager:
    """Manages MusicPlayer instances across different Discord servers."""
    def __init__(self, bot, playlist_storage=None):
        self.bot = bot
        self.playlist_storage = playlist_storage
        self.players: Dict[int, MusicPlayer] = {}

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        if guild.id not in self.players or self.players[guild.id].destroyed:
            self.players[guild.id] = MusicPlayer(self.bot, guild, playlist_storage=self.playlist_storage)
        return self.players[guild.id]

    async def extract_query(self, query: str, requester: discord.User | discord.Member) -> tuple[List[Song], bool, str]:
        """
        Extracts tracks from a YouTube URL or search query.
        Returns: (list_of_songs, is_playlist, title_or_query)
        """
        loop = asyncio.get_running_loop()
        is_url = query.startswith("http://") or query.startswith("https://")

        # Flat extraction to detect playlists without fetching full video streams
        flat_ytdl = yt_dlp.YoutubeDL({
            'extract_flat': 'in_playlist',
            'quiet': True,
            'no_warnings': True,
            'source_address': '0.0.0.0',
            'remote_components': ['ejs:github'],
            'js_runtimes': {'node': {}},
        })

        search_query = query if is_url else f"ytsearch:{query}"
        data = await loop.run_in_executor(None, lambda: flat_ytdl.extract_info(search_query, download=False))

        if not data:
            return [], False, ""

        entries = data.get('entries')
        if entries is not None and not isinstance(entries, list):
            entries = list(entries)

        # Check if result is a playlist
        if data.get('_type') == 'playlist' and entries and len(entries) > 1:
            songs = []
            for entry in entries:
                if not entry:
                    continue
                # Some flat playlist entries have id or url
                title = entry.get('title') or "Unknown Track"
                url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
                if not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={entry.get('id', url)}"
                duration = entry.get('duration')
                thumbnail = entry.get('thumbnail')
                songs.append(Song(title=title, webpage_url=url, duration=duration, thumbnail=thumbnail, requester=requester))
            return songs, True, data.get('title') or query

        # If it's a search result, take the first entry
        if entries and len(entries) > 0:
            entry = entries[0]
            song = Song.from_info_dict(entry, requester)
            return [song], False, song.title

        # Single video URL
        song = Song.from_info_dict(data, requester)
        return [song], False, song.title
