#!/usr/bin/env python3
"""
Thin wrapper around Vinted's internal web API (/api/v2).

All Vinted-specific HTTP lives here; reposter.py contains none. If Vinted changes
an endpoint or a field name, this is the only file you need to touch.

Vinted has no public API, so every call below was derived from what the web app
itself sends and then checked against a live account. If one starts failing:
open vinted.nl in Chrome, DevTools -> Network -> filter "api/v2", perform the
action manually once, and compare the request against the code here.
"""
import base64
import json
import re
import uuid

import config

# Vinted sits behind Cloudflare and DataDome, which fingerprint the TLS
# handshake and the HTTP/2 settings, not just the headers. curl_cffi presents
# Chrome's; plain requests has its own and does not speak HTTP/2 at all. It was
# not what broke the refresh in August 2026, but the refresher container already
# runs this way, and the two should not differ in how they look to Vinted.
try:
    from curl_cffi import requests
    IMPERSONATE = "chrome"
except ImportError:                       # still runs, just more recognisable
    import requests
    IMPERSONATE = None


def new_session():
    return requests.Session(**({"impersonate": IMPERSONATE} if IMPERSONATE else {}))


def multipart_kwargs(files, data):
    """
    The photo upload is the one multipart call. requests takes `files=` with
    `(filename, bytes, mimetype)` tuples; curl_cffi refuses that and wants a
    CurlMime instead. Build whichever the client in use understands, with the
    plain form fields alongside.
    """
    if not IMPERSONATE:
        return {"files": files, "data": data}
    from curl_cffi import CurlMime
    form = CurlMime()
    for name, (filename, content, mimetype) in files.items():
        form.addpart(name, content_type=mimetype, filename=filename, data=content)
    for name, value in (data or {}).items():
        form.addpart(name, data=str(value).encode())
    return {"multipart": form}


class VintedError(RuntimeError):
    pass


def cookie_value(jar, name, host=None):
    """
    One value for a cookie name, even when the jar holds several.

    requests raises CookieConflictError from jar.get() as soon as the same name
    exists for more than one domain -- which happens the moment Vinted sets its
    own datadome for .vinted.nl next to the one we injected without a domain.
    The value scoped to the site wins; otherwise the most recently added.
    """
    # requests' jar iterates as Cookie objects; curl_cffi's wrapper iterates as
    # names and keeps the real CookieJar under .jar. Reach for that when present.
    matches = [c for c in getattr(jar, "jar", jar) if c.name == name]
    if not matches:
        return None
    if host:
        for cookie in matches:
            domain = (cookie.domain or "").lstrip(".")
            if domain and (host == domain or host.endswith("." + domain)):
                return cookie.value
    return matches[-1].value


def jwt_claims(token):
    """
    The claims of a Vinted token. Both the access and the refresh token are
    plain JWTs, so their real expiry can be read instead of assumed.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def jwt_expiry(token):
    exp = jwt_claims(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def client_hints(user_agent):
    """
    Derive the Sec-Ch-Ua headers from the user agent.

    These have to agree with each other: a datadome cookie is issued against a
    browser fingerprint, so a cookie copied from Chrome on a Mac is suspect the
    moment it arrives with headers claiming Chrome on Windows. Deriving them
    means changing the user agent in Settings is enough to stay consistent.
    """
    version = re.search(r"Chrome/(\d+)", user_agent)
    version = version.group(1) if version else "138"
    if "Windows" in user_agent:
        platform = "Windows"
    elif "Mac OS X" in user_agent or "Macintosh" in user_agent:
        platform = "macOS"
    elif "Android" in user_agent:
        platform = "Android"
    elif "Linux" in user_agent:
        platform = "Linux"
    else:
        platform = "Unknown"
    return {
        "Sec-Ch-Ua": f'"Not)A;Brand";v="8", "Chromium";v="{version}", '
                     f'"Google Chrome";v="{version}"',
        "Sec-Ch-Ua-Mobile": "?1" if "Mobile" in user_agent else "?0",
        "Sec-Ch-Ua-Platform": f'"{platform}"',
    }


class VintedClient:
    def __init__(self, locale, access_token, user_agent, datadome=None,
                 cf_clearance=None, timeout=30, on_cookies=None):
        self.on_cookies = on_cookies      # called when Vinted hands out fresh cookies
        self.locale = locale
        self.base = f"https://{locale}/api/v2"
        self.timeout = timeout
        self.s = new_session()
        self.s.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Origin": f"https://{locale}",
            "Referer": f"https://{locale}/",
            **client_hints(user_agent),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            # The SPA stamps these on every API call; the API branches on them.
            "Platform": "web",
            "X-Next-App": "marketplace-web",
        })
        # Vinted accepts the session cookie; the Bearer header is what the SPA
        # sends. Sending both is what a real browser tab does.
        self.s.cookies.set("access_token_web", access_token)
        self.s.headers["Authorization"] = f"Bearer {access_token}"
        if datadome:
            self.s.cookies.set("datadome", datadome)
        if cf_clearance:
            self.s.cookies.set("cf_clearance", cf_clearance)
        self.access_token = access_token
        self._wardrobe_path = None       # learned on the first call, then reused
        self._warmed = False
        self._cookies_seen = {"datadome": datadome, "cf_clearance": cf_clearance}

    # ---- low level ------------------------------------------------------

    def _request(self, method, path, **kw):
        if path.startswith("http"):
            url = path
        elif path.startswith(("/api/", "/web/")):     # site-root, e.g. the gateway
            url = f"https://{self.locale}{path}"
        else:
            url = f"{self.base}{path}"                # plain /api/v2 resource
        r = self.s.request(method, url, timeout=self.timeout, **kw)
        self._harvest_cookies()
        if r.status_code == 401:
            raise VintedError(f"401 unauthorized on {method} {path} -- access token expired?")
        if r.status_code == 403:
            body = r.text[:200]
            if "captcha-delivery" in body:
                raise VintedError(
                    f"403 on {method} {path}: DataDome is serving a CAPTCHA. Open "
                    f"vinted.nl in your browser, solve it, and copy the fresh "
                    f"datadome cookie into the refresher's state.json.")
            raise VintedError(
                f"403 forbidden on {method} {path} -- Vinted refused the call "
                f"itself (not a bot check). Body: {body}")
        if r.status_code >= 400:
            raise VintedError(f"{r.status_code} on {method} {path}: {r.text[:400]}")
        if not r.content:
            return {}
        try:
            return r.json()
        except json.JSONDecodeError:
            raise VintedError(f"non-JSON response on {method} {path}: {r.text[:200]}")

    def _cookie(self, name):
        return cookie_value(self.s.cookies, name, self.locale)

    def _harvest_cookies(self):
        """
        Vinted refreshes the datadome cookie on ordinary responses, not just on
        a token refresh. Keeping the newest one is what stops it from ageing
        into a CAPTCHA between refreshes.

        This runs after every request, so it is wrapped whole: bookkeeping must
        never turn a call that succeeded into a failed job.
        """
        try:
            fresh = {name: self._cookie(name) for name in ("datadome", "cf_clearance")}
            changed = {k: v for k, v in fresh.items()
                       if v and v != self._cookies_seen.get(k)}
            if not changed:
                return
            self._cookies_seen.update(changed)
            if self.on_cookies:
                self.on_cookies(changed)
        except Exception as e:
            print(f"[cookies] ignored while harvesting: {e}", flush=True)

    # ---- identity -------------------------------------------------------

    def _token_claim(self, name):
        return jwt_claims(self.access_token).get(name)

    def current_user_id(self):
        """Read the user id straight out of the JWT (no extra request)."""
        uid = self._token_claim("sub") or self._token_claim("user_id")
        if not uid:
            raise VintedError("no user id claim in the access token")
        return int(uid)

    # ---- reads ----------------------------------------------------------

    def list_wardrobe(self, user_id, per_page=100, max_pages=10):
        """All items in your own wardrobe, newest first (Vinted's own order)."""
        items = []
        for page in range(1, max_pages + 1):
            params = {"page": page, "per_page": per_page, "order": "relevance"}
            if self._wardrobe_path is None:
                try:
                    body = self._request("GET", f"/users/{user_id}/items", params=params)
                    self._wardrobe_path = "/users/{}/items"
                except VintedError as e:
                    if "404" not in str(e):        # 401/403 are not a wrong-endpoint problem
                        raise
                    # older deployments expose the same list under /wardrobe/
                    body = self._request("GET", f"/wardrobe/{user_id}/items", params=params)
                    self._wardrobe_path = "/wardrobe/{}/items"
            else:
                body = self._request("GET", self._wardrobe_path.format(user_id), params=params)
            batch = body.get("items", [])
            items.extend(batch)
            if len(batch) < per_page:
                break
        return items

    def get_item(self, item_id):
        """
        Full item in the *upload* shape. Note this is /item_upload/items/{id},
        not /items/{id} -- the latter 404s for the logged-in seller view, and
        this one returns exactly the fields the editor posts back.
        """
        body = self._request("GET", f"/item_upload/items/{item_id}")
        item = body.get("item")
        if not item:
            raise VintedError(f"no item in response for {item_id}")
        return item

    def download_photo(self, url):
        r = requests.get(url, timeout=self.timeout,
                         headers={"User-Agent": self.s.headers["User-Agent"]},
                         **({"impersonate": IMPERSONATE} if IMPERSONATE else {}))
        if r.status_code != 200:
            raise VintedError(f"photo download failed ({r.status_code}): {url}")
        return r.content

    # ---- writes ---------------------------------------------------------

    def warm_session(self):
        """
        Load the upload page once, for the session cookies and -- the part that
        actually matters -- the CSRF token.

        Every write endpoint rejects a request without `X-CSRF-Token` with
        403 {"code":106,"message_code":"access_denied"}, which reads like a
        permissions problem and is really a missing header. The token sits in a
        config blob in the page HTML; the SPA reads it from there too. The page
        load also hands us the anon_id and locale cookies the SPA echoes back as
        headers on every API call.
        """
        if self._warmed:
            return
        html = self.s.get(f"https://{self.locale}/items/new", timeout=self.timeout).text

        match = re.search(r'CSRF_TOKEN\\?"\s*:\s*\\?"([0-9a-fA-F-]{36})', html)
        if match:
            self.s.headers["X-CSRF-Token"] = match.group(1)
        else:
            raise VintedError(
                "could not find the CSRF token on /items/new -- writes will be "
                "refused with access_denied. Vinted may have moved it again.")

        anon_id = self._cookie("anon_id") or self._token_claim("anid")
        if anon_id:
            self.s.headers["X-Anon-Id"] = anon_id
        locale_cookie = self._cookie("anonymous-iso-locale")
        if locale_cookie:
            self.s.headers["Locale"] = locale_cookie
        self._warmed = True

    def upload_photo(self, image_bytes, temp_uuid, filename="photo.jpg"):
        """
        CONFIRMED against a live account. Returns the numeric photo id that
        assigned_photos refers to when the listing is created. All photos of one
        listing share a temp_uuid, which is how Vinted groups them.
        """
        self.warm_session()
        body = self._request(
            "POST", config.PHOTO_UPLOAD_PATH,
            **multipart_kwargs({"photo[file]": (filename, image_bytes, "image/jpeg")},
                               {"photo[type]": "item", "photo[temp_uuid]": temp_uuid}))
        photo_id = (body.get("photo") or body).get("id")
        if not photo_id:
            raise VintedError(f"no photo id in upload response: {json.dumps(body)[:300]}")
        return photo_id

    def create_item(self, item, photo_ids, temp_uuid, price=None):
        """
        CONFIRMED against a live account. Creates a listing and returns its id.
        photo_ids are the numeric ids returned by upload_photo(); price overrides
        the item's own price when the repost lowers it.
        """
        self.warm_session()
        payload = build_upload_payload(item, photo_ids, temp_uuid, price=price)
        body = self._request("POST", config.ITEM_CREATE_PATH, json=payload)
        new_id = (body.get("item") or {}).get("id")
        if not new_id:
            raise VintedError(f"no item id in create response: {json.dumps(body)[:300]}")
        return new_id

    def delete_item(self, item_id):
        """CONFIRMED. DELETE on the item; POST .../delete is a fallback."""
        path = config.ITEM_DELETE_PATH.format(id=item_id)
        try:
            return self._request("DELETE", path)
        except VintedError as e:
            if "404" not in str(e):
                raise
            return self._request("POST", path.rstrip("/") + "/delete")


# ---- read shape -> write shape ------------------------------------------

def price_amount(item):
    """Vinted returns {"amount": "20.0", "currency_code": "EUR"}; older shapes are scalar."""
    price = item.get("price")
    if isinstance(price, dict):
        return price.get("amount"), price.get("currency_code") or item.get("currency") or "EUR"
    return price, item.get("currency") or "EUR"


def _item_attributes(item):
    """
    The editor *returns* size and condition as `size_id` / `status_id`, but
    *posts* them as attributes. Anything else already on the item (material and
    friends) is carried over untouched.
    """
    by_code = {a["code"]: dict(a) for a in (item.get("item_attributes") or [])
               if a.get("code")}
    for code, source in (("size", "size_id"), ("condition", "status_id"),
                         ("material", None)):
        entry = by_code.setdefault(code, {"code": code, "ids": []})
        if source and not entry.get("ids") and item.get(source):
            entry["ids"] = [item[source]]
    order = ["size", "condition", "material"]
    return ([by_code[c] for c in order]
            + [v for k, v in by_code.items() if k not in order])


def build_upload_payload(item, photo_ids, temp_uuid, price=None):
    """
    Builds the body for POST /api/v2/item_upload/items, field for field as the
    editor sends it.

    Note the price goes out as a bare number even though the read shape returns
    {"amount": "5.0", ...}, and that colours/size/condition are restructured:
    the read shape has color1_id, size_id and status_id, the write shape wants
    color_ids plus item_attributes.
    """
    amount, currency = price_amount(item)
    price = float(amount if price is None else price)
    if price.is_integer():
        price = int(price)
    return {
        "item": {
            "id": None,
            "currency": currency,
            "temp_uuid": temp_uuid,
            "title": item.get("title"),
            "description": item.get("description"),
            "brand_id": item.get("brand_id"),
            "brand": (item.get("brand_dto") or {}).get("title") or item.get("brand"),
            "catalog_id": item.get("catalog_id"),
            "isbn": item.get("isbn"),
            "is_unisex": bool(item.get("is_unisex")),
            "ai_photo": False,
            "price": price,
            "package_size_id": item.get("package_size_id"),
            "shipment_prices": {"domestic": None, "international": None},
            "color_ids": [c for c in (item.get("color1_id"), item.get("color2_id")) if c],
            "assigned_photos": [{"id": pid, "orientation": 0} for pid in photo_ids],
            "measurement_length": item.get("measurement_length"),
            "measurement_width": item.get("measurement_width"),
            "item_attributes": _item_attributes(item),
            # The browser only sends the attributes, but Vinted answers
            # "Vul je maat in" to a body that carries nothing else -- its
            # validator reads these. Sending both satisfies either reading.
            "size_id": item.get("size_id"),
            "status_id": item.get("status_id"),
            "manufacturer": item.get("manufacturer"),
            "manufacturer_labelling": item.get("manufacturer_labelling"),
        },
        "push_up": False,          # never buy a paid bump on our behalf
        "parcel": None,
        "upload_session_id": temp_uuid,
    }


def photo_urls(item):
    """Highest-resolution URL for each photo, in the order they are shown."""
    photos = sorted(item.get("photos") or [], key=lambda p: p.get("image_no") or 0)
    return [url for url in
            ((p.get("full_size_url") or p.get("url")) for p in photos) if url]


def photo_keys(item):
    """
    Vinted's own identity for each photo, in order. Used to notice that the
    photos of a listing changed since the last backup.
    """
    photos = sorted(item.get("photos") or [], key=lambda p: p.get("image_no") or 0)
    return [str((p.get("high_resolution") or {}).get("id") or p.get("id") or p.get("url"))
            for p in photos]


def photo_timestamp(item):
    """Upload time of the first photo -- the closest thing to a listing date."""
    for p in sorted(item.get("photos") or [], key=lambda p: p.get("image_no") or 0):
        ts = (p.get("high_resolution") or {}).get("timestamp")
        if isinstance(ts, (int, float)):
            return ts
    return None


def thumbnail_url(item, prefer="thumb310x430"):
    """Small image for the overview; falls back to the full-size photo."""
    photos = sorted(item.get("photos") or [], key=lambda p: p.get("image_no") or 0)
    if not photos:
        return None
    for t in photos[0].get("thumbnails") or []:
        if t.get("type") == prefer and t.get("url"):
            return t["url"]
    return photos[0].get("url") or photos[0].get("full_size_url")


def new_temp_uuid():
    return str(uuid.uuid4())


