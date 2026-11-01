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
    "api_key": "",
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
            "description": "Master switch to enable or disable XC VOD redirection to Streamcache.",
        },
        {
            "id": "streamcache_base_url",
            "label": "Streamcache Base URL",
            "type": "string",
            "default": "http://streamcache:8080",
            "description": "Base URL of Streamcache as reachable by your XC media players (e.g., http://streamcache:8080 or http://192.168.1.50:8080).",
        },
        {
            "id": "dispatcharr_stream_url",
            "label": "Dispatcharr Base URL for Streamcache",
            "type": "string",
            "default": "http://dispatcharr:9191",
            "description": "Base URL of Dispatcharr as reachable by Streamcache (e.g., http://dispatcharr:9191 or http://host.docker.internal:9191).",
        },
        {
            "id": "api_key",
            "label": "Dispatcharr API Key",
            "type": "string",
            "default": "",
            "description": "API key used by Streamcache to authenticate with Dispatcharr (/proxy/vod/...) on cache misses.",
        },
        {
            "id": "bypass_header",
            "label": "Bypass Header Name",
            "type": "string",
            "default": "X-Streamcache-Bypass",
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

    def _inject_hooks(self):
        """
        Dynamically wraps `stream_vod` in apps.proxy.vod_proxy.views.
        This intercepts all XC movie and episode requests after user authorization
        and UUID lookup, allowing on-the-fly HTTP 302 redirection to Streamcache.
        """
        vod_views = sys.modules.get("apps.proxy.vod_proxy.views")
        if vod_views is None:
            try:
                import apps.proxy.vod_proxy.views as vod_views
            except ImportError:
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
            bypass_header_name = ACTIVE_PLUGIN.get_setting("bypass_header", "X-Streamcache-Bypass")
            has_bypass_header = False
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
                return HttpResponseRedirect(redirect_url, status=302)
            except Exception as e:
                logger.error(f"[Stasharr] Error generating Streamcache redirect: {e}. Falling back to default stream.", exc_info=True)
                return orig_stream_vod(request, content_type, content_id, *args, **kwargs)

        vod_views.stream_vod = patched_stream_vod
        logger.info("[Stasharr] stream_vod successfully wrapped for XC VOD interception.")

    def get_setting(self, key: str, default=None):
        """
        Retrieves a plugin setting, checking cached settings, database PluginConfig,
        and finally falling back to DEFAULT_SETTINGS.
        """
        if key in self._cached_settings:
            return self._cached_settings[key]

        # Query persisted database configuration if available in Django context
        try:
            from apps.plugins.models import PluginConfig
            # Match by plugin name or key
            config = PluginConfig.objects.filter(name__iexact="stasharr").first()
            if not config:
                config = PluginConfig.objects.filter(key__iexact="stasharr").first()
            if config and config.settings:
                val = config.settings.get(key)
                if val is not None:
                    self._cached_settings[key] = val
                    return val
        except Exception:
            pass

        return DEFAULT_SETTINGS.get(key, default)

    def is_enabled(self) -> bool:
        """
        Returns True if the plugin is active both in settings and in Dispatcharr's DB model.
        """
        # Master DB toggle check
        try:
            from apps.plugins.models import PluginConfig
            config = PluginConfig.objects.filter(name__iexact="stasharr").first()
            if not config:
                config = PluginConfig.objects.filter(key__iexact="stasharr").first()
            if config and not config.enabled:
                return False
        except Exception:
            pass

        # Setting field toggle check
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
        sc_base = str(self.get_setting("streamcache_base_url", "http://streamcache:8080")).strip().rstrip("/")
        dp_base = str(self.get_setting("dispatcharr_stream_url", "http://dispatcharr:9191")).strip().rstrip("/")
        api_key = str(self.get_setting("api_key", "")).strip()

        # Build canonical internal Dispatcharr target URL
        target_url = f"{dp_base}/proxy/vod/{content_type}/{content_uuid}"
        if api_key:
            target_url += f"?api_key={api_key}"

        # URL-encode the target for Streamcache's ?url= query parameter
        escaped_target = quote(target_url, safe="")
        return f"{sc_base}/stream?url={escaped_target}"

    def run(self, action: str, params: dict, context: dict) -> dict:
        """
        Handles synchronous administrative actions triggered from the Dispatcharr UI.
        """
        # Sync latest settings from context if provided
        if context and "settings" in context:
            self._cached_settings.update(context["settings"])

        if action == "status_check":
            is_hooked = False
            vod_views = sys.modules.get("apps.proxy.vod_proxy.views")
            if vod_views is None:
                try:
                    import apps.proxy.vod_proxy.views as vod_views
                except Exception:
                    pass
            if vod_views:
                is_hooked = hasattr(vod_views, "_stasharr_orig_stream_vod")

            sc_base = self.get_setting("streamcache_base_url")
            dp_base = self.get_setting("dispatcharr_stream_url")
            api_key_set = bool(self.get_setting("api_key"))

            return {
                "status": "success",
                "message": (
                    f"Stasharr is {'active and hooked' if is_hooked else 'standby'}. "
                    f"Enabled: {self.is_enabled()}. "
                    f"Streamcache Base: {sc_base}. "
                    f"Dispatcharr Base: {dp_base}. "
                    f"API Key Configured: {'Yes' if api_key_set else 'No'}."
                ),
                "is_hooked": is_hooked,
                "is_enabled": self.is_enabled(),
            }

        if action == "ping_streamcache":
            sc_base = str(self.get_setting("streamcache_base_url", "http://streamcache:8080")).strip().rstrip("/")
            test_url = f"{sc_base}/"
            try:
                req = urllib.request.Request(test_url, headers={"User-Agent": "Dispatcharr-Stasharr-Plugin"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    code = resp.getcode()
                    return {
                        "status": "success",
                        "message": f"Successfully reached Streamcache at {sc_base} (HTTP {code}).",
                    }
            except urllib.error.HTTPError as e:
                # Any HTTP response (even 404/403) confirms Streamcache server is online and listening
                return {
                    "status": "success",
                    "message": f"Streamcache server responded at {sc_base} (HTTP {e.code}). Connectivity confirmed.",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Failed to connect to Streamcache at {sc_base}: {str(e)}",
                }

        return {"status": "error", "message": f"Unknown action: {action}"}
