# Vinted Reposter

A small web page, running in a Docker container on your own computer or NAS, that
shows your Vinted wardrobe and lets you **repost** any listing with one click.
Before it touches anything it saves a copy of the listing (photos included) to disk,
so nothing is ever lost.

It is the companion of [Vinted Token Refresher](https://github.com/TerrorSource/VintedTokenRefresher),
which keeps you logged in. The reposter can also run on its own.

## Why would I want this?

On Vinted, new listings show up at the top of search results. After a few weeks a
listing sinks to the bottom and hardly anyone sees it any more. Sellers "repost"
to get back to the top: they create the listing again and delete the old one.

Doing that by hand means re-typing the title and description, re-uploading the
photos and choosing the size, brand and condition again — for every single item.
This tool does it for you.

## What a repost actually does

Vinted has no repost button, so this is what happens behind the scenes for each item:

1. The listing is read in full: title, description, price, brand, size, condition,
   colours, package size.
2. A **backup** is saved to disk: the listing details plus the original photos.
3. The photos are uploaded again.
4. A **new listing** is created with the same content.
5. Only when the new listing exists is the **old one deleted**. If anything goes
   wrong before that, the old listing stays exactly as it was.

The new listing starts from zero: no likes, no views, and a new web address. That
is how reposting works on Vinted, and no tool can avoid it.

## What you see

Open `http://<your-nas-ip>:8095` in a browser and log in. There are five tabs.

**Listings** — your wardrobe with photo, price, age and number of likes. Search by
title, sort by age, price or likes. Sold items are hidden unless you tick *show
sold*. For each item you can:

- **Back up** — save a copy to disk without changing anything on Vinted.
- **Repost** — you get a small window where you set the price for the new listing:
  keep it, type a new one, or take 5, 10 or 20 percent off. A small price drop is
  usually what actually sells an item, so you decide per item. You are asked to
  confirm, and reminded that the old listing will be deleted.
- **Exclude** — keep the automatic reposter away from this item.

Tick several items and a bar appears to back up or repost them all at once.

A repost you click does not happen instantly. It waits a random 30 to 90 seconds
first, and you see the countdown. This is deliberate: five clicks in five seconds
would look like a robot to Vinted. Reposts queue up and are carried out one at a
time.

**Backups** — everything saved so far. **Download** gives you a zip with the
details and photos. **Restore** creates a new listing from a backup and deletes
nothing — your undo button if a repost went wrong. **Delete** removes the backup
from disk and leaves Vinted alone.

Sold items can be backed up too, but only partly: Vinted no longer gives out the
description of a sold item, so you get the photos, title, price, size and brand.
Those backups are marked and cannot be restored, but you can still download them.

**Settings** — every knob in one form. Changes take effect immediately, no restart
needed. This is also where you paste your Vinted login (see *Getting it working*).

**History** — every repost that happened: which old listing became which new one,
when, and whether its backup is still there.

**Log** — what the container is doing, in plain sentences.

At the top of every page a banner tells you whether you are in **dry run** or
**live** mode, how many reposts were done today, and how long your login is still
good for. Your Vinted login expires after about a week if it is not kept alive, so
the banner warns you in time.

## Getting it working

You need Docker (for example Portainer or Container Station on a NAS), a Vinted
account, and about fifteen minutes.

### 1. Create two folders on your NAS

One for your login details, one for the backups. For example:

```
/share/CACHEDEV1_DATA/Docker/vinted-token-refresher/state
/share/CACHEDEV1_DATA/Docker/vinted-reposter/backups
```

If you already run the Token Refresher, use its existing `state` folder. **Both
containers must share that folder**: they then use the same login and never get in
each other's way.

### 2. Start the container

1. In Portainer go to *Stacks* → *Add stack*, choose *Web editor*, and paste the
   contents of `docker-compose.yml`.
2. Change the two folder paths at the bottom to the folders you just created.
3. Set `WEB_USER` and `WEB_PASSWORD` to a login of your own choosing. The page can
   delete your listings, so do not leave these at `CHANGE_ME`.
4. Set `USER_AGENT` to the browser you use for Vinted. In Chrome, open
   `chrome://version` and copy the *User Agent* line. This matters: Vinted checks
   that the login it gave your browser is used by the same kind of browser.
5. Deploy. The image is downloaded automatically from
   `ghcr.io/terrorsource/vintedreposter:latest`.

Open `http://<your-nas-ip>:8095` and log in with the user and password from step 3.

### 3. Give it your Vinted login

The container needs a few cookies from your browser, the small pieces of data that
tell Vinted "this is me". There are two ways to hand them over.

**The easy way: the browser script.** Install the
[Tampermonkey](https://www.tampermonkey.net/) extension, then add the script
`vinted-state.user.js` from the [Token Refresher repo](https://github.com/TerrorSource/VintedTokenRefresher).
Open the reposter page once in the same browser and log in, so the browser
remembers the login. Then, on vinted.nl, open the Tampermonkey menu and choose
**Send to reposter…**. The first time it asks for the address of the reposter
(`http://<your-nas-ip>:8095`). That is it: the cookies go straight into the
container and it logs in to Vinted on the spot.

**The manual way.** On vinted.nl open the browser's developer tools
(F12 → *Application* → *Cookies* → `www.vinted.nl`). Copy the values of
`refresh_token_web`, `access_token_web`, `datadome`, `v_udt` and `anon_id` into the
matching fields under **Settings** and press **Save & refresh token**.

Be quick with the manual way: `refresh_token_web` is single use. If your browser
refreshes its own session while you are copying, the value you copied is already
dead. The browser script avoids that race, which is why it exists.

Once the container has logged in, it keeps the login alive by itself. The page
never shows the values back to you, only that they are stored and their last few
characters.

### 4. Try it in dry run

The container starts in **dry run**: Repost goes through all the motions and writes
to the log, but nothing on Vinted changes. Use it to check that you see your
wardrobe and that a repost would do what you expect.

### 5. Go live

Under **Settings**, switch *Dry run* off. From now on a repost is real. Reposts
still only happen when you click, unless you also switch on automatic reposting.

## Automatic reposting

Under **Settings** you can let the container repost on its own. When on, it looks
through your wardrobe every hour and reposts items that have not been touched for
a while. You control:

- **Only items older than** — how many days since the item was uploaded or last
  reposted. Default 30 days.
- **Max reposts per day** — a daily cap. Default 3. Your own clicks count towards
  it but are never blocked.
- **Active hours** — for example 09:00 to 22:00, so reposts happen when a person
  would be awake.
- **Pause between reposts** — a random wait, default 2 to 10 minutes, so
  automatic reposts do not come in a burst.
- **Only these / Never these item ids** — a whitelist or blacklist. The
  **Exclude** button on the Listings tab fills the blacklist for you.

Start small. Three reposts a day of items older than a month is unlikely to draw
attention. Reposting your whole wardrobe every day may get your account banned
(see the disclaimer at the bottom).

## Telegram messages

Under **Settings** → *Notifications* you can have the container message you on
Telegram: only when something fails (a repost that went wrong, a login that
expired), or for every repost. Create a bot with
[@BotFather](https://t.me/BotFather), paste the token it gives you, and paste your
own chat id (message [@userinfobot](https://t.me/userinfobot) to find it). **Send
test message** checks that it works.

## Backups

Every item is backed up before it is reposted. Backups are kept for 30 days by
default and then cleaned up; set *Delete backups after* to 0 to keep them forever.
Backing an item up again resets its clock. The backups folder on your NAS contains
one folder per item with the listing details and the photos, so even without this
tool you can read them.

## When something goes wrong

- **"Login expired" or every action fails with 401** — the login has run out.
  Send the cookies again with the browser script, or paste them under Settings.
- **CAPTCHA or 403 on every action** — Vinted wants to see a human. Open vinted.nl
  in your browser, solve the puzzle if one appears, and send the cookies again.
  Check that `USER_AGENT` matches your browser.
- **New listing created but the old one still there** — this is the safe outcome
  of a failure halfway. Delete the old listing by hand in the Vinted app. The
  repost is in History and the backup is kept.
- **The wardrobe looks out of date** — it is cached for two minutes; press
  **Reload**.
- **The container shows *unhealthy*** — its internal workers have stopped. Check
  the Log tab or the container log, then restart the container.

## Keeping it safe

- The page can delete and re-list your items, and it holds your Vinted login. Set
  a proper `WEB_USER` and `WEB_PASSWORD`, and **do not open port 8095 to the
  internet**. Use it on your home network or over a VPN.
- Your login details are stored in `state.json` in the state folder, readable
  only by the container's user, and never leave your NAS. The web page can only
  see that a value is stored, not the value itself.
- Backups contain your listing texts and photos. They also stay on your NAS.
- Neither file is part of the Docker image or this repository.

## Settings you can only change in Docker

Nearly everything lives under **Settings** on the page. The compose file only
holds the starting values plus a few things that cannot change while the container
runs:

| Variable | What it does |
|---|---|
| `WEB_USER` / `WEB_PASSWORD` | Login for the page. Kept out of Settings so the password is never written to a file |
| `USER_AGENT` | Your browser's identity. Must match the browser your cookies come from |
| `TZ` | Your timezone, so *Active hours* mean what you think |
| `WEB_ENABLED` | `false` runs without the web page, automatic reposting only |

Editing a value in the compose file only has effect for settings you have never
changed on the page. **Reset to docker-compose values** under Settings goes back
to the compose file for everything.

## For the technically curious

The container is Python with Flask, served by waitress. It talks to the same
`/api/v2` endpoints the Vinted website uses, through `curl_cffi` so the connection
looks like Chrome's. There is no browser engine and no proxy.

| File | |
|---|---|
| `app.py` | the web routes and JSON API |
| `worker.py` | the job queue, backup/repost/restore, the scheduler |
| `vinted_api.py` | every call to Vinted, and the mapping from what Vinted returns to what it wants back |
| `auth.py` | the cookies in `state.json` and the access token in `token.json` |
| `settings.py` | the settings form, validation, `settings.json` |
| `store.py` | log, history, backups on disk |
| `templates/index.html` | the whole page, one file |

Three things that cost an evening each, for whoever has to debug this later:

- **Every write needs an `X-CSRF-Token` header.** Without it Vinted answers
  `403 access_denied`, which looks like a permissions problem. The token is in the
  page HTML of `/items/new`; the container fetches it once per session.
- **The create call wants size and condition twice**: inside `item_attributes`
  *and* as flat `size_id` / `status_id`. The browser only sends the first, but a
  body built that way is rejected. The price goes out as a bare number.
- **The login refresh needs the `access_token_web` cookie**, not just
  `refresh_token_web`. Without it Vinted returns an empty `400`, however valid
  the refresh token is.

The endpoints: `GET /api/v2/users/{id}/items` (wardrobe),
`GET /api/v2/item_upload/items/{id}` (full item in the editor's shape),
`POST /api/v2/photos` (multipart upload, returns a numeric id),
`POST /api/v2/item_upload/items` (create), `DELETE /api/v2/items/{id}`.
All three write paths can be overridden with the environment variables
`PHOTO_UPLOAD_PATH`, `ITEM_CREATE_PATH` and `ITEM_DELETE_PATH` if Vinted moves one.

Every write to the page's own API must carry the header
`X-Requested-With: vinted-reposter`. The page and the browser script add it; a
malicious site open in the same browser cannot, which is what stops it from
deleting your listings behind your back.

## Disclaimer

Vinted's terms of service restrict automated use, and mass re-listing may be
treated as spam, up to and including a permanent ban. This tool automates actions
you could perform by hand in the app; using it is your own decision and your own
risk. It is provided as-is, without any warranty.
