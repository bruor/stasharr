# Stasharr

**Stasharr** is a high-performance [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that transparently intercepts incoming Xtream Codes (XC) Video-on-Demand (VOD) playback requests from client players (such as Tivimate, IPTV Smarters, XCIPTV, Emby, etc.) and redirects them via **HTTP 302 Found** to [GoStreamCache (Streamcache)](https://github.com/bruor/GoStreamCache).

By redirecting clients directly to Streamcache with Dispatcharr's canonical `/proxy/vod/...` playback URL, Stasharr provides:
* **Zero Upstream Overhead on Cache Hits**: If a movie or episode is already in the cache, Dispatcharr and your upstream IPTV provider are **100% idle**.
* **100% Shared Cache with Emby (`dp2strm`)**: Content cached by Emby via [dp2strm](https://github.com/bruor/dp2strm) produces the exact same MD5 cache key in Streamcache as streams requested through Tivimate/XC players, eliminating redundant downloads.
* **Natural Loop Prevention**: End-user XC requests enter via `/movie/...` and `/series/...` (which Stasharr redirects), while Streamcache queries `/proxy/vod/...` on cache misses (which passes through to the provider without redirecting).

---

## Architecture & Stream Flow

```mermaid
sequenceDiagram
    autonumber
    actor Player as XC Player (Tivimate / Smarters)
    participant Dispatcharr as Dispatcharr (+ Stasharr Plugin)
    participant Streamcache as Streamcache (GoStreamCache)
    participant Provider as Upstream IPTV Provider

    Note over Player,Dispatcharr: Step 1: Interception & 302 Redirection
    Player->>Dispatcharr: GET /movie/{user}/{pass}/{stream_id}.mp4
    Note over Dispatcharr: Stasharr intercepts stream_vod -> issues 302 Redirect
    Dispatcharr-->>Player: HTTP 302 Found -> http://streamcache:8080/stream?url=http%3A%2F%2Fdispatcharr%3A9191%2Fproxy%2Fvod%2Fmovie%2F{uuid}

    Note over Player,Streamcache: Step 2: Streamcache Resolution
    Player->>Streamcache: GET /stream?url=...
    Note over Streamcache: Calculates MD5(http://dispatcharr:9191/proxy/vod/movie/{uuid})

    alt Cache Hit (Cached via Emby/dp2strm or prior playback)
        Streamcache-->>Player: 200 OK / 206 Partial Content (Direct from disk)
        Note over Streamcache,Provider: Zero provider connections consumed!
    else Cache Miss
        Streamcache->>Dispatcharr: GET /proxy/vod/movie/{uuid}
        Note over Dispatcharr: Path is /proxy/vod/ -> NOT redirected -> streams from provider
        Dispatcharr->>Provider: Request stream
        Provider-->>Dispatcharr: Stream chunks
        Dispatcharr-->>Streamcache: Stream chunks
        Streamcache-->>Player: Stream to player while saving to disk
    end
```

---

## Installation

### Option A: Direct Folder Copy (Docker Compose)
1. Copy the `stasharr` directory into your Dispatcharr container's plugins directory (default: `/app/data/plugins/stasharr/`):
   ```bash
   cp -r /path/to/stasharr /your/docker/volumes/dispatcharr_data/plugins/stasharr
   ```
2. In the Dispatcharr Web UI, navigate to **Plugins**.
3. Click the **Reload** (refresh) icon to scan the plugin folder.
4. Toggle **Stasharr** to **Enabled**.

### Option B: Zip Upload via Dispatcharr Web UI
1. Create a release zip:
   ```bash
   zip -r stasharr.zip plugin.json plugin.py README.md
   ```
2. In Dispatcharr Web UI, go to **Plugins** -> **Import Plugin**.
3. Upload `stasharr.zip` and enable the plugin.

---

## Configuration Settings

Stasharr automatically renders a configuration interface in the Dispatcharr web UI under **Plugins -> Stasharr**:

| Setting | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| **Enable Stasharr Interceptor** | `boolean` | `true` | Master toggle to enable or disable XC VOD redirection. Disabling immediately restores standard Dispatcharr playback without container restarts. |
| **Streamcache Base URL** | `string` | `http://streamcache:8080` | The address where Streamcache is reachable by client media players (e.g. `https://streamcache.domain.ca` or `http://192.168.1.50:8080`). |
| **Dispatcharr Base URL for Streamcache** | `string` | `http://dispatcharr:9191` | The address of Dispatcharr as reachable by Streamcache (e.g. `http://dispatcharr:9191` or `https://dp.domain.ca`). Must match `DISPATCHARR_STREAM_URL` in `dp2strm` for shared MD5 cache keys. |
| **Bypass Header Name** | `string` | `X-Streamcache-Bypass` | HTTP request header that disables redirection if present (secondary loop safety mechanism). |

---

## Administrative Actions

From the Stasharr plugin card in the Dispatcharr UI, two diagnostic actions are available:
* **Check Status**: Verifies that runtime method wrapping is active, checks database enablement, and displays current configuration.
* **Ping Streamcache**: Sends an HTTP probe from Dispatcharr to the configured `Streamcache Base URL` to verify network connectivity.

---

## Running Automated Tests

Stasharr includes a standalone unit test suite that verifies hook injection, URL formatting, cache key compatibility with GoStreamCache, loop avoidance, and fallback error handling:

```bash
python3 -m unittest discover -s tests -v
```

---

## License

MIT
