# Changelog

## v1.4.0 (in progress)

### Added
- **Now playing.** A card at the top of the Player tab shows the file that is
  playing, its time and progress, and what Silence Player is doing: playing,
  ducked (and for which app), waiting for silence (with the countdown), or
  off. Hovering the tray icon shows the same in its tooltip. The window is a
  little taller to make room.
- **New tray menu.** Right-clicking the tray icon opens a dark menu in Silence
  Player's own style, headed by what's playing (green), ducked (yellow) or
  waiting for silence, and updating live while it's open. Rounded corners on
  Windows 11.
- **Update notice.** Silence Player checks GitHub for a newer stable release
  shortly after it starts and every 6 hours. When there is one, a bar offers
  **Download** (opens the release page), **Skip this version** or **Later**, and
  the tray menu gets a "Download" entry. Pre-releases are never offered. You
  can switch the check off, or check right away, under **Advanced > Check for
  updates**. Nothing is downloaded or installed by itself.
- **Start with Windows.** A new switch on the Player tab. When it's on,
  Silence Player starts when you sign in to Windows, goes straight to the tray
  and begins monitoring. Like every other setting it is applied when you click
  **Save Settings**. It only affects your Windows user and needs no admin
  rights. If you turn Silence Player off in Windows' own startup settings
  (Settings > Apps > Startup, or Task Manager), the switch shows it as off.
- **More apps on the Mirroring Fix list:** XSplit Broadcaster and Gamecaster,
  Lively Wallpaper and FluentFlyout. Existing settings get them added once; an
  app you removed from the list is not added back.
- **Installer.** Releases now come as `SilencePlayer-Setup-x.y.z.exe`, which
  installs Silence Player for your Windows user (no admin rights), adds a Start
  menu shortcut and an uninstaller, and keeps your settings when you install a
  newer version. A portable zip is there too, for running it from any folder.

### Improved
- **Apps on any output device are detected.** Silence Player used to watch only
  Windows' default output device. An app playing on another one (a headset
  picked in Discord's settings, a per-app output device in Windows) went
  unnoticed and the ambient sound played over it. Every active output device is
  watched now. Audio routing apps (NVIDIA Broadcast, Voicemeeter, SteelSeries
  Sonar, Elgato Wave Link, FxSound, Krisp, Boom 3D) are ignored: they only
  re-play other apps' sound, and those apps are detected directly.
- **The Mirroring Fix uses much less CPU.** It listened to each watched app by
  asking for new audio every millisecond, even when nothing played (3.3% of a
  CPU core for three watched processes, measured on Windows). It now checks
  every 20 ms and takes everything that arrived at once: 0.6-0.8%, with the
  same audio.
- **More reliable in the background.** The parts that watch audio run in their
  own threads, and Windows only lets those use its audio system after a setup
  step they never did; it worked because something else happened to do it.
  Silence Player now does it itself when it starts.
- **VLC 3.0.24.** The bundled player engine is updated from 3.0.23 to 3.0.24,
  a release with many security fixes.
- **Starts faster, fewer antivirus false alarms.** The app is no longer one EXE
  that unpacks itself into a temporary folder on every start; it's a normal
  program folder.
- If Windows doesn't allow writing next to `silenceplayer.exe` (for example in
  Program Files), settings are saved in `%APPDATA%\SilencePlayer` instead.

### For contributors
- The code is split into a `silenceplayer/` package with one job per file
  (see README.md, "How the code is organised"). `app.py` only starts the app.
- GitHub Actions checks the code with ruff, runs the tests, builds the
  installer and portable zip, and turns a pushed `v*` tag into a draft release.
  See RELEASING.md.
- Dependabot opens a pull request when a dependency has an update.
- `tools/update_vlc.py` updates the bundled VLC from the official zip in one
  command, after checking VideoLAN's published checksum.
- The repository only keeps the 43 VLC plugins the app uses (it had all 366).

## v1.3.1

### Fixed
- **Watching someone's Discord stream didn't reset the silence timer.** Silence
  Player started the ambient sound over the stream, stopped it again a moment
  later, and then started the countdown anew even though the stream was still
  playing. The Mirroring Fix is now only used when there is something for an
  app to mirror: your ambient sound, or an excluded app that is playing. In
  every other case Discord's sound counts as real audio.
- **Quiet audio could be mistaken for "mirroring only".** A stream played at low
  volume sat right on the Mirroring Fix's cutoff, so Discord flipped between
  "real" and "just mirroring" and the countdown kept running while you were
  listening. An app that only mirrors renders exact silence, so the cutoff is
  now just above zero, and the Mirroring Fix looks at the loudest sound of the
  last two seconds instead of an average.
- The log now shows what the Mirroring Fix heard (per process) whenever it
  changes its mind about an app, which makes problems like this easier to track
  down.

## v1.3.0 (stable)

Stability release, plus a few improvements found during testing.

### Fixed
- **Start/Stop/Start could leave two monitor threads running.** Each monitor
  worker now has its own stop signal, and a new worker waits for the old one to
  exit.
- **Playlist resume restarted the wrong track.** Silence Player now remembers
  the track as well as the position. If that track was deleted, it continues
  with the next one from the start. Single-file resume only applies to the file
  that was playing; a newly picked file starts at 0:00.
- **Loop Song restarted each repeat twice**, causing a glitch. Each repeat now
  loads the song once.
- **Running the EXE twice started two copies.** A second launch now opens the
  running copy's Settings window and exits.
- **Stop Monitoring froze the window during the fade-out.** Fades now run in
  the background, and a new fade or playback cleanly takes over from an old one.
- **A new playlist track played at full volume while ducked.** It now fades in
  to the ducked level.
- **The Mirroring Fix could treat Discord as active forever** after its
  audio stopped. It also ignored freshly started Discord streams for a moment,
  and could leave a capture running after being switched off.
- **Settings were saved inconsistently**: some on their own, some only when typed
  values happened to be read. Now every change applies right away, and settings
  are saved only when you click **Save Settings**, never on close or quit.
- **Error messages for invalid input were blank or unclear.** They now explain
  exactly what's wrong.
- **A damaged `config.json` was silently overwritten.** It's now kept as a
  backup, and the file is written safely so a crash can't corrupt it.
- **A deleted or moved audio file made playback retry forever.** It now stops
  with a clear message.
- **The tray menu always said "Stop Monitoring".** It now shows the right
  action.

### Added
- **The Mirroring Fix now covers more apps and is on by default.** It ships with
  Discord, SignalRGB and Wallpaper Engine, and you can add others (Advanced tab).
  These apps react to your sound and showed up as "playing", which stopped the
  ambient sound as soon as it started.
- If an app starts "playing" right after the ambient sound does, Silence Player
  offers to add it to the Mirroring Fix in one click.

### Changed
- The first tab is now called **Player**.
- The version number is shown in the bottom-left corner.
- On a new installation, Settings stays open until an ambient file or folder is
  chosen. Existing setups still start hidden in the tray.
- A diagnostic log is written to `%LOCALAPPDATA%\SilencePlayer\silenceplayer.log`.
- If ProcTap can't load, only the Mirroring Fix is disabled; the rest of the app
  keeps working.
- Much smaller download: only the VLC plugins needed for audio playback are
  bundled (24 MB instead of 133 MB), and SciPy is no longer included.
- Reproducible build: pinned `requirements.txt`, `silenceplayer.spec`,
  `build.ps1`, a test suite and CI.
