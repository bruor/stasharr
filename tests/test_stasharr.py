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
        """Verify delivery_mode default setting is 'proxy'."""
        self.assertEqual(self.plugin_mod.DEFAULT_SETTINGS["delivery_mode"], "proxy")
        self.assertEqual(self.plugin.get_setting("delivery_mode"), "proxy")

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

    def test_xc_series_episode_head_request_skips_prehydration(self):
        """Verify XC episode HEAD requests do not query or append next_urls for prehydration."""
        vod_views = sys.modules["apps.proxy.vod_proxy.views"]

        req = MagicMock()
        req.path = "/series/testuser/testpass/555.mkv"
        req.method = "HEAD"
        req.headers = {}
        req.META = {}
        req.user = None

        self.plugin._cached_settings = {
            "delivery_mode": "redirect",
            "streamcache_base_url": "https://streamcache.domain.ca",
            "dispatcharr_stream_url": "https://dp.domain.ca",
            "prehydrate_count": 2,
        }

        with patch.object(self.plugin, "get_next_episode_urls", return_value=["http://dp/next1", "http://dp/next2"]) as mock_next:
            resp = vod_views.stream_vod(req, "episode", "ep-uuid-456")
            mock_next.assert_not_called()

        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]
        self.assertNotIn("next_url", location)
        self.assertIn("url=", location)

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

    def test_build_streamcache_redirect_url_with_next_urls(self):
        """Verify build_streamcache_redirect_url correctly appends &next_url= parameters."""
        url = self.plugin.build_streamcache_redirect_url(
            "episode",
            "ep-uuid-1",
            username="alice",
            next_urls=[
                "http://dispatcharr:9191/proxy/vod/episode/ep-uuid-2",
                "http://dispatcharr:9191/proxy/vod/episode/ep-uuid-3",
            ],
        )
        self.assertIn("url=", url)
        self.assertIn("user=alice", url)
        self.assertIn("&next_url=http%3A%2F%2Fdispatcharr%3A9191%2Fproxy%2Fvod%2Fepisode%2Fep-uuid-2", url)
        self.assertIn("&next_url=http%3A%2F%2Fdispatcharr%3A9191%2Fproxy%2Fvod%2Fepisode%2Fep-uuid-3", url)

    def test_get_provider_capacity_metrics(self):
        """Verify provider capacity calculation aggregates profiles and Redis connection counts."""
        # Mock M3U models and RedisClient
        mock_m3u_mod = types.ModuleType("apps.m3u.models")
        mock_account_cls = MagicMock()
        mock_m3u_mod.M3UAccount = mock_account_cls
        mock_m3u_mod.M3UAccountProfile = MagicMock()

        acc1 = MagicMock()
        acc1.id = 1
        acc1.name = "Provider A"
        acc1.locked = False
        acc1.is_active = True

        prof1 = MagicMock()
        prof1.id = 10
        prof1.name = "Profile 1"
        prof1.max_streams = 2
        prof1.is_active = True

        acc1.profiles.filter.return_value = [prof1]

        acc2 = MagicMock()
        acc2.id = 2
        acc2.name = "Provider B (Unlimited)"
        acc2.locked = False
        acc2.is_active = True

        prof2 = MagicMock()
        prof2.id = 20
        prof2.name = "Unlimited Profile"
        prof2.max_streams = 0  # 0 = unlimited
        prof2.is_active = True

        acc2.profiles.filter.return_value = [prof2]

        acc_custom = MagicMock()
        acc_custom.id = 99
        acc_custom.name = "custom"
        acc_custom.locked = True
        mock_account_cls.objects.filter.return_value.prefetch_related.return_value = [acc1, acc2, acc_custom]

        mock_redis_client = MagicMock()
        # Profile 1 has 1 active stream in Redis
        def fake_redis_get(key):
            if "10" in str(key):
                return b"1"
            return None
        mock_redis_client.get.side_effect = fake_redis_get
        def fake_scan_iter(match=None, count=None):
            if "live:channel:" in str(match):
                return iter(["live:channel:1:clients:c1", "live:channel:1:clients:c2"])
            if "vod_persistent_connection:" in str(match):
                return iter(["vod_persistent_connection:v1", "vod_persistent_connection:v2", "vod_persistent_connection:v3"])
            return iter([])
        mock_redis_client.scan_iter.side_effect = fake_scan_iter

        # v1 and v2 are proxied client sessions (no m3u_profile_id); v3 is a native provider stream
        def fake_redis_hget(key, field):
            if "v3" in str(key) and field == "m3u_profile_id":
                return b"10"
            return None
        mock_redis_client.hget.side_effect = fake_redis_hget

        mock_core_utils = types.ModuleType("core.utils")
        mock_redis_cls = MagicMock()
        mock_redis_cls.get_client.return_value = mock_redis_client
        mock_core_utils.RedisClient = mock_redis_cls

        with patch.dict(sys.modules, {
            "apps.m3u.models": mock_m3u_mod,
            "core.utils": mock_core_utils,
        }):
            data = self.plugin_mod.get_provider_capacity()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(len(data["providers"]), 2)

            # Provider A: max=2, active=1, available=1
            pA = next(p for p in data["providers"] if p["account_id"] == 1)
            self.assertEqual(pA["max_streams"], 2)
            self.assertEqual(pA["active_streams"], 1)
            self.assertEqual(pA["available_streams"], 1)

            # Provider B: unlimited (max_streams=0) -> avail=999
            pB = next(p for p in data["providers"] if p["account_id"] == 2)
            self.assertEqual(pB["available_streams"], 999)

            # Total available includes unlimited -> 999
            self.assertGreaterEqual(data["total_available_streams"], 999)
            self.assertEqual(data["live_clients"], 2)
            self.assertEqual(data["live_streams"], 1)
            self.assertEqual(data["vod_streams"], 1)
            self.assertEqual(data["vod_clients"], 3)

            # Also verify via plugin.run action "provider_capacity"
            action_res = self.plugin.run("provider_capacity", {}, {})
            self.assertEqual(action_res["status"], "ok")
            self.assertEqual(action_res["providers"][0]["account_id"], 1)

    def test_get_provider_capacity_proxied_vod_zero_upstream(self):
        """Verify proxied XC client sessions without m3u_profile_id do not increment vod_streams."""
        mock_m3u_mod = types.ModuleType("apps.m3u.models")
        mock_account_cls = MagicMock()
        mock_m3u_mod.M3UAccount = mock_account_cls
        mock_m3u_mod.M3UAccountProfile = MagicMock()

        acc1 = MagicMock(id=1, name="Provider A", locked=False)
        acc1.profiles.filter.return_value = [MagicMock(id=10, name="P1", max_streams=2, is_active=True)]
        mock_account_cls.objects.filter.return_value.prefetch_related.return_value = [acc1]

        mock_redis_client = MagicMock()
        mock_redis_client.get.return_value = None
        # All 2 sessions are purely proxied downstream clients (no m3u_profile_id)
        mock_redis_client.scan_iter.side_effect = lambda match=None, count=None: iter(
            ["vod_persistent_connection:sess1", "vod_persistent_connection:sess2"]
            if "vod_persistent_connection:" in str(match) else []
        )
        mock_redis_client.hget.return_value = None

        mock_core_utils = types.ModuleType("core.utils")
        mock_redis_cls = MagicMock()
        mock_redis_cls.get_client.return_value = mock_redis_client
        mock_core_utils.RedisClient = mock_redis_cls

        with patch.dict(sys.modules, {
            "apps.m3u.models": mock_m3u_mod,
            "core.utils": mock_core_utils,
        }):
            data = self.plugin_mod.get_provider_capacity()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["vod_streams"], 0)
            self.assertEqual(data["vod_clients"], 2)
            self.assertEqual(data["total_active_streams"], 0)

    def test_get_provider_capacity_scoped_to_content(self):
        """Verify target_available_streams scopes to the provider backing the requested content."""
        mock_m3u_mod = types.ModuleType("apps.m3u.models")
        mock_account_cls = MagicMock()
        mock_m3u_mod.M3UAccount = mock_account_cls
        mock_m3u_mod.M3UAccountProfile = MagicMock()

        acc1 = MagicMock()
        acc1.id = 1
        acc1.name = "Provider A"
        acc1.locked = False
        acc1.profiles.filter.return_value = [MagicMock(id=10, name="P1", max_streams=1, is_active=True)]

        acc2 = MagicMock()
        acc2.id = 2
        acc2.name = "Provider B"
        acc2.locked = False
        acc2.profiles.filter.return_value = [MagicMock(id=20, name="P2", max_streams=5, is_active=True)]

        acc_custom = MagicMock()
        acc_custom.id = 99
        acc_custom.name = "custom"
        acc_custom.locked = True
        mock_account_cls.objects.filter.return_value.prefetch_related.return_value = [acc1, acc2, acc_custom]

        mock_redis_client = MagicMock()
        # Provider A has 1 active stream (so available = 0)
        # Provider B has 0 active streams (so available = 5)
        def fake_redis_get(key):
            if "10" in str(key):
                return b"1"
            return b"0"
        mock_redis_client.get.side_effect = fake_redis_get

        mock_core_utils = types.ModuleType("core.utils")
        mock_redis_cls = MagicMock()
        mock_redis_cls.get_client.return_value = mock_redis_client
        mock_core_utils.RedisClient = mock_redis_cls

        # Mock _get_content_and_relation to indicate episode is ONLY served by Provider A (account_id=1)
        mock_cand = MagicMock()
        mock_cand.m3u_account_id = 1
        fake_vod_views = types.ModuleType("apps.proxy.vod_proxy.views")
        fake_vod_views._get_content_and_relation = MagicMock(return_value=(MagicMock(), MagicMock(), [mock_cand]))

        with patch.dict(sys.modules, {
            "apps.m3u.models": mock_m3u_mod,
            "core.utils": mock_core_utils,
            "apps.proxy.vod_proxy.views": fake_vod_views,
        }):
            data = self.plugin_mod.get_provider_capacity(content_type="episode", content_id="ep-uuid-1")
            self.assertEqual(data["status"], "ok")
            # Global available is Provider A (0) + Provider B (5) = 5
            self.assertEqual(data["total_available_streams"], 5)
            # Target available is strictly Provider A (0)
            self.assertEqual(data["target_available_streams"], 0)

    def test_get_provider_capacity_multi_profile_formatting(self):
        """Verify multi-profile providers format profile breakdowns and exclude locked custom accounts."""
        mock_m3u_mod = types.ModuleType("apps.m3u.models")
        mock_account_cls = MagicMock()
        mock_m3u_mod.M3UAccount = mock_account_cls
        mock_m3u_mod.M3UAccountProfile = MagicMock()

        acc_inf = MagicMock()
        acc_inf.id = 1
        acc_inf.name = "Infinity"
        acc_inf.locked = False
        prof1 = MagicMock(id=10, max_streams=5, is_active=True)
        prof1.name = "Profile 1"
        prof2 = MagicMock(id=11, max_streams=5, is_active=True)
        prof2.name = "Profile 2"
        acc_inf.profiles.filter.return_value = [prof1, prof2]

        acc_custom = MagicMock()
        acc_custom.id = 99
        acc_custom.name = "custom"
        acc_custom.locked = True
        prof_custom = MagicMock(id=999, max_streams=0, is_active=True)
        prof_custom.name = "custom"
        acc_custom.profiles.filter.return_value = [prof_custom]

        mock_account_cls.objects.filter.return_value.prefetch_related.return_value = [acc_inf, acc_custom]

        mock_redis_client = MagicMock()
        # Profile 1 has 1 active stream, Profile 2 has 1 active stream (2 active total, 8 available total)
        def fake_redis_get(key):
            if "10" in str(key):
                return b"1"
            if "11" in str(key):
                return b"1"
            return None
        mock_redis_client.get.side_effect = fake_redis_get

        mock_core_utils = types.ModuleType("core.utils")
        mock_redis_cls = MagicMock()
        mock_redis_cls.get_client.return_value = mock_redis_client
        mock_core_utils.RedisClient = mock_redis_cls

        with patch.dict(sys.modules, {
            "apps.m3u.models": mock_m3u_mod,
            "core.utils": mock_core_utils,
        }):
            data = self.plugin_mod.get_provider_capacity()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(len(data["providers"]), 1)
            self.assertEqual(data["total_max_streams"], 10)
            self.assertEqual(data["total_active_streams"], 2)
            self.assertEqual(data["total_available_streams"], 8)
            # Verify human-readable message contains accurate capacity and profile breakdown
            self.assertIn("Provider Capacity: 8/10 streams available (2 active).", data["message"])
            self.assertIn("Infinity: 8/10 free [Profile 1: 4/5, Profile 2: 4/5]", data["message"])
            self.assertNotIn("custom", data["message"])

    def test_get_next_episode_urls(self):
        """Verify get_next_episode_urls queries upcoming episodes in sequence order."""
        mock_vod_models = types.ModuleType("apps.vod.models")
        mock_ep_cls = MagicMock()
        mock_vod_models.Episode = mock_ep_cls
        mock_vod_models.M3UEpisodeRelation = MagicMock()

        cur_ep = MagicMock()
        cur_ep.id = 100
        cur_ep.uuid = "cur-ep-uuid"
        cur_ep.series_id = 42
        cur_ep.season_number = 1
        cur_ep.episode_number = 1

        next_ep1 = MagicMock(id=101, uuid="next-uuid-1", season_number=1, episode_number=2)
        next_ep2 = MagicMock(id=102, uuid="next-uuid-2", season_number=1, episode_number=3)

        filter_mock = MagicMock()
        filter_mock.first.return_value = cur_ep
        filter_mock.exclude.return_value.filter.return_value.order_by.return_value.__getitem__.return_value = [
            next_ep1,
            next_ep2,
        ]
        mock_ep_cls.objects.filter.return_value = filter_mock

        with patch.dict(sys.modules, {"apps.vod.models": mock_vod_models}):
            urls = self.plugin.get_next_episode_urls("cur-ep-uuid", count=2)
            self.assertEqual(len(urls), 2)
            self.assertIn("next-uuid-1", urls[0])
            self.assertIn("next-uuid-2", urls[1])

    def test_route_injection_in_dispatcharr_urls(self):
        """Verify provider-capacity route is inserted into dispatcharr.urls and cleaned up on stop."""
        mock_urls_mod = types.ModuleType("dispatcharr.urls")
        mock_urls_mod.urlpatterns = []

        with patch.dict(sys.modules, {"dispatcharr.urls": mock_urls_mod}):
            self.plugin._inject_hooks()
            route_names = [getattr(p, "name", None) for p in mock_urls_mod.urlpatterns]
            self.assertIn("stasharr_provider_capacity", route_names)

            # Test stop cleans it up
            self.plugin.stop()
            route_names_after = [getattr(p, "name", None) for p in mock_urls_mod.urlpatterns]
            self.assertNotIn("stasharr_provider_capacity", route_names_after)


    def test_prehydrate_count_setting_and_legacy_fallback(self):
        """Verify prehydrate_count setting is used and falls back to prefetch_count if needed."""
        # 1. Default when neither set
        self.plugin._cached_settings = {}
        val_default = self.plugin.get_setting("prehydrate_count")
        self.assertEqual(int(val_default or 1), 1)

        # 2. Modern prehydrate_count setting
        self.plugin._cached_settings = {"prehydrate_count": 3}
        val_modern = self.plugin.get_setting("prehydrate_count")
        self.assertEqual(int(val_modern or 1), 3)

        # 3. Legacy prefetch_count fallback
        self.plugin._cached_settings = {"prefetch_count": 2}
        val_legacy = self.plugin.get_setting("prehydrate_count")
        self.assertEqual(int(val_legacy or 1), 2)


if __name__ == "__main__":
    unittest.main()
