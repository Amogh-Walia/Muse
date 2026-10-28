# Navidrome setup

[Navidrome](https://www.navidrome.org/) serves the library for playback: the play buttons in Muse, Navidrome's own web player, and Subsonic apps on phones and portable players. Muse stays the tool that edits tags and folders. Navidrome only reads the same folder.

    $MUSE_LIBRARY ◄── Muse writes (cleanup, MusicBrainz, artwork)
          │
          ▼ reads only
     Navidrome :4533 ── Subsonic API ──► phone / portable player apps
                                    └──► Muse page (play buttons, through Muse's proxy)
                                    └──► Navidrome's own web player

## 1. Prerequisites
- Python 3, FFmpeg (includes ffprobe), git
- The music folder copied or mounted on the machine

## 2. Muse
    git clone <repo> && cd Muse
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env                     # set MUSE_LIBRARY to the music folder
    .venv/bin/python app.py                  # creates the catalog on first start; http://127.0.0.1:8800

## 3. Install Navidrome
Download the release for your platform from [github.com/navidrome/navidrome/releases](https://github.com/navidrome/navidrome/releases) and check it against `navidrome_checksums.txt`. Then put the `navidrome` binary somewhere on your `PATH`, e.g. `~/.local/bin`. A distro package or Docker works too.

Create `~/.config/navidrome/navidrome.toml`:

    MusicFolder = "/path/to/your/music"   # the same folder as MUSE_LIBRARY
    DataFolder  = "/path/to/navidrome-data"
    Port        = 4533
    Address     = "127.0.0.1"            # "0.0.0.0" to reach it from phones on your network (then see section 5)
    ScanSchedule = "@every 1h"           # Muse also asks for a rescan after each of its changes
    Subsonic.DefaultReportRealPath = true
    PID.Album = "folder"                 # one album per folder (see below)

`DefaultReportRealPath` makes Navidrome report each song's real file path, which is how Muse links songs most reliably. It applies to clients Navidrome sees for the first time, so set it before Muse first connects. Otherwise, turn on "Report real path" for the `muse` player in Navidrome's settings (Players). Without it, Muse falls back to matching by MusicBrainz recording id and by album, track and title.

`PID.Album = "folder"` makes each folder one album. By default Navidrome identifies albums by their MusicBrainz release id, so an album pieced together from several downloads, with songs tagged from different editions, shows up as several albums with the same name. Muse keeps every album in its own folder, so grouping by folder keeps them whole whatever the tags say. Changing it on an existing install needs a full rescan (as admin: Settings → "Full scan"), and gives albums and songs new Navidrome ids. Muse re-links on its next sync, and phone apps refresh their library.

You don't need to hide Muse's trash folder yourself: Muse puts a `.ndignore` file in `.muse-trash/`, so discarded duplicates never show up in Navidrome.

## 4. Run Navidrome as a user service
It then starts at login and restarts if it crashes. `~/.config/systemd/user/navidrome.service`:

    [Unit]
    Description=Navidrome
    After=network-online.target
    [Service]
    ExecStart=%h/.local/bin/navidrome --configfile %h/.config/navidrome/navidrome.toml
    Restart=on-failure
    [Install]
    WantedBy=default.target

Then:

    systemctl --user daemon-reload
    systemctl --user enable --now navidrome
    loginctl enable-linger $USER     # keep it running when you're logged out

If Navidrome isn't answering, Muse runs `systemctl --user start navidrome` itself (`NAVIDROME_SERVICE`).

## 5. Only devices on your home Wi-Fi (firewalld)
With `Address = "0.0.0.0"`, the firewall decides who gets in. On a laptop the Wi-Fi joins other networks too, so don't open the port in the zone your Wi-Fi normally uses (`public`). Instead, give the home Wi-Fi connection its own zone that allows Navidrome, only from the home subnet. Every other network stays in `public`, with the port closed. Replace the subnet and the connection name (`nmcli connection show`) with yours:

    sudo firewall-cmd --permanent --new-zone=homewifi
    sudo firewall-cmd --permanent --zone=homewifi --add-service=dhcpv6-client   # same as public otherwise
    sudo firewall-cmd --permanent --zone=homewifi --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="4533" protocol="tcp" accept'
    sudo firewall-cmd --reload
    nmcli connection modify "Your Wi-Fi" connection.zone homewifi
    nmcli connection up "Your Wi-Fi"       # re-applies the zone (a brief reconnect)

Check with `firewall-cmd --get-active-zones`: the Wi-Fi interface should be listed under `homewifi`. Avoid firewalld's built-in `home` zone for this, since it also opens SSH and file sharing.

## 6. First login
- Open http://127.0.0.1:4533 and create the admin user.
- Create a second user, `muse`, for Muse. It only needs to read and stream, so it doesn't need to be an admin.

Only Navidrome admins can start a scan, so Muse's non-admin user relies on Navidrome's folder watcher, which notices changes on its own within seconds. After each change, Muse waits for that scan and then re-links songs. The hourly `ScanSchedule` is the backstop.

## 7. Connect Muse to Navidrome
In Muse's `.env`:

    NAVIDROME_URL=http://127.0.0.1:4533
    NAVIDROME_USER=muse
    NAVIDROME_PASSWORD=...
    # NAVIDROME_PUBLIC_URL=http://my-server:4533   # if your browser reaches Navidrome at another address

Restart Muse. In the background it checks Navidrome is running (starting the service if needed), waits for Navidrome to pick up any changes, and links every song to its Navidrome id. The Ingest tab shows the status and has **Sync with Navidrome**.

Muse signs Subsonic requests itself and proxies the audio, so the password and tokens never reach the browser. Songs play as-is, with seeking. ALAC (`.m4a`) and other formats browsers can't play are transcoded to MP3 320 by Navidrome.

## 8. Keep unfinished downloads out of Navidrome
Muse keeps an `Inbox/` folder in your library (`MUSE_INBOX`) with a `.ndignore` in it, so Navidrome ignores whatever is there. Set your downloader's download folder to it (in Nicotine+: Settings → Downloads → download folder, e.g. `~/Music/<library>/Inbox`). Songs then reach Navidrome only when Clean up moves them into `Artist/Year - Album/`, with their tags, cover and lyrics already sorted, so phone apps never cache a half-finished version.

## 9. Phones and portable players
- Install a Subsonic client: **Symfonium** (paid, best offline caching) or **Tempo** (free, open source) on Android.
- Add a server: `http://<this machine's address on your Wi-Fi>:4533` (e.g. `http://192.168.1.50:4533`) with your Navidrome user. Muse's **Access Navidrome** tab shows the current address, checks Navidrome answers on it, and links the admin pages on this machine (`http://localhost:4533`). `Address` must not be `127.0.0.1` for this, and the firewall must allow it (section 5).
- A fixed address helps: reserve this machine's IP in your router (DHCP reservation), so the apps keep finding it.
- Streaming quality: **Original/Raw** on Wi-Fi, so a DAC gets the untouched FLAC.
- Enable offline caching or downloads for albums you want away from home.

## 10. Away from home
Install **Tailscale** on the server and the device, then use the server's Tailscale address. Don't port-forward 4533 to the internet.
