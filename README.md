# Discord Bot - Full Features & Command Guide

A Discord bot featuring multi-timer countdowns, smart scheduled reminders, music streaming with an interactive button controller, and a meme voice soundboard.

---

## 🚀 Setup & Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ZyggieZ/BMT.git
   cd BMT
   ```

2. **Install requirements**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Copy `.env.example` to `.env` and paste your Discord bot token:
   ```env
   DISCORD_BOT_TOKEN=your_token_here
   ```

4. **Download Soundboard Assets (Optional)**:
   ```bash
   python setup_sounds.py
   ```

5. **Run the bot**:
   ```bash
   python bot.py
   ```

---

## 🎵 Music & Interactive Controller
Play audio from YouTube directly into your voice channel with live E2EE (DAVE Protocol) support and interactive control buttons.

* `/play query: <url or search>` — Play or queue a song from YouTube.
* `/controller` — Display the interactive button controller panel in chat at any time.
* `/pause` — Pause current playback.
* `/resume` — Resume playback.
* `/skip` — Skip to the next song in the queue.
* `/stop` — Stop playback, clear the queue, and disconnect the bot.
* `/queue` — View the current song and upcoming queue.

### Interactive Music Buttons
Underneath the "Now Playing" card, you have one-click buttons:
* `[ ⏯ Pause/Resume ]` — Toggle playback.
* `[ ⏭ Skip ]` — Skip song.
* `[ 🔀 Shuffle ]` — Randomize upcoming queue order.
* `[ 📜 Queue ]` — Quick ephemeral view of the playlist.
* `[ ⏹ Stop ]` — Stop and leave voice.
* `[ 🔉 Vol - ]` / `[ 🔊 Vol + ]` — Adjust playback volume up or down in 10% steps.

> **Auto-Disconnect**: If all human members leave the voice channel, the bot automatically disconnects after 60 seconds of inactivity.

---

## 🔊 Voice Soundboard & Meme Audio FX
Play meme sound effects directly in your voice call. If music is playing, the bot pauses the music, plays the effect, and resumes right where it left off.

* `/soundboard` — Opens the interactive soundboard panel with a dropdown menu and quick-access buttons.
* `/sound name: <sound>` — Directly trigger a sound effect with autocomplete choices:
  * 💥 **Vine Boom**
  * 📢 **MLG Airhorn**
  * 🗿 **Bruh**
  * 🥢 **Metal Pipe**
  * 🎻 **Sad Violin**
  * 🥁 **Drum Roll**
  * 🎺 **Victory Fanfare**
  * 🥶 **Sheesh**

---

## 🔔 Smart Reminders & Countdown Timers

### Smart Reminders (with Notes & DM Option)
* `/remind duration: <time> note: <text> [dm: True/False]` — Schedule a reminder with a custom note (e.g. `/remind duration:2h30m note:Take pizza out of oven dm:True`).
* `/reminders` — View all your active scheduled reminders with dynamic countdowns.
* `/cancel_reminder [reminder_id]` — Cancel a specific reminder by ID or cancel all active reminders.

### Quick Countdown Timers
* `/timer duration: <time>` — Start a countdown timer (e.g. `/timer 15m30s`, `/timer 1h`, `/timer 45s`).
* `/time` — Check remaining time on active timers.
* `/cancel_timer` — Cancel your active timers.

> **Persistence**: Both timers and reminders are saved to disk (`timers.json` & `reminders.json`) and automatically restored if the bot restarts.

---

## 🛡️ Single-Instance Process Lock
The bot enforces a kernel file lock (`bot.lock`). Attempting to start a second instance will automatically abort with an error to prevent duplicate Discord interaction conflicts.

---

## 🤝 Collaboration & Contributors

Developed in collaboration with [@Cappuccino-san](https://github.com/Cappuccino-san) and [@ZyggieZ](https://github.com/ZyggieZ).
