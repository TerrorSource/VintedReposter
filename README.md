# Vinted Reposter

A small web page, running in Docker on your NAS, that shows your Vinted wardrobe
and reposts any listing with one click. Every listing is backed up to disk, photos
included, before anything is touched.

Companion of [Vinted Token Refresher](https://github.com/TerrorSource/VintedTokenRefresher),
which keeps you logged in. Works on its own too.

## Why

New listings show at the top of Vinted's search results. After a few weeks they
sink and nobody sees them. Reposting puts them back on top, but by hand it means
re-typing everything and re-uploading the photos for every item. This does that
for you.

## What a repost does

Vinted has no repost button, so for each item the tool:

1. reads the listing and **saves a backup** to disk,
2. uploads the photos again and **creates a new listing** with the same content,
3. **deletes the old one**, only once the new one exists. If anything fails
   before that, the old listing is untouched.

The new listing starts with zero likes and views. That is how Vinted works.

## The page

Open `http://<your-nas-ip>:8095`. Five tabs:

- **Listings** — your wardrobe. Per item: *Back up*, *Repost* or *Exclude*. Tick
  several to do them all. Repost asks for the price of the new listing (keep it,
  or take 5/10/20% off) and confirms that the old one will be deleted. A repost
  waits a random 30–90 seconds before it runs, so clicks never reach Vinted as a
  burst.
- **Backups** — download as zip, or *Restore* to recreate a listing from a backup
  (your undo). Backups are cleaned up after 30 days; set it to 0 to keep them.
- **Settings** — every option, applied immediately. Also where your Vinted login
  goes, and where you can switch on Telegram messages for failures or reposts.
- **History** — which listing became which, and when.
- **Log** — what the container is doing.

A banner shows whether you are in dry run or live mode, today's repost count and
how long your login is still valid.

## Getting it working

### 1. Two folders on your NAS

One for the login, one for backups, for example:

```
/share/CACHEDEV1_DATA/Docker/vinted-token-refresher/state
/share/CACHEDEV1_DATA/Docker/vinted-reposter/backups
```

If you run the Token Refresher, use its `state` folder. Both containers **must
share it**, so they use one login and never get in each other's way.

### 2. Start the container

In Portainer: *Stacks* → *Add stack* → *Web editor*, paste `docker-compose.yml`
from this repo. The short version, with everything you need to change:

```yaml
services:
  vinted-reposter:
    image: ghcr.io/terrorsource/vintedreposter:latest
    container_name: vinted-reposter
    restart: unless-stopped
    ports:
      - "8095:8080"
    environment:
      - TZ=Europe/Amsterdam
      - WEB_USER=CHANGE_ME                 # login for the page
      - WEB_PASSWORD=CHANGE_ME
      - USER_AGENT=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36
      - DRY_RUN=true                       # switch off under Settings once it looks right
    volumes:
      - /share/CACHEDEV1_DATA/Docker/vinted-token-refresher/state:/state
      - /share/CACHEDEV1_DATA/Docker/vinted-reposter/backups:/data/backups
```

- `WEB_USER` / `WEB_PASSWORD` — the page can delete your listings, so pick
  something real.
- `USER_AGENT` — the *User Agent* line from `chrome://version` in the browser you
  use for Vinted. Vinted checks that your login is used by the same kind of browser.
- The two folder paths — the ones from step 1.

The full `docker-compose.yml` also lists the starting values for every setting
(daily cap, active hours, and so on). You can change all of those on the page
later, so leaving them out here is fine.

Deploy, open `http://<your-nas-ip>:8095`, log in.

### 3. Hand over your Vinted login

The container needs a few cookies from your browser. Easiest: install
[Tampermonkey](https://www.tampermonkey.net/) and add `vinted-state.user.js` from
the [Token Refresher repo](https://github.com/TerrorSource/VintedTokenRefresher).
Open the reposter page once in that browser and log in. Then on vinted.nl choose
**Send to reposter…** from the Tampermonkey menu, enter the reposter's address, done.

By hand instead: on vinted.nl press F12 → *Application* → *Cookies*, copy
`refresh_token_web`, `access_token_web`, `datadome`, `v_udt` and `anon_id` into
**Settings** and press **Save & refresh token**. Be quick: `refresh_token_web` is
single use, and if your browser refreshes first, the copied value is dead.

From here on the container keeps the login alive by itself.

### 4. Dry run, then live

The container starts in **dry run**: Repost logs what it would do and changes
nothing. Once your wardrobe looks right, switch *Dry run* off under Settings.
Reposts still only happen when you click, unless you switch on automatic reposting.

## Automatic reposting

Under Settings. When on, the container reposts by itself: items older than 30
days, at most 3 a day, between 09:00 and 22:00, with a random pause of 2–10
minutes between them. All of that is adjustable, and *Exclude* on the Listings
tab keeps an item out. Start small: reposting your whole wardrobe daily can get
your account banned.

## When something goes wrong

- **Login expired / 401** — send the cookies again (Tampermonkey or Settings).
- **CAPTCHA / 403** — open vinted.nl in your browser, solve the puzzle if there
  is one, send the cookies again. Check that `USER_AGENT` matches your browser.
- **New listing created, old one still there** — a failure halfway; delete the
  old one by hand. The backup is kept.
- **Wardrobe out of date** — it is cached for two minutes; press *Reload*.

## Safety

Set a real `WEB_USER` and `WEB_PASSWORD` and **do not open port 8095 to the
internet**. Your login is stored in `state.json` in the state folder and never
leaves your NAS; the page can only see that a value is stored, not the value.
Backups stay on your NAS too. Neither is in the image or this repository.

## For developers

Python + Flask, served by waitress, talking to Vinted's `/api/v2` endpoints via
`curl_cffi`. No browser engine. Three things that each cost an evening:

- Every write needs an `X-CSRF-Token` header (from the `/items/new` page HTML),
  or Vinted answers `403 access_denied`.
- The create body needs size and condition both inside `item_attributes` and as
  flat `size_id` / `status_id`. Price is a bare number.
- The login refresh needs the `access_token_web` cookie as well, or it returns an
  empty `400`.

Every write to the page's own API must carry `X-Requested-With: vinted-reposter`,
which stops other sites in your browser from using it.

## Disclaimer

Vinted's terms restrict automated use, and mass re-listing can get you banned.
This tool automates what you could do by hand; using it is your own risk.
Provided as-is, no warranty.
