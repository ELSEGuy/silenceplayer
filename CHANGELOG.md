# Changelog

## v1.3.1

### Fixed
- **Watching someone's Discord stream didn't reset the silence timer.** Silence
  Player started the ambient sound over the stream, stopped it again a moment
  later, and then started the countdown anew even though the stream was still
  playing. The Mirroring Fix is now only used when there is something for an
  app to mirror: your ambient sound, or an excluded app that is playing. In
  every other case Discord's sound counts as real audio.
- **Quiet or bursty audio could be mistaken for "mirroring only".** The Mirroring
  Fix now looks at the loudest sound in the last two seconds instead of an
  average of the last few moments.
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
