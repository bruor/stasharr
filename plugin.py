import sys
import logging
import urllib.request
import urllib.error
from urllib.parse import quote
from functools import wraps

logger = logging.getLogger(__name__)

# Module-level pointer to the active plugin instance
ACTIVE_PLUGIN = None

# Default settings fallback
DEFAULT_SETTINGS = {
    "enabled": True,
    "streamcache_base_url": "http://streamcache:8080",
    "dispatcharr_stream_url": "http://dispatcharr:9191",
    "bypass_header": "X-Streamcache-Bypass",
}

try:
    from django.http import HttpResponseRedirect
except ImportError:
    class HttpResponseRedirect:
        def __init__(self, redirect_url, status=302):
            self.status_code = status
            self.url = redirect_url
            self.headers = {"Location": redirect_url}

        def __getitem__(self, item):
            return self.headers.get(item)

        def get(self, item, default=None):
            return self.headers.get(item, default)

try:
    from django.db import close_old_connections
except ImportError:
    def close_old_connections():
        pass


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
            "id": "streamcache_base_url",
            "label": "Streamcache Base URL",
            "type": "string",
            "default": "http://streamcache:8080",
            "placeholder": "http://streamcache:8080",
            "help_text": "Base URL of Streamcache as reachable by your XC media players (e.g., https://streamcache.domain.ca or http://192.168.1.50:8080).",
            "description": "Base URL of Streamcache as reachable by your XC media players (e.g., https://streamcache.domain.ca or http://192.168.1.50:8080).",
        },
        {
            "id": "dispatcharr_stream_url",
            "label": "Dispatcharr Base URL for Streamcache",
            "type": "string",
            "default": "http://dispatcharr:9191",
            "placeholder": "http://dispatcharr:9191",
            "help_text": "Base URL of Dispatcharr as reachable by Streamcache (must match DISPATCHARR_STREAM_URL in dp2strm for shared MD5 cache keys).",
            "description": "Base URL of Dispatcharr as reachable by Streamcache (must match DISPATCHARR_STREAM_URL in dp2strm for shared MD5 cache keys).",
        },
        {
            "id": "bypass_header",
            "label": "Bypass Header Name",
            "type": "string",
            "default": "X-Streamcache-Bypass",
            "placeholder": "X-Streamcache-Bypass",
            "help_text": "Optional HTTP request header that disables redirection to prevent loops.",
            "description": "Optional HTTP request header that disables redirection to prevent loops.",
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
        Dynamically wraps `stream_vod` in apps.proxy.vod_proxy.views.
        This intercepts all XC movie and episode requests after user authorization
        and UUID lookup, allowing on-the-fly HTTP 302 redirection to Streamcache.
        """
        vod_views = self._get_vod_views_module()
        if vod_views is None:
            logger.debug("[Stasharr] apps.proxy.vod_proxy.views not importable in this environment.")
            return

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

            # Check for bypass header or bypass query parameter (loop safety)
            bypass_header_name = str(
                ACTIVE_PLUGIN.get_setting("bypass_header", "X-Streamcache-Bypass")
                or "X-Streamcache-Bypass"
            ).strip()
            has_bypass_header = False
            if bypass_header_name:
                if hasattr(request, "headers"):
                    has_bypass_header = bool(request.headers.get(bypass_header_name))
                elif hasattr(request, "META"):
                    django_meta_key = f"HTTP_{bypass_header_name.upper().replace('-', '_')}"
                    has_bypass_header = bool(request.META.get(django_meta_key))

            has_bypass_param = False
            if hasattr(request, "GET"):
                has_bypass_param = bool(request.GET.get("bypass") or request.GET.get("bypass_cache"))

            if has_bypass_header or has_bypass_param:
                logger.info(f"[Stasharr] Bypass flag detected for {req_path}. Falling through to upstream fetch.")
                return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

            # Build Streamcache redirect URL with canonical /proxy/vod/... path
            try:
                redirect_url = ACTIVE_PLUGIN.build_streamcache_redirect_url(content_type, str(content_id))
                logger.info(f"[Stasharr] Intercepted XC {content_type} ({content_id}). Redirecting -> {redirect_url}")
                close_old_connections()
                return HttpResponseRedirect(redirect_url, status=302)
            except Exception as e:
                logger.error(
                    f"[Stasharr] Error generating Streamcache redirect: {e}. Falling back to default stream.",
                    exc_info=True,
                )
                return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

        patched_stream_vod._is_stasharr_patched = True
        vod_views.stream_vod = patched_stream_vod
        logger.info("[Stasharr] stream_vod successfully wrapped for XC VOD interception.")

    def stop(self, context=None):
        """
        Called by Dispatcharr's PluginManager when the plugin is disabled, reloaded, or deleted.
        Restores the original `stream_vod` function and clears the active plugin reference.
        """
        global ACTIVE_PLUGIN
        vod_views = self._get_vod_views_module()
        if vod_views is not None and hasattr(vod_views, "_stasharr_orig_stream_vod"):
            vod_views.stream_vod = vod_views._stasharr_orig_stream_vod
            logger.info("[Stasharr] Restored original stream_vod function.")
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

    def build_streamcache_redirect_url(self, content_type: str, content_uuid: str) -> str:
        """
        Constructs the Streamcache redirect URL:
        http://{streamcache_base}/stream?url={encoded_dispatcharr_url}
        matching the exact MD5 cache key format expected by dp2strm and GoStreamCache.
        """
        sc_base = str(self.get_setting("streamcache_base_url", "http://streamcache:8080") or "http://streamcache:8080").strip().rstrip("/")
        dp_base = str(self.get_setting("dispatcharr_stream_url", "http://dispatcharr:9191") or "http://dispatcharr:9191").strip().rstrip("/")

        # Build canonical internal Dispatcharr target URL
        target_url = f"{dp_base}/proxy/vod/{content_type}/{content_uuid}"

        # URL-encode the target for Streamcache's ?url= query parameter
        escaped_target = quote(target_url, safe="")
        return f"{sc_base}/stream?url={escaped_target}"

    def run(self, action: str, params: dict = None, context: dict = None) -> dict:
        """
        Handles synchronous administrative actions triggered from the Dispatcharr UI.
        """
        if context and isinstance(context.get("settings"), dict):
            self._cached_settings.update(context["settings"])

        if action == "status_check":
            vod_views = self._get_vod_views_module()
            is_hooked = bool(
                vod_views is not None
                and hasattr(vod_views, "_stasharr_orig_stream_vod")
                and getattr(vod_views.stream_vod, "_is_stasharr_patched", False)
            )

            sample_url = self.build_streamcache_redirect_url(
                "movie", "00000000-0000-0000-0000-000000000000"
            )
            return {
                "status": "ok",
                "is_hooked": is_hooked,
                "hook_installed": is_hooked,
                "is_enabled": self.is_enabled(),
                "sample_redirect": sample_url,
                "message": (
                    f"Stasharr is {'active and hooked' if (self.is_enabled() and is_hooked) else 'DISABLED or unhooked'}. "
                    f"Sample redirect: {sample_url}"
                ),
            }

        if action == "ping_streamcache":
            sc_base = str(self.get_setting("streamcache_base_url", "http://streamcache:8080") or "http://streamcache:8080").strip().rstrip("/")
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

        return {"status": "error", "error": f"Unknown action: {action}", "message": f"Unknown action: {action}"}
