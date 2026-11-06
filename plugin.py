import sys
import logging
import time
import random
import urllib.request
import urllib.error
from urllib.parse import quote
from functools import wraps
import requests

logger = logging.getLogger(__name__)

# Module-level pointer to the active plugin instance
ACTIVE_PLUGIN = None

# Default settings fallback
DEFAULT_SETTINGS = {
    "enabled": True,
    "delivery_mode": "proxy",
    "streamcache_session_timeout": 120,
    "streamcache_base_url": "http://streamcache:8080",
    "dispatcharr_stream_url": "http://dispatcharr:9191",
}

try:
    from django.http import HttpResponseRedirect, StreamingHttpResponse, JsonResponse
except ImportError:
    class _HeaderDict(dict):
        def __getitem__(self, key):
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
            raise KeyError(key)

        def __setitem__(self, key, value):
            for k in list(self.keys()):
                if k.lower() == key.lower():
                    super().__delitem__(k)
                    break
            super().__setitem__(key, str(value))

        def __delitem__(self, key):
            for k in list(self.keys()):
                if k.lower() == key.lower():
                    super().__delitem__(k)
                    return
            raise KeyError(key)

        def get(self, key, default=None):
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
            return default

        def __contains__(self, key):
            return any(k.lower() == key.lower() for k in self.keys())

    class HttpResponseRedirect:
        def __init__(self, redirect_url, status=302):
            self.status_code = status
            self.url = redirect_url
            self.headers = _HeaderDict({"Location": redirect_url})

        def __getitem__(self, item):
            return self.headers[item]

        def __setitem__(self, item, value):
            self.headers[item] = value

        def get(self, item, default=None):
            return self.headers.get(item, default)

        def __contains__(self, item):
            return item in self.headers

    class StreamingHttpResponse:
        def __init__(self, streaming_content=(), status=200, content_type=None):
            self.streaming_content = streaming_content
            self.status_code = status
            self.headers = _HeaderDict()
            if content_type:
                self.headers["Content-Type"] = content_type

        def __getitem__(self, item):
            return self.headers[item]

        def __setitem__(self, item, value):
            self.headers[item] = value

        def get(self, item, default=None):
            return self.headers.get(item, default)

        def __contains__(self, item):
            return item in self.headers

    class JsonResponse:
        def __init__(self, data, status=200):
            import json
            self.status_code = status
            self.content = json.dumps(data)
            self._data = data
            self.headers = _HeaderDict({"Content-Type": "application/json"})

        def __getitem__(self, item):
            return self.headers[item]

        def __setitem__(self, item, value):
            self.headers[item] = value

        def get(self, item, default=None):
            return self.headers.get(item, default)

        def __contains__(self, item):
            return item in self.headers

        def json(self):
            return self._data

try:
    from django.db import close_old_connections
except ImportError:
    def close_old_connections():
        pass


def _extract_req_header(req, header_name: str, meta_name: str):
    if hasattr(req, "headers") and req.headers is not None:
        if hasattr(req.headers, "get"):
            v = req.headers.get(header_name) or req.headers.get(header_name.lower())
            if v:
                return v
        if isinstance(req.headers, dict):
            for k, v in req.headers.items():
                if k.lower() == header_name.lower():
                    return v
    if hasattr(req, "META") and req.META is not None:
        if meta_name in req.META:
            return req.META[meta_name]
        alt_meta = "HTTP_" + header_name.upper().replace("-", "_")
        if alt_meta in req.META:
            return req.META[alt_meta]
    return None


class Plugin:
    name = "Stasharr"
    version = "1.0.0"
    description = "Intercepts Xtream Codes (XC) VOD playback requests and redirects players to Streamcache using canonical Dispatcharr playback URLs."
    author = "bruor"

    fields = [
        {
            "id": "enabled",
            "label": "Enable Stasharr Interceptor",
            "type": "boolean",
            "default": True,
            "help_text": "Master switch to enable or disable XC VOD redirection to Streamcache.",
            "description": "Master switch to enable or disable XC VOD redirection to Streamcache.",
        },
        {
            "id": "delivery_mode",
            "type": "select",
            "label": "Delivery Mode",
            "description": "Choose delivery mode: 'Proxy' (Default) in-line streams through Dispatcharr with native stream termination and zero Nginx buffering; 'Redirect' offloads to Streamcache with HTTP 429 gate rejection.",
            "help_text": "In Proxy mode, Dispatcharr manages the connection, supporting native 'Terminate on Limit Exceeded' to kill victim streams. In Redirect mode, streams are offloaded to Streamcache; Dispatcharr cannot terminate external connections, so limit enforcement acts as an admission gate that rejects new streams with HTTP 429.",
            "default": "proxy",
            "options": [
                {
                    "label": "Proxy (Default - Native Connection Limits, Stream Termination & Zero Nginx Buffering)",
                    "value": "proxy",
                },
                {
                    "label": "Redirect (Direct Streamcache Offload & HTTP 429 Abuse Gate)",
                    "value": "redirect",
                },
            ],
        },
        {
            "id": "streamcache_session_timeout",
            "type": "integer",
            "label": "Streamcache Idle Lease (seconds)",
            "description": "Only used in Redirect Mode. Duration an idle VOD session remains reserved in Streamcache during buffer pauses.",
            "default": 120,
        },
        {
            "id": "streamcache_base_url",
            "label": "Streamcache Base URL",
            "type": "string",
            "default": "http://streamcache:8080",
            "placeholder": "https://streamcache.yourdomain.ca",
            "help_text": "The publicly accessible Streamcache endpoint that XC clients will be redirected to (e.g., https://streamcache.yourdomain.ca).",
            "description": "The publicly accessible Streamcache endpoint that XC clients will be redirected to (e.g., https://streamcache.yourdomain.ca).",
        },
        {
            "id": "dispatcharr_stream_url",
            "label": "Dispatcharr Base URL",
            "type": "string",
            "default": "http://dispatcharr:9191",
            "placeholder": "https://dispatcharr.yourdomain.ca",
            "help_text": "The canonical Dispatcharr base URL passed inside the target ?url= parameter that Streamcache uses to fetch media via /proxy/vod/... on cache misses. Setting this explicitly to match dp2strm ensures all requests use identical pathing and produce identical MD5 cache keys regardless of how a client connected.",
            "description": "The canonical Dispatcharr base URL passed inside the target ?url= parameter that Streamcache uses to fetch media via /proxy/vod/... on cache misses. Setting this explicitly to match dp2strm ensures all requests use identical pathing and produce identical MD5 cache keys regardless of how a client connected.",
        },
    ]

    actions = [
        {
            "id": "status_check",
            "label": "Check Hook Status",
            "description": "Verify interceptor status, active settings, and runtime method wrapping state.",
            "button_label": "Check Status",
            "button_variant": "filled",
            "button_color": "blue",
        },
        {
            "id": "ping_streamcache",
            "label": "Ping Streamcache",
            "description": "Test HTTP connectivity from Dispatcharr to the configured Streamcache URL.",
            "button_label": "Ping Streamcache",
            "button_variant": "outline",
            "button_color": "green",
        },
    ]

    def __init__(self):
        global ACTIVE_PLUGIN
        ACTIVE_PLUGIN = self
        self._cached_settings = {}
        self._user_cache = {}
        self._sessions_cache = {}
        self._inject_hooks()
        logger.info("[Stasharr] Plugin initialized and registered as active instance.")

    @staticmethod
    def _get_vod_views_module():
        vod_views = sys.modules.get("apps.proxy.vod_proxy.views")
        if vod_views is None:
            try:
                import apps.proxy.vod_proxy.views as vod_views
            except ImportError:
                return None
        return vod_views

    @staticmethod
    def _get_proxy_utils_module():
        proxy_utils = sys.modules.get("apps.proxy.utils")
        if proxy_utils is None:
            try:
                import apps.proxy.utils as proxy_utils
            except ImportError:
                return None
        return proxy_utils

    def resolve_username(self, user_id):
        """Resolves username for a given user or user_id."""
        if user_id is None or type(user_id).__name__.startswith("MagicMock"):
            return None
        if hasattr(user_id, "username"):
            raw = getattr(user_id, "username", None)
            if raw and not type(raw).__name__.startswith("MagicMock"):
                return str(raw)
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            from apps.accounts.models import User
            u = User.objects.filter(id=user_id).first()
            if u and hasattr(u, "username"):
                uname = str(u.username)
                self._user_cache[user_id] = uname
                return uname
        except Exception:
            pass
        if isinstance(user_id, str):
            return user_id
        return str(user_id)

    def get_streamcache_sessions(self, username: str) -> list:
        """
        Queries Streamcache GET /api/sessions?user={username} with a 2-second in-memory cache.
        """
        if not username:
            return []
        now = time.time()
        cached = self._sessions_cache.get(username)
        if cached is not None:
            ts, data = cached
            if now - ts < 2.0:
                return data

        sc_base = str(
            self.get_setting("streamcache_base_url", "http://streamcache:8080")
            or "http://streamcache:8080"
        ).strip().rstrip("/")
        url = f"{sc_base}/api/sessions?user={quote(str(username))}"
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                raw_data = resp.json()
                if isinstance(raw_data, dict):
                    data = raw_data.get("sessions", [])
                    if not isinstance(data, list):
                        data = []
                elif isinstance(raw_data, list):
                    data = raw_data
                else:
                    data = []
                self._sessions_cache[username] = (now, data)
                return data
            else:
                logger.warning(
                    f"[Stasharr] Streamcache /api/sessions returned HTTP {resp.status_code} for user {username}"
                )
                return []
        except Exception as e:
            logger.warning(
                f"[Stasharr] Error querying Streamcache sessions for user {username}: {e}"
            )
            if cached is not None:
                return cached[1]
            return []

    def _get_plugin_config(self):
        """
        Look up the persisted PluginConfig row from Dispatcharr's database.
        Matches by module key (if loaded as `_dispatcharr_plugin_<key>.plugin` or `plugins.<key>`),
        `key='stasharr'`, or `name='Stasharr'`.
        """
        try:
            from apps.plugins.models import PluginConfig

            if __name__.startswith("_dispatcharr_plugin_"):
                runtime_key = __name__[len("_dispatcharr_plugin_"):].split(".", 1)[0]
                config = PluginConfig.objects.filter(key=runtime_key).first()
                if config is not None:
                    return config

            if __name__.startswith("plugins."):
                runtime_key = __name__.split(".", 1)[1]
                config = PluginConfig.objects.filter(key=runtime_key).first()
                if config is not None:
                    return config

            config = PluginConfig.objects.filter(key__iexact="stasharr").first()
            if config is not None:
                return config

            return PluginConfig.objects.filter(name__iexact="stasharr").first()
        except Exception:
            return None

    def _inject_hooks(self):
        """
        Dynamically wraps `stream_vod` and `get_user_active_connections`.
        """
        vod_views = self._get_vod_views_module()
        if vod_views is not None:
            # Store the original unwrapped stream_vod function once
            if not hasattr(vod_views, "_stasharr_orig_stream_vod"):
                vod_views._stasharr_orig_stream_vod = vod_views.stream_vod

            orig_stream_vod = vod_views._stasharr_orig_stream_vod

            @wraps(orig_stream_vod)
            def patched_stream_vod(request, content_type, content_id, *args, **kwargs):
                # Check if active plugin instance is enabled
                if not ACTIVE_PLUGIN or not ACTIVE_PLUGIN.is_enabled():
                    return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

                # Check if incoming request is from an XC client route (/movie/ or /series/)
                req_path = getattr(request, "path", "") or ""
                is_xc_route = req_path.startswith("/movie/") or req_path.startswith("/series/")

                # Do not intercept internal Dispatcharr routes (/proxy/vod/...) or non-XC requests
                if not is_xc_route:
                    return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

                # Resolve user object from kwargs.get('user'), args, or request
                user = kwargs.get("user")
                if user is None:
                    if len(args) >= 3 and args[2] is not None:
                        user = args[2]
                    else:
                        for a in args:
                            if hasattr(a, "username") and not isinstance(a, type):
                                user = a
                                break
                if user is None:
                    if hasattr(request, "user") and request.user is not None:
                        # If Django request, verify authenticated user or not MagicMock
                        u = request.user
                        if not isinstance(u, type(None)):
                            user = u
                    elif hasattr(request, "_request") and hasattr(request._request, "user"):
                        user = request._request.user

                username = None
                if user is not None:
                    raw_name = None
                    if hasattr(user, "username"):
                        raw_name = getattr(user, "username", None)
                    elif isinstance(user, dict):
                        raw_name = user.get("username")
                    if raw_name is not None and not type(raw_name).__name__.startswith("MagicMock"):
                        username = str(raw_name)

                user_id = None
                if user is not None:
                    raw_id = getattr(user, "id", None) if not isinstance(user, dict) else user.get("id")
                    if raw_id is not None and not type(raw_id).__name__.startswith("MagicMock"):
                        user_id = raw_id

                if user_id is not None and username:
                    ACTIVE_PLUGIN._user_cache[user_id] = username

                delivery_mode = ACTIVE_PLUGIN.get_setting("delivery_mode", "proxy")

                if delivery_mode == "redirect":
                    try:
                        raw_limit = getattr(user, "stream_limit", 0) if user else 0
                        stream_limit = (
                            int(raw_limit)
                            if (isinstance(raw_limit, (int, float)) and not type(raw_limit).__name__.startswith("MagicMock"))
                            else 0
                        )
                        if stream_limit > 0:
                            proxy_utils = ACTIVE_PLUGIN._get_proxy_utils_module()
                            get_conns_func = (
                                getattr(proxy_utils, "get_user_active_connections", None)
                                if proxy_utils
                                else None
                            )
                            lookup_id = user_id or username
                            if get_conns_func is not None:
                                active_conns = get_conns_func(lookup_id)
                            else:
                                active_conns = []
                                if username:
                                    sc_sessions = ACTIVE_PLUGIN.get_streamcache_sessions(username)
                                    for s in sc_sessions:
                                        active_conns.append({
                                            "media_id": s["key"],
                                            "client_id": s["id"],
                                            "connected_at": time.time(),
                                            "type": "vod",
                                        })

                            if len(active_conns) >= stream_limit:
                                logger.warning(
                                    f"[Stasharr] User {username} exceeded stream limit "
                                    f"({stream_limit} allowed, {len(active_conns)} active). Returning 429."
                                )
                                return JsonResponse(
                                    {
                                        "error": f"Stream limit exceeded ({stream_limit} concurrent streams allowed)"
                                    },
                                    status=429,
                                )

                        redirect_url = ACTIVE_PLUGIN.build_streamcache_redirect_url(
                            content_type, str(content_id), username=username
                        )
                        logger.info(
                            f"[Stasharr] Intercepted XC {content_type} ({content_id}). Redirecting -> {redirect_url}"
                        )
                        close_old_connections()
                        return HttpResponseRedirect(redirect_url, status=302)
                    except Exception as e:
                        logger.error(
                            f"[Stasharr] Error generating Streamcache redirect: {e}. Falling back to default stream.",
                            exc_info=True,
                        )
                        return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

                # Default to Proxy mode
                try:
                    # Resolve or mint session_id
                    session_id = None
                    if hasattr(request, "GET") and not type(request.GET).__name__.startswith("MagicMock"):
                        if hasattr(request.GET, "get"):
                            raw_sess = request.GET.get("session_id")
                            if raw_sess and not type(raw_sess).__name__.startswith("MagicMock"):
                                session_id = str(raw_sess)
                    if not session_id and kwargs.get("session_id"):
                        raw_sess = kwargs.get("session_id")
                        if raw_sess and not type(raw_sess).__name__.startswith("MagicMock"):
                            session_id = str(raw_sess)
                    if not session_id and len(args) >= 4 and args[3]:
                        raw_sess = args[3]
                        if raw_sess and not type(raw_sess).__name__.startswith("MagicMock"):
                            session_id = str(raw_sess)
                    if not session_id:
                        session_id = f"vod_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

                    # Enforce user stream limits in Proxy mode
                    raw_limit = getattr(user, "stream_limit", 0) if user else 0
                    stream_limit = (
                        int(raw_limit)
                        if (isinstance(raw_limit, (int, float)) and not type(raw_limit).__name__.startswith("MagicMock"))
                        else 0
                    )
                    if stream_limit > 0:
                        proxy_utils = ACTIVE_PLUGIN._get_proxy_utils_module()
                        check_limits_func = (
                            getattr(proxy_utils, "check_user_stream_limits", None)
                            if proxy_utils
                            else None
                        )
                        if check_limits_func is not None:
                            allowed = check_limits_func(user, session_id, media_id=str(content_id))
                            if not allowed:
                                logger.warning(
                                    f"[Stasharr] User {username} exceeded stream limit via check_user_stream_limits. Returning 429."
                                )
                                return JsonResponse(
                                    {
                                        "error": f"Stream limit exceeded ({stream_limit} concurrent streams allowed)"
                                    },
                                    status=429,
                                )
                        else:
                            get_conns_func = (
                                getattr(proxy_utils, "get_user_active_connections", None)
                                if proxy_utils
                                else None
                            )
                            active_conns = get_conns_func(user_id or username) if get_conns_func else []
                            if len(active_conns) >= stream_limit:
                                logger.warning(
                                    f"[Stasharr] User {username} exceeded stream limit ({len(active_conns)}/{stream_limit} active). Returning 429."
                                )
                                return JsonResponse(
                                    {
                                        "error": f"Stream limit exceeded ({stream_limit} concurrent streams allowed)"
                                    },
                                    status=429,
                                )

                    upstream_url = ACTIVE_PLUGIN.build_streamcache_redirect_url(
                        content_type, str(content_id), username=username
                    )
                    logger.info(
                        f"[Stasharr] Intercepted XC {content_type} ({content_id}) in proxy mode -> {upstream_url}"
                    )

                    forward_headers = {}
                    for hdr_name, meta_name in [
                        ("Range", "HTTP_RANGE"),
                        ("If-Range", "HTTP_IF_RANGE"),
                        ("User-Agent", "HTTP_USER_AGENT"),
                    ]:
                        val = _extract_req_header(request, hdr_name, meta_name)
                        if val:
                            forward_headers[hdr_name] = str(val)

                    upstream_resp = requests.get(
                        upstream_url,
                        headers=forward_headers,
                        stream=True,
                        timeout=10,
                    )

                    # Connect to Redis to register VOD session state for connection tracking
                    redis_client = None
                    try:
                        from core.utils import RedisClient
                        redis_client = RedisClient.get_client()
                    except Exception:
                        pass

                    conn_key = f"vod_persistent_connection:{session_id}"
                    stop_key = f"vod_client_stop_key:{session_id}"
                    client_ip = ""
                    if hasattr(request, "META") and request.META:
                        client_ip = request.META.get("REMOTE_ADDR", "") or request.META.get("HTTP_X_FORWARDED_FOR", "")

                    if redis_client is not None and user_id is not None:
                        conn_data = {
                            "user_id": str(user_id),
                            "username": str(username) if username else "",
                            "content_uuid": str(content_id),
                            "content_obj_type": str(content_type),
                            "client_id": str(session_id),
                            "client_ip": str(client_ip),
                            "created_at": str(time.time()),
                            "last_activity": str(time.time()),
                            "active_streams": "1",
                        }
                        try:
                            if hasattr(redis_client, "hset"):
                                redis_client.hset(conn_key, mapping=conn_data)
                            elif hasattr(redis_client, "hmset"):
                                redis_client.hmset(conn_key, conn_data)
                            if hasattr(redis_client, "expire"):
                                redis_client.expire(conn_key, 60)
                            logger.debug(f"[Stasharr] Registered VOD connection {conn_key} for user {username} in Redis.")
                        except Exception as e:
                            logger.warning(f"[Stasharr] Error registering Redis VOD connection {conn_key}: {e}")

                    def stream_iterator():
                        last_refresh = time.time()
                        try:
                            for chunk in upstream_resp.iter_content(chunk_size=65536):
                                if not chunk:
                                    continue
                                now = time.time()
                                if redis_client is not None and user_id is not None and now - last_refresh > 15:
                                    last_refresh = now
                                    try:
                                        if hasattr(redis_client, "exists") and redis_client.exists(stop_key):
                                            logger.info(f"[Stasharr] Terminating VOD stream for {session_id} per stop key.")
                                            break
                                        if hasattr(redis_client, "expire"):
                                            redis_client.expire(conn_key, 60)
                                    except Exception:
                                        pass
                                yield chunk
                        finally:
                            try:
                                upstream_resp.close()
                            except Exception:
                                pass
                            if redis_client is not None and user_id is not None:
                                try:
                                    if hasattr(redis_client, "delete"):
                                        redis_client.delete(conn_key)
                                        redis_client.delete(stop_key)
                                except Exception:
                                    pass

                    response = StreamingHttpResponse(
                        stream_iterator(),
                        status=upstream_resp.status_code,
                    )
                    response.status_code = upstream_resp.status_code
                    response["X-Accel-Buffering"] = "no"

                    for hdr in ("Content-Type", "Content-Range", "Content-Length", "Accept-Ranges"):
                        if hdr in upstream_resp.headers:
                            response[hdr] = upstream_resp.headers[hdr]

                    response["Cache-Control"] = "no-cache"
                    response["Connection"] = "keep-alive"
                    close_old_connections()
                    return response
                except Exception as e:
                    logger.error(
                        f"[Stasharr] Error proxying stream from Streamcache: {e}. Falling back to default stream.",
                        exc_info=True,
                    )
                    return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

            patched_stream_vod._is_stasharr_patched = True
            vod_views.stream_vod = patched_stream_vod
            logger.info("[Stasharr] stream_vod successfully wrapped for XC VOD interception.")

        proxy_utils = self._get_proxy_utils_module()
        if proxy_utils is not None:
            if not hasattr(proxy_utils, "_stasharr_orig_get_user_active_connections"):
                proxy_utils._stasharr_orig_get_user_active_connections = proxy_utils.get_user_active_connections

            orig_get_conns = proxy_utils._stasharr_orig_get_user_active_connections

            @wraps(orig_get_conns)
            def patched_get_user_active_connections(user_id=None, *args, **kwargs):
                conns = orig_get_conns(user_id, *args, **kwargs)
                connections = list(conns) if conns is not None else []

                if not ACTIVE_PLUGIN or not ACTIVE_PLUGIN.is_enabled():
                    return connections

                delivery_mode = ACTIVE_PLUGIN.get_setting("delivery_mode", "proxy")
                if delivery_mode == "redirect" and user_id:
                    username = ACTIVE_PLUGIN.resolve_username(user_id)
                    if username:
                        sc_sessions = ACTIVE_PLUGIN.get_streamcache_sessions(username)
                        for s in sc_sessions:
                            connections.append({
                                "media_id": s["key"],
                                "client_id": s["id"],
                                "connected_at": time.time(),
                                "type": "vod",
                            })

                return connections

            patched_get_user_active_connections._is_stasharr_patched = True
            proxy_utils.get_user_active_connections = patched_get_user_active_connections

            for mod_name in ("apps.output.views", "apps.timeshift.views"):
                mod = sys.modules.get(mod_name)
                if mod is not None and hasattr(mod, "get_user_active_connections"):
                    if not hasattr(mod, "_stasharr_orig_get_user_active_connections"):
                        mod._stasharr_orig_get_user_active_connections = mod.get_user_active_connections
                    mod.get_user_active_connections = patched_get_user_active_connections

            logger.info("[Stasharr] get_user_active_connections successfully wrapped.")

    def stop(self, context=None):
        """
        Called by Dispatcharr's PluginManager when the plugin is disabled, reloaded, or deleted.
        Restores original functions and clears the active plugin reference.
        """
        global ACTIVE_PLUGIN
        vod_views = self._get_vod_views_module()
        if vod_views is not None and hasattr(vod_views, "_stasharr_orig_stream_vod"):
            vod_views.stream_vod = vod_views._stasharr_orig_stream_vod
            delattr(vod_views, "_stasharr_orig_stream_vod")
            logger.info("[Stasharr] Restored original stream_vod function.")

        proxy_utils = self._get_proxy_utils_module()
        if proxy_utils is not None and hasattr(proxy_utils, "_stasharr_orig_get_user_active_connections"):
            proxy_utils.get_user_active_connections = proxy_utils._stasharr_orig_get_user_active_connections
            delattr(proxy_utils, "_stasharr_orig_get_user_active_connections")
            logger.info("[Stasharr] Restored original get_user_active_connections function.")

        for mod_name in ("apps.output.views", "apps.timeshift.views"):
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, "_stasharr_orig_get_user_active_connections"):
                mod.get_user_active_connections = mod._stasharr_orig_get_user_active_connections
                delattr(mod, "_stasharr_orig_get_user_active_connections")

        if ACTIVE_PLUGIN is self:
            ACTIVE_PLUGIN = None
        logger.info(f"[Stasharr] Plugin stopped (context={context}).")

    def get_setting(self, key: str, default=None):
        """
        Retrieves a plugin setting.
        Always queries Dispatcharr's persisted PluginConfig first so settings updates
        made in the UI take effect immediately across all uWSGI worker processes.
        Falls back to in-memory `_cached_settings` (for standalone tests/overrides)
        and then `DEFAULT_SETTINGS`.
        """
        config = self._get_plugin_config()
        if config is not None and isinstance(config.settings, dict):
            val = config.settings.get(key)
            if val is not None:
                return val

        if key in self._cached_settings:
            return self._cached_settings[key]

        return DEFAULT_SETTINGS.get(key, default)

    def is_enabled(self) -> bool:
        """
        Returns True if the plugin is active both in Dispatcharr's DB model and in plugin settings.
        """
        config = self._get_plugin_config()
        if config is not None and not config.enabled:
            return False

        val = self.get_setting("enabled", True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    def build_streamcache_redirect_url(
        self, content_type: str, content_uuid: str, username: str = None
    ) -> str:
        """
        Constructs the Streamcache redirect URL:
        http://{streamcache_base}/stream?url={encoded_dispatcharr_url}[&user={username}]
        matching the exact MD5 cache key format expected by dp2strm and Streamcache.
        """
        sc_base = str(
            self.get_setting("streamcache_base_url", "http://streamcache:8080")
            or "http://streamcache:8080"
        ).strip().rstrip("/")
        dp_base = str(
            self.get_setting("dispatcharr_stream_url", "http://dispatcharr:9191")
            or "http://dispatcharr:9191"
        ).strip().rstrip("/")

        # Build canonical Dispatcharr target URL
        target_url = f"{dp_base}/proxy/vod/{content_type}/{content_uuid}"

        # URL-encode the target for Streamcache's ?url= query parameter
        escaped_target = quote(target_url, safe="")
        url = f"{sc_base}/stream?url={escaped_target}"
        if username:
            url += f"&user={quote(str(username))}"
        return url

    def run(self, action: str, params: dict = None, context: dict = None) -> dict:
        """
        Handles synchronous administrative actions triggered from the Dispatcharr UI.
        """
        if context and isinstance(context.get("settings"), dict):
            self._cached_settings.update(context["settings"])

        if action == "status_check":
            vod_views = self._get_vod_views_module()
            is_vod_hooked = bool(
                vod_views is not None
                and hasattr(vod_views, "_stasharr_orig_stream_vod")
                and getattr(vod_views.stream_vod, "_is_stasharr_patched", False)
            )
            proxy_utils = self._get_proxy_utils_module()
            is_utils_hooked = bool(
                proxy_utils is not None
                and hasattr(proxy_utils, "_stasharr_orig_get_user_active_connections")
                and getattr(
                    proxy_utils.get_user_active_connections, "_is_stasharr_patched", False
                )
            )

            sample_url = self.build_streamcache_redirect_url(
                "movie", "00000000-0000-0000-0000-000000000000"
            )
            delivery_mode = self.get_setting("delivery_mode", "proxy")
            return {
                "status": "ok",
                "is_hooked": is_vod_hooked,
                "hook_installed": is_vod_hooked,
                "connections_hooked": is_utils_hooked,
                "delivery_mode": delivery_mode,
                "is_enabled": self.is_enabled(),
                "sample_redirect": sample_url,
                "message": (
                    f"Stasharr is {'active and hooked' if (self.is_enabled() and is_vod_hooked) else 'DISABLED or unhooked'} "
                    f"in {delivery_mode} mode. Sample redirect: {sample_url}"
                ),
            }

        if action == "ping_streamcache":
            sc_base = str(
                self.get_setting("streamcache_base_url", "http://streamcache:8080")
                or "http://streamcache:8080"
            ).strip().rstrip("/")
            ping_url = f"{sc_base}/"
            try:
                req = urllib.request.Request(ping_url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    code = resp.getcode()
            except urllib.error.HTTPError as e:
                # Streamcache root returns 404/400 when no route matches /, which proves HTTP reachability
                code = e.code
            except Exception as e:
                return {
                    "status": "error",
                    "error": f"Streamcache unreachable at {ping_url}: {e}",
                    "message": f"Streamcache unreachable at {ping_url}: {e}",
                }

            return {
                "status": "ok",
                "message": f"Successfully reached Streamcache at {sc_base} (HTTP {code}).",
            }

        return {
            "status": "error",
            "error": f"Unknown action: {action}",
            "message": f"Unknown action: {action}",
        }
