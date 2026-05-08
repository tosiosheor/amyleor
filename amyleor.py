#!/usr/bin/env python3
"""
Video Mixer — web UI launcher.
Starts a local FastAPI server then opens the app in a window or browser.

  python3 amyleor.py          # opens browser tab
  python3 amyleor.py --app    # opens native window via pywebview (pip install pywebview)

Requires: fastapi uvicorn[standard]  →  pip install fastapi "uvicorn[standard]"
Optional: pywebview               →  pip install pywebview
"""
import argparse
import socket
import threading
import time
import webbrowser


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int) -> None:
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="Video Mixer")
    parser.add_argument(
        "--app", action="store_true",
        help="Open in a native window (requires pywebview)",
    )
    args = parser.parse_args()

    port = _free_port()
    url  = f"http://127.0.0.1:{port}"

    if args.app:
        try:
            import webview
        except ImportError:
            print("pywebview not installed — falling back to browser.")
            print("Install with:  pip install pywebview")
            args.app = False

    # Start server in background thread
    t = threading.Thread(target=_start_server, args=(port,), daemon=True)
    t.start()

    # Give uvicorn a moment to bind
    time.sleep(0.6)

    if args.app:
        import webview  # already checked above
        webview.create_window("Video Mixer", url, width=1100, height=820)
        webview.start()
    else:
        print(f"Video Mixer running at {url}")
        print("Press Ctrl-C to stop.")
        webbrowser.open(url)
        try:
            t.join()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
