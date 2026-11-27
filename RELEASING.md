# Releasing a new version

GitHub builds the installer and the portable zip for you. You prepare the
version number, push a tag, check the draft, and click **Publish**.

## 1. Try the build first

Push your work to a branch whose name starts with `v` (for example `v1.4-dev`)
or to `master`. GitHub Actions then runs the code check and the tests, and
builds everything. When the run is green:

1. Open the repo on GitHub → **Actions** → click the run.
2. Under **Artifacts**, download **silenceplayer-build**.
3. Install `SilencePlayer-Setup-….exe` and try it. Also try the portable zip.

## 2. Set the version number

On `master` (after merging your pull request as usual):

1. In `silenceplayer/__init__.py`, set `APP_VERSION = "1.4.0"` (no `-dev`).
2. In `CHANGELOG.md`, change `## v1.4.0 (in progress)` to `## v1.4.0`.
   The text under it becomes the release notes.
3. Commit (for example "Release 1.4.0") and push.

## 3. Create the tag (GitHub Desktop)

1. Open the **History** tab.
2. Right-click the commit from step 2 → **Create Tag…** → type `v1.4.0` →
   **Create Tag**.
3. Click **Push origin**. This also pushes the tag.

The tag must match `APP_VERSION` exactly (`v` + the version). If it doesn't,
the release step stops with an error that says so.

## 4. Publish

1. Wait for the **Actions** run of the tag to finish (about 10 minutes).
2. Go to **Releases**. You'll see a **draft** called "Silence Player v1.4.0" with
   the installer, the portable zip and `SHA256SUMS.txt` attached.
3. Edit the notes if you like, then click **Publish release**.

From then on, everyone running 1.4 or newer gets the "update available"
notice.

## If something went wrong

- **The run failed:** open it in **Actions** to see which step failed.
- **Wrong tag:** delete the draft release, then delete the tag on GitHub
  (**Releases** → **Tags** → the tag → delete). Fix the problem, then create the
  tag again.
- **You made the release on the website instead:** that works too. Create the
  release with a new tag there and the files are added to it automatically when
  the build finishes. But the release is public before its files arrive, so the
  draft route above is nicer for your users.

## Updating VLC

Every few months VideoLAN releases a new VLC 3.0.x, often with security fixes.
Run `python tools\update_vlc.py 3.0.25` (with the new version number) on your
PC, then run the tests and play a few files before committing. It also writes
the new version into README.md, requirements.txt, silenceplayer.spec and
THIRD-PARTY-NOTICES.md (a test checks that they all agree).
