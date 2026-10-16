# Discord 24/7 Music & Radio Bot 🎧

A production-grade, self-healing Discord music bot designed for 24/7 server voice channels. Features automatic silent channel joining, listener presence-triggered True Random radio playback, zero-privileged-intents operation, and drift-locked jitter-free audio streaming.

---

## 🌟 Key Features

1. **Silent 24/7 Auto-Join & Idle**:
   - The bot automatically connects to your designated voice/radio channel (configured via `AUTO_JOIN_VOICE_CHANNEL_ID` in `.env` or auto-detected by channel name) silently on startup without spamming chat embeds.
   - If listeners are already in the channel when it comes online, it starts playing the server playlist automatically.
   - If the room is empty, it sits silently in the channel.

2. **Presence-Driven Playback**:
   - When a human joins the voice channel, the bot immediately starts streaming the server playlist in **True Random** (memoryless shuffle with replacement) mode.
   - When the last human leaves, the bot stops playback after a 2-second grace period to save CPU and network bandwidth. It **never disconnects**—it waits silently for the next listener.

3. **Self-Healing Watchdog**:
   - A 30-second background watchdog monitors voice connectivity. If Discord's voice servers drop or a network blip occurs, the bot reconnects automatically.
   - If an administrator explicitly runs `/leave`, the bot honors it and will not force-rejoin until `/join`, `/play`, or a listener enters.

4. **WebRTC & RFC 3550 Compliant Audio Engine**:
   - **RTP Timestamp Gap Compensation**: Patched `discord.VoiceClient.send_audio_packet` dynamically advances RFC 3550 RTP timestamps across track changes and pauses, preventing Discord's WebRTC NetEq buffer from falling into slow-motion catch-up or high-speed jitter.
   - **Pre-Decoded 5-Second Memory Buffer**: Decodes up to 250 audio frames (5 seconds) into RAM via a background thread to prevent underruns during network packet spikes.
   - **Drift-Locked Pacing**: Hardware-timed 20ms frame delivery (`time.perf_counter()`) with initial silence priming.

---

## 🔐 Discord Application Setup & Permissions Guide

Follow these step-by-step instructions in the [Discord Developer Portal](https://discord.com/developers/applications) to register and configure the application:

### Step 1: Create the Discord Application
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click the **"New Application"** button in the top right.
3. Give your application a name (e.g. `DJ Pete`) and agree to the Developer Terms of Service.
4. *(Optional)* Under **General Information**, upload an avatar/icon and a description for your bot.

---

### Step 2: Configure the Bot User & Secret Token
1. In the left navigation menu, click on the **"Bot"** tab.
2. Click **"Reset Token"** (and enter your 2FA code if prompted).
3. **Copy the token immediately** and paste it into your `.env` file as `DISCORD_TOKEN`. Keep this token completely private!
4. **Public Bot**: You can toggle this **OFF** if you want to ensure only you can invite the bot to servers.
5. **Privileged Gateway Intents**:
   Leave **all three switches toggled OFF**:
   - ❌ **Presence Intent**: OFF
   - ❌ **Server Members Intent**: OFF
   - ❌ **Message Content Intent**: OFF

> **Why Zero Privileged Intents?**
> - The bot uses Discord Slash Commands (`/play`, `/playlist`), which deliver arguments directly via interactions without needing message content inspection.
> - The bot uses Discord Gateway `channel.voice_states` to detect voice listeners in real-time, eliminating the need for privileged member cache intents.
> - Your bot will **never require Discord verification** or intent auditing!

---

### Step 3: Generate the Bot Invite Link (OAuth2)
1. In the left navigation menu, navigate to **OAuth2 > URL Generator**.
2. Under **Scopes**, select:
   - `bot`
   - `applications.commands`
3. Under **Bot Permissions**, select the following checkboxes:
   - **General Permissions**:
     - *View Channels*
   - **Text Permissions**:
     - *Send Messages*
     - *Embed Links*
     - *Read Message History*
     - *Use External Emojis*
   - **Voice Permissions**:
     - *Connect*
     - *Speak*
     - *Use Voice Activity*
     - *Priority Speaker* *(optional but recommended)*
4. The generated permissions integer will be: **`37047552`**.
5. Copy the generated URL at the bottom:
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=37047552&scope=bot+applications.commands
   ```
6. Open the URL in your web browser, select your Discord server, and click **Authorize**.

---

### Step 4: Server Whitelist Safeguard
To guarantee that nobody else can run the bot or hijack its audio streams, the bot enforces optional server ID whitelisting in your `.env` file:
```env
GUILD_ID=your_server_id_here
```
If the bot is ever invited to an unauthorized server, it logs the attempt and auto-leaves immediately. (Leave blank to allow the bot in any server).

---

## 🛠️ Linux Production Build & Deployment

The bot runs as a background service managed by `systemd` on Linux (Ubuntu / Debian).

### 1. System Dependencies

```bash
# Update package lists
sudo apt update

# Install audio decoder, Opus codecs, and Node.js (for YouTube JS challenges)
sudo apt install -y ffmpeg libopus0 libopus-dev nodejs python3 python3-pip python3-venv git curl
```

### 2. Project Directory & Python Virtual Environment

```bash
# Create directory
mkdir -p /home/<user>/bot/DiscordBot
cd /home/<user>/bot/DiscordBot

# Create and activate virtualenv
python3 -m venv venv
source venv/bin/activate

# Install required Python packages
pip install --upgrade pip
pip install discord.py yt-dlp python-dotenv
```

### 3. Environment Configuration (`.env`)

Create or update your `.env` file:

```env
# Discord Bot Token from Developer Portal
DISCORD_TOKEN=your_bot_token_here

# Restrict bot commands to your server ID (leave blank to allow any server)
GUILD_ID=your_server_id_here

# Designated voice channel ID to auto-join silently (leave blank to auto-detect by name)
AUTO_JOIN_VOICE_CHANNEL_ID=your_voice_channel_id_here
```

### 4. Systemd Service Setup (`amg-bot.service`)

Create the systemd service file at `/etc/systemd/system/amg-bot.service`:

```ini
[Unit]
Description=Discord Music Bot
After=network.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/bot/DiscordBot
ExecStart=/home/<user>/bot/DiscordBot/venv/bin/python3 bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable amg-bot
sudo systemctl restart amg-bot
```

Monitor live logs:
```bash
journalctl -u amg-bot -f
```

---

---

## 🎵 Commands Reference

### Server Master Playlist Commands
| Slash Command | Permissions | Description |
|---|---|---|
| `/play` *(no query)* | Everyone | Starts playing the **Server Master Playlist** on True Random loop. |
| `/play [track]` | Everyone | Starts playing the master playlist at a specific track number (e.g. `/play 5`). |
| `/playlist play` | Everyone | Starts playing the master playlist on continuous True Random loop. |
| `/playlist list` | Everyone | View all saved tracks with pagination buttons. |
| `/playlist add <url/query>` | **Admin Only** | Adds a song or full YouTube playlist to the master playlist. |
| `/playlist remove <#>` | **Admin Only** | Removes a track number from the master playlist. |
| `/playlist clear` | **Admin Only** | Clears all songs from the master playlist. |

### Playback & Audio Controls
| Slash Command | Permissions | Description |
|---|---|---|
| `/play <url/name>` | Everyone | Plays a specific one-off song (plays once and stops). |
| `/pause` / `/resume` | Everyone | Pause or resume playback. |
| `/skip` | Everyone | Skip to the next track. |
| `/stop` | Everyone | Stops playback and clears the active session. |
| `/nowplaying` | Everyone | View current track details, thumbnail, duration, and requester. |
| `/queue` | Everyone | Display upcoming songs and current loop mode. |
| `/volume <1-100>` | Everyone | Adjust bot output volume. |
| `/autovolume` | Everyone | Toggle automatic loudness equalization (boosts quiet tracks). |
| `/join` / `/leave` | Everyone | Connect or disconnect the bot from voice. |



This application is so lightweight, it will actually run on a smart phone as a host for it
