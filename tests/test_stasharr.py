import sys
import unittest
from unittest.mock import MagicMock, patch, ANY
import types
import hashlib
import json
import time
from urllib.parse import urlparse, unquote


class TestStasharrPlugin(unittest.TestCase):
    def setUp(self):
        # Create mock apps.proxy.vod_proxy.views module if not present
        if "apps.proxy.vod_proxy.views" not in sys.modules:
            mock_vod_views = types.ModuleType("apps.proxy.vod_proxy.views")
            self.orig_stream_vod_mock = MagicMock(return_value="ORIGINAL_STREAM_CALLED")
            mock_vod_views.stream_vod = self.orig_stream_vod_mock
            sys.modules["apps.proxy.vod_proxy.views"] = mock_vod_views
        else:
            mock_vod_views = sys.modules["apps.proxy.vod_proxy.views"]
            self.orig_stream_vod_mock = MagicMock(return_value="ORIGINAL_STREAM_CALLED")
            mock_vod_views.stream_vod = self.orig_stream_vod_mock
            if hasattr(mock_vod_views, "_stasharr_orig_stream_vod"):
                delattr(mock_vod_views, "_stasharr_orig_stream_vod")

        # Create mock apps.proxy.utils module if not present
        if "apps.proxy.utils" not in sys.modules:
            mock_proxy_utils = types.ModuleType("apps.proxy.utils")
            self.orig_get_user_active_conns_mock = MagicMock(return_value=[])
            mock_proxy_utils.get_user_active_connections = self.orig_get_user_active_conns_mock
            sys.modules["apps.proxy.utils"] = mock_proxy_utils
        else:
            mock_proxy_utils = sys.modules["apps.proxy.utils"]
            self.orig_get_user_active_conns_mock = MagicMock(return_value=[])
            mock_proxy_utils.get_user_active_connections = self.orig_get_user_active_conns_mock
            if hasattr(mock_proxy_utils, "_stasharr_orig_get_user_active_connections"):
                delattr(mock_proxy_utils, "_stasharr_orig_get_user_active_connections")

        # Clean up any mocked apps.plugins.models between tests
        sys.modules.pop("apps.plugins.models", None)

        # Import plugin
        import plugin
        self.plugin_mod = plugin
        self.plugin = plugin.Plugin()

    def tearDown(self):
        # Restore original stream_vod in views module
        if "apps.proxy.vod_proxy.views" in sys.modules:
            mock_vod_views = sys.modules["apps.proxy.vod_proxy.views"]
            if hasattr(mock_vod_views, "_stasharr_orig_stream_vod"):
                mock_vod_views.stream_vod = mock_vod_views._stasharr_orig_stream_vod
                delattr(mock_vod_views, "_stasharr_orig_stream_vod")
        # Restore original get_user_active_connections in utils module
        if "apps.proxy.utils" in sys.modules:
            mock_proxy_utils = sys.modules["apps.proxy.utils"]
            if hasattr(mock_proxy_utils, "_stasharr_orig_get_user_active_connections"):
                mock_proxy_utils.get_user_active_connections = mock_proxy_utils._stasharr_orig_get_user_active_connections
                delattr(mock_proxy_utils, "_stasharr_orig_get_user_active_connections")
        sys.modules.pop("apps.plugins.models", None)

    def test_hook_injection(self):
        """Verify stream_vod and get_user_active_connections are wrapped and originals preserved."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]
        proxy_utils = sys.modules["apps.proxy.utils"]
        self.assertTrue(hasattr(vod_views, "_stasharr_orig_stream_vod"))
        self.assertNotEqual(vod_views.stream_vod, self.orig_stream_vod_mock)
        self.assertTrue(hasattr(proxy_utils, "_stasharr_orig_get_user_active_connections"))
        self.assertNotEqual(proxy_utils.get_user_active_connections, self.orig_get_user_active_conns_mock)

    def test_delivery_mode_default_is_proxy(self):
        """Verify delivery_mode default setting is 'proxy' and timeout is 120."""
        self.assertEqual(self.plugin_mod.DEFAULT_SETTINGS["delivery_mode"], "proxy")
        self.assertEqual(self.plugin_mod.DEFAULT_SETTINGS["streamcache_session_timeout"], 120)
        self.assertEqual(self.plugin.get_setting("delivery_mode"), "proxy")
        self.assertEqual(self.plugin.get_setting("streamcache_session_timeout"), 120)

    def test_xc_movie_redirection(self):
        """Verify XC movie requests are intercepted and redirected to Streamcache in redirect mode."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {}
        req.GET = {}
        req.user = None

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "https://streamcache.domain.ca",
            "dispatcharr_stream_url": "https://dp.domain.ca",
        }

        resp = vod_views.stream_vod(req, "movie", "movie-uuid-999")

        # Must not call the original upstream stream
        self.orig_stream_vod_mock.assert_not_called()

        # Must return an HTTP 302 redirect
        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]

        # Expected target URL embedded in Streamcache ?url= query parameter
        self.assertTrue(location.startswith("https://streamcache.domain.ca/stream?url="))
        extracted_url = unquote(location.split("url=")[1])
        self.assertEqual(extracted_url, "https://dp.domain.ca/proxy/vod/movie/movie-uuid-999")

    def test_xc_series_episode_redirection(self):
        """Verify XC episode requests are intercepted and redirected to Streamcache in redirect mode."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/series/testuser/testpass/555.mkv"
        req.headers = {}
        req.GET = {}
        req.user = None

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "https://streamcache.domain.ca",
            "dispatcharr_stream_url": "https://dp.domain.ca",
        }

        resp = vod_views.stream_vod(req, "episode", "ep-uuid-456")

        self.orig_stream_vod_mock.assert_not_called()
        self.assertEqual(resp.status_code, 302)

        location = resp["Location"]
        extracted_url = unquote(location.split("url=")[1])
        self.assertEqual(extracted_url, "https://dp.domain.ca/proxy/vod/episode/ep-uuid-456")

    def test_proxy_mode_streaming_response(self):
        """Verify proxy mode in-line streaming with Range headers, status 206, and X-Accel-Buffering: no."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {
            "Range": "bytes=100-200",
            "If-Range": "etag-999",
            "User-Agent": "VLC/3.0.18",
        }
        req.META = {}
        req.user = None

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "proxy",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        mock_upstream = MagicMock()
        mock_upstream.status_code = 206
        mock_upstream.headers = {
            "Content-Type": "video/mp4",
            "Content-Range": "bytes 100-200/5000",
            "Content-Length": "101",
            "Accept-Ranges": "bytes",
        }
        mock_upstream.iter_content.return_value = [b"chunk1", b"chunk2"]

        with patch("requests.get", return_value=mock_upstream) as mock_get:
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-999")

            self.orig_stream_vod_mock.assert_not_called()
            mock_get.assert_called_once()
            call_url = mock_get.call_args[0][0]
            call_kwargs = mock_get.call_args[1]

            # Upstream URL check
            self.assertTrue(call_url.startswith("http://streamcache:8080/stream?url="))
            extracted_url = unquote(call_url.split("url=")[1])
            self.assertEqual(extracted_url, "http://dispatcharr:9191/proxy/vod/movie/movie-uuid-999")

            # Forwarded headers check
            self.assertEqual(call_kwargs["headers"].get("Range"), "bytes=100-200")
            self.assertEqual(call_kwargs["headers"].get("If-Range"), "etag-999")
            self.assertEqual(call_kwargs["headers"].get("User-Agent"), "VLC/3.0.18")
            self.assertTrue(call_kwargs.get("stream"))
            self.assertEqual(call_kwargs.get("timeout"), 10)

            # Response verification
            self.assertEqual(resp.status_code, 206)
            self.assertEqual(resp["X-Accel-Buffering"], "no")
            self.assertEqual(resp["Content-Type"], "video/mp4")
            self.assertEqual(resp["Content-Range"], "bytes 100-200/5000")
            self.assertEqual(resp["Content-Length"], "101")
            self.assertEqual(resp["Accept-Ranges"], "bytes")
            self.assertEqual(resp["Cache-Control"], "no-cache")
            self.assertEqual(resp["Connection"], "keep-alive")

            # Verify chunk streaming and close
            chunks = list(resp.streaming_content)
            self.assertEqual(chunks, [b"chunk1", b"chunk2"])
            mock_upstream.close.assert_called_once()

    def test_proxy_mode_redis_connection_lifecycle(self):
        """Verify proxy mode registers VOD connection in Redis and cleans it up upon completion."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/admin/testpass/101.mp4"
        req.headers = {}
        req.GET = {}
        req.META = {"REMOTE_ADDR": "192.168.1.100"}
        user_mock = MagicMock()
        user_mock.id = 42
        user_mock.username = "admin"
        user_mock.stream_limit = 0
        req.user = user_mock

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "proxy",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        mock_upstream = MagicMock()
        mock_upstream.status_code = 200
        mock_upstream.headers = {"Content-Type": "video/mp4"}
        mock_upstream.iter_content.return_value = [b"chunkA", b"chunkB"]

        mock_redis = MagicMock()
        # Create core.utils module if missing for test
        if "core.utils" not in sys.modules:
            mock_core_utils = types.ModuleType("core.utils")
            sys.modules["core.utils"] = mock_core_utils
        else:
            mock_core_utils = sys.modules["core.utils"]

        mock_redis_client_cls = MagicMock()
        mock_redis_client_cls.get_client.return_value = mock_redis
        mock_core_utils.RedisClient = mock_redis_client_cls

        with patch("requests.get", return_value=mock_upstream):
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-999", session_id="test_sess_1", user=user_mock)

            self.assertEqual(resp.status_code, 200)

            # Verify Redis registration
            mock_redis.hset.assert_called_once()
            call_key = mock_redis.hset.call_args[0][0]
            call_mapping = mock_redis.hset.call_args[1]["mapping"]
            self.assertEqual(call_key, "vod_persistent_connection:test_sess_1")
            self.assertEqual(call_mapping["user_id"], "42")
            self.assertEqual(call_mapping["username"], "admin")
            self.assertEqual(call_mapping["content_uuid"], "movie-uuid-999")
            mock_redis.expire.assert_called_with("vod_persistent_connection:test_sess_1", 60)

            # Consume stream to trigger finally block
            chunks = list(resp.streaming_content)
            self.assertEqual(chunks, [b"chunkA", b"chunkB"])

            # Verify Redis cleanup
            mock_redis.delete.assert_any_call("vod_persistent_connection:test_sess_1")
            mock_redis.delete.assert_any_call("vod_client_stop_key:test_sess_1")

    def test_proxy_mode_stream_limit_rejection_429(self):
        """Verify proxy mode rejects with 429 when check_user_stream_limits rejects."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]
        proxy_utils = sys.modules["apps.proxy.utils"]

        req = MagicMock()
        req.path = "/movie/alice/testpass/101.mp4"
        req.headers = {}
        req.META = {}
        user_mock = MagicMock()
        user_mock.id = 99
        user_mock.username = "alice"
        user_mock.stream_limit = 1
        req.user = user_mock

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "proxy",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        # Mock check_user_stream_limits returning False (limit exceeded)
        with patch.object(proxy_utils, "check_user_stream_limits", create=True, return_value=False):
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-999", user=user_mock)
            self.assertEqual(resp.status_code, 429)
            self.assertIn("Stream limit exceeded", resp.content)

    def test_proxy_mode_is_default_without_explicit_setting(self):
        """Verify that omitting delivery_mode uses proxy mode by default."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {}
        req.META = {}
        req.user = None

        self.plugin._cached_settings = {
            "enabled": True,
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
            # delivery_mode omitted to test default fallback
        }

        mock_upstream = MagicMock()
        mock_upstream.status_code = 200
        mock_upstream.headers = {"Content-Type": "video/mp4"}
        mock_upstream.iter_content.return_value = [b"data"]

        with patch("requests.get", return_value=mock_upstream) as mock_get:
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-999")

            mock_get.assert_called_once()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["X-Accel-Buffering"], "no")

    def test_redirect_mode_with_user_parameter(self):
        """Verify redirect mode appends &user=alice when user object is provided."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/alice/pass123/101.mp4"
        req.headers = {}
        req.GET = {}

        user_mock = MagicMock()
        user_mock.id = 10
        user_mock.username = "alice"
        user_mock.stream_limit = 0  # unlimited
        req.user = user_mock

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        resp = vod_views.stream_vod(req, "movie", "movie-uuid-123", user=user_mock)

        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]
        self.assertTrue(location.endswith("&user=alice"))

    def test_redirect_mode_stream_limit_rejection_429(self):
        """Verify redirect mode returns 429 when user stream limit is reached or exceeded."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]
        proxy_utils = sys.modules["apps.proxy.utils"]

        req = MagicMock()
        req.path = "/movie/alice/pass123/101.mp4"
        req.headers = {}
        req.GET = {}

        user_mock = MagicMock()
        user_mock.id = 10
        user_mock.username = "alice"
        user_mock.stream_limit = 1  # max 1 concurrent stream
        req.user = user_mock

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        # Mock proxy_utils.get_user_active_connections to return 1 active session
        self.orig_get_user_active_conns_mock.return_value = [
            {"media_id": "live-channel-1", "client_id": "c1", "connected_at": time.time(), "type": "live"}
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        with patch("requests.get", return_value=mock_resp):
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-123", user=user_mock)

        self.assertEqual(resp.status_code, 429)
        data = json.loads(resp.content) if hasattr(resp, "content") else resp.json()
        self.assertIn("Stream limit exceeded (1 concurrent streams allowed)", data["error"])

    def test_redirect_mode_within_stream_limit_allowed(self):
        """Verify redirect mode allows playback when user is below stream limit."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/alice/pass123/101.mp4"
        req.headers = {}
        req.GET = {}

        user_mock = MagicMock()
        user_mock.id = 10
        user_mock.username = "alice"
        user_mock.stream_limit = 2  # limit is 2
        req.user = user_mock

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        # 1 active session in Dispatcharr
        self.orig_get_user_active_conns_mock.return_value = [
            {"media_id": "live-channel-1", "client_id": "c1", "connected_at": time.time(), "type": "live"}
        ]

        # 0 sessions in Streamcache
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        with patch("requests.get", return_value=mock_resp):
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-123", user=user_mock)

            self.assertEqual(resp.status_code, 302)
            self.assertTrue(resp["Location"].endswith("&user=alice"))

    def test_get_user_active_connections_merges_streamcache_sessions(self):
        """
        Verify get_user_active_connections merges active sessions from Streamcache
        when delivery_mode is 'redirect', with 2-second caching.
        """
        proxy_utils = sys.modules["apps.proxy.utils"]

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        # Populate user cache for user_id 5 -> "charlie"
        self.plugin._user_cache[5] = "charlie"

        # Dispatcharr native active streams
        self.orig_get_user_active_conns_mock.return_value = [
            {"media_id": "chan-99", "client_id": "client-99", "connected_at": 1000.0, "type": "live"}
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "count": 1,
            "user": "charlie",
            "sessions": [{"id": "sc-session-42", "key": "movie-cache-key-101"}],
        }

        with patch("requests.get", return_value=mock_resp) as mock_get:
            conns = proxy_utils.get_user_active_connections(user_id=5)

            # Check HTTP call was made
            mock_get.assert_called_once_with(
                "http://streamcache:8080/api/sessions?user=charlie",
                timeout=2,
            )

            # Check merged connections
            self.assertEqual(len(conns), 2)
            self.assertEqual(conns[0]["type"], "live")
            self.assertEqual(conns[0]["media_id"], "chan-99")
            self.assertEqual(conns[1]["type"], "vod")
            self.assertEqual(conns[1]["media_id"], "movie-cache-key-101")
            self.assertEqual(conns[1]["client_id"], "sc-session-42")

            # Test 2-second in-memory cache: second call does not query HTTP again
            mock_get.reset_mock()
            conns_cached = proxy_utils.get_user_active_connections(user_id=5)
            mock_get.assert_not_called()
            self.assertEqual(len(conns_cached), 2)

    def test_get_user_active_connections_in_proxy_mode_does_not_query_streamcache(self):
        """Verify get_user_active_connections does NOT query Streamcache in proxy mode."""
        proxy_utils = sys.modules["apps.proxy.utils"]

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "proxy",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        self.plugin._user_cache[5] = "charlie"
        self.orig_get_user_active_conns_mock.return_value = [
            {"media_id": "chan-99", "client_id": "client-99", "connected_at": 1000.0, "type": "live"}
        ]

        with patch("requests.get") as mock_get:
            conns = proxy_utils.get_user_active_connections(user_id=5)
            mock_get.assert_not_called()
            self.assertEqual(len(conns), 1)
            self.assertEqual(conns[0]["media_id"], "chan-99")

    def test_streamcache_md5_cache_key_compatibility(self):
        """
        Verify that Streamcache's MD5 algorithm yields the exact same cache key
        for URLs generated by Stasharr (without ?api_key=) as it does for dp2strm (with ?api_key=).
        """
        self.plugin._cached_settings = {
            "streamcache_base_url": "https://streamcache.domain.ca",
            "dispatcharr_stream_url": "https://dp.domain.ca",
        }

        redirect_url = self.plugin.build_streamcache_redirect_url("movie", "uuid-777")
        extracted_target = unquote(redirect_url.split("url=")[1])

        # Simulate Streamcache's getCacheKeyAndExt logic:
        # cleanURL := parsed.Scheme + "://" + parsed.Host + parsed.Path
        # key := md5(cleanURL)
        parsed_stasharr = urlparse(extracted_target)
        clean_stasharr_url = f"{parsed_stasharr.scheme}://{parsed_stasharr.netloc}{parsed_stasharr.path}"
        stasharr_md5 = hashlib.md5(clean_stasharr_url.encode("utf-8")).hexdigest()

        # Simulate dp2strm target URL (which includes ?api_key=...)
        dp2strm_target = "https://dp.domain.ca/proxy/vod/movie/uuid-777?api_key=some-secret-key"
        parsed_dp2strm = urlparse(dp2strm_target)
        clean_dp2strm_url = f"{parsed_dp2strm.scheme}://{parsed_dp2strm.netloc}{parsed_dp2strm.path}"
        dp2strm_md5 = hashlib.md5(clean_dp2strm_url.encode("utf-8")).hexdigest()

        self.assertEqual(stasharr_md5, dp2strm_md5)

    def test_internal_playback_route_not_redirected(self):
        """
        Verify internal routes (/proxy/vod/...) are NEVER redirected.
        This ensures Streamcache can pull from Dispatcharr on a cache miss without looping.
        """
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/proxy/vod/movie/movie-uuid-999"
        req.headers = {}
        req.GET = {}

        res = vod_views.stream_vod(req, "movie", "movie-uuid-999")

        # Must call original stream_vod directly
        self.orig_stream_vod_mock.assert_called_once()
        self.assertEqual(res, "ORIGINAL_STREAM_CALLED")

    def test_disabled_plugin_falls_back_to_original(self):
        """Verify toggling enabled to False immediately disables redirection."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {}
        req.GET = {}

        self.plugin._cached_settings["enabled"] = False

        res = vod_views.stream_vod(req, "movie", "movie-uuid-999")

        self.orig_stream_vod_mock.assert_called_once()
        self.assertEqual(res, "ORIGINAL_STREAM_CALLED")

    def test_live_db_settings_update_not_stale_cached(self):
        """
        Verify that updating PluginConfig.settings in Dispatcharr's database
        is immediately reflected on the very next request without requiring a restart.
        """
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://old-cache:8080",
            "dispatcharr_stream_url": "http://old-dp:9191",
        }

        mock_models = types.ModuleType("apps.plugins.models")
        mock_plugin_config_cls = MagicMock()
        mock_plugin_config_cls.objects.filter.return_value.first.return_value = mock_config
        mock_models.PluginConfig = mock_plugin_config_cls
        sys.modules["apps.plugins.models"] = mock_models

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {}
        req.GET = {}
        req.user = None

        resp1 = vod_views.stream_vod(req, "movie", "movie-uuid-999")
        self.assertIn("http://old-cache:8080/stream?url=", resp1["Location"])

        # Simulate user saving new settings in the Dispatcharr Web UI
        mock_config.settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://new-cache:8080",
            "dispatcharr_stream_url": "http://new-dp:9191",
        }

        resp2 = vod_views.stream_vod(req, "movie", "movie-uuid-999")
        self.assertIn("http://new-cache:8080/stream?url=", resp2["Location"])
        extracted_url = unquote(resp2["Location"].split("url=")[1])
        self.assertEqual(extracted_url, "http://new-dp:9191/proxy/vod/movie/movie-uuid-999")

    def test_plugin_stop_restores_original_functions(self):
        """Verify calling stop() unhooks stream_vod and get_user_active_connections, and clears ACTIVE_PLUGIN."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]
        proxy_utils = sys.modules["apps.proxy.utils"]
        self.assertNotEqual(vod_views.stream_vod, self.orig_stream_vod_mock)
        self.assertNotEqual(proxy_utils.get_user_active_connections, self.orig_get_user_active_conns_mock)

        self.plugin.stop(context={"reason": "disable"})

        self.assertEqual(vod_views.stream_vod, self.orig_stream_vod_mock)
        self.assertEqual(proxy_utils.get_user_active_connections, self.orig_get_user_active_conns_mock)
        self.assertIsNone(self.plugin_mod.ACTIVE_PLUGIN)

    def test_proxy_mode_upstream_error_fallback(self):
        """Verify proxy mode falls back to orig_stream_vod if Streamcache connection fails."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {}
        req.META = {}
        req.user = None

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "proxy",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        with patch("requests.get", side_effect=Exception("Streamcache connection failed")):
            res = vod_views.stream_vod(req, "movie", "movie-uuid-999")
            self.orig_stream_vod_mock.assert_called_once()
            self.assertEqual(res, "ORIGINAL_STREAM_CALLED")

    def test_stream_vod_resolves_user_from_positional_args(self):
        """Verify user object is properly extracted from positional arguments."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/testuser/testpass/101.mp4"
        req.headers = {}
        req.META = {}
        req.user = None

        user_mock = MagicMock()
        user_mock.id = 99
        user_mock.username = "david"
        user_mock.stream_limit = 0

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        # stream_vod signature: (request, content_type, content_id, session_id, profile_id, user)
        resp = vod_views.stream_vod(req, "movie", "movie-uuid-123", "session-xyz", 1, user_mock)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].endswith("&user=david"))
        self.assertEqual(self.plugin._user_cache.get(99), "david")

    def test_redirect_mode_limit_exceeded_via_streamcache_sessions(self):
        """Verify redirect mode detects stream limit exceeded when active session is in Streamcache."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/movie/david/pass/101.mp4"
        req.headers = {}
        req.META = {}

        user_mock = MagicMock()
        user_mock.id = 77
        user_mock.username = "david"
        user_mock.stream_limit = 1
        req.user = user_mock

        self.plugin._cached_settings = {
            "enabled": True,
            "delivery_mode": "redirect",
            "streamcache_base_url": "http://streamcache:8080",
            "dispatcharr_stream_url": "http://dispatcharr:9191",
        }

        # Dispatcharr has 0 active connections
        self.orig_get_user_active_conns_mock.return_value = []

        # Streamcache has 1 active session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "count": 1,
            "user": "david",
            "sessions": [{"id": "sc-sess-1", "key": "some-media-key"}],
        }

        with patch("requests.get", return_value=mock_resp):
            resp = vod_views.stream_vod(req, "movie", "movie-uuid-123", user=user_mock)

        self.assertEqual(resp.status_code, 429)
        data = json.loads(resp.content) if hasattr(resp, "content") else resp.json()
        self.assertIn("Stream limit exceeded (1 concurrent streams allowed)", data["error"])

    def test_status_check_action(self):
        """Verify status_check action returns diagnostic info."""
        result = self.plugin.run("status_check", {}, {})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_hooked"])
        self.assertTrue(result["is_enabled"])
        self.assertTrue(result["connections_hooked"])
        self.assertEqual(result["delivery_mode"], "proxy")
        self.assertIn("active and hooked", result["message"])


if __name__ == "__main__":
    unittest.main()
