#!/usr/bin/env python3
"""Radar Lab -- standalone desktop launcher.

The other entry point (radar_lab.py's own __main__) runs headless: bind
0.0.0.0, serve_forever, meant to sit behind systemd/Tailscale on a server
someone reaches with a browser. This one is for running directly on a
laptop as an actual double-click program -- no browser chrome, no address
bar, no separate "start the server, then go open a tab" step.

Same backend, unchanged: this just runs it on a background thread bound
to 127.0.0.1 only (nothing needs to reach it over the network in this
mode) and opens the result in a real native window via pywebview, which
wraps the OS's own webview control (WebView2 on Windows, WKWebView on
macOS, WebKitGTK on Linux) rather than bundling a whole browser.
"""
import threading

import webview

import radar_lab


def main():
    # Found 2026-09-25: this had drifted out of sync with radar_lab.py's
    # own main() (the headless/server entry point) -- lightning and
    # snowplow tracking were both added after this file was first
    # written and never backfilled here, so the desktop app would have
    # silently shipped with those two features always empty. Same three
    # poll loops as the server entry point now.
    radar_lab.get_cache(radar_lab.SITE)  # start warming the default site immediately
    threading.Thread(target=radar_lab.mosaic_poll_loop, daemon=True).start()
    threading.Thread(target=radar_lab.lightning_poll_loop, daemon=True).start()
    threading.Thread(target=radar_lab.snowplow_poll_loop, daemon=True).start()

    server = radar_lab.ThreadingHTTPServer(("127.0.0.1", radar_lab.PORT), radar_lab.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[radar-lab] backend running on 127.0.0.1:{radar_lab.PORT}, opening window")

    webview.create_window(
        "Radar Lab",
        f"http://127.0.0.1:{radar_lab.PORT}",
        width=1280,
        height=820,
        min_size=(640, 480),
    )
    # Blocks on the main thread until the window is closed -- required on
    # macOS (Cocoa demands GUI event loops run on the main thread) and
    # harmless everywhere else, so no platform branch needed here.
    #
    # private_mode=False: pywebview defaults to private/incognito
    # browsing, which wipes localStorage on every relaunch -- silently
    # breaking the night-mode toggle's persistence (app.js:
    # localStorage["radarLabNightMode"]), the one setting that actually
    # matters surviving a restart for the vehicle-mounted use case this
    # app is built for.
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()
