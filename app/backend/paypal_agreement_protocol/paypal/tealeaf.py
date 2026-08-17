"""Tealeaf session replay simulation.

Tealeaf (IBM) captures mouse movements, clicks, keyboard events,
DOM snapshots, and sends them gzip-compressed to /platform/tealeaftarget.

Config from patlcfg.js:
- AppKey: 76938917d7504ff7a962174c021690bd  # gitleaks:allow
- Queue: maxEvents=30, timerInterval=20s, maxSize=30KB, encoder=gzip
- Mousemove: sampleRate=200ms, ignoreRadius=3px
- DOM capture: diffEnabled, triggered on click/change/load
"""
import gzip
import json
import time
import random
import uuid
from typing import Optional
from loguru import logger
from config import TEALEAF_APP_KEY, VIEWPORT


def generate_mouse_path(start_x: int, start_y: int, end_x: int, end_y: int,
                        steps: int = 10, interval_ms: int = 200) -> dict:
    """Generate a realistic mouse movement path between two points."""
    dx = []
    dy = []
    ts = []
    for i in range(steps):
        progress = i / (steps - 1) if steps > 1 else 1
        # add slight randomness for natural movement
        jitter_x = random.randint(-3, 3)
        jitter_y = random.randint(-3, 3)
        x = int(start_x + (end_x - start_x) * progress + jitter_x)
        y = int(start_y + (end_y - start_y) * progress + jitter_y)
        dx.append(x)
        dy.append(y)
        ts.append(i * interval_ms + random.randint(0, 50))
    return {"dx": dx, "dy": dy, "ts": ts}


def build_tealeaf_payload(page_url: str, offset_ms: int = 0,
                          mouse_data: Optional[dict] = None,
                          dom_html: Optional[str] = None) -> dict:
    """Build a Tealeaf message batch."""
    messages = []

    # Performance message (type 6)
    messages.append({
        "type": 6,
        "offset": 0,
        "screenviewOffset": 0,
        "performance": {
            "timing": {
                "navigationStart": int(time.time() * 1000) - 5000,
                "domContentLoadedEventEnd": int(time.time() * 1000) - 3000,
                "loadEventEnd": int(time.time() * 1000) - 2000,
            },
        },
    })

    # Screenview message (type 2)
    messages.append({
        "type": 2,
        "offset": offset_ms,
        "screenviewOffset": 0,
        "screenview": {
            "type": "LOAD",
            "name": page_url,
            "url": page_url,
            "host": "www.paypal.com",
            "referrer": "",
        },
    })

    # DOM capture (type 12) - full or diff
    if dom_html:
        messages.append({
            "type": 12,
            "offset": offset_ms + 500,
            "screenviewOffset": 0,
            "domCapture": {
                "dcid": str(uuid.uuid4()),
                "fullDOM": True,
                "root": dom_html[:50000],  # cap at 50KB
                "mutationCount": 0,
            },
        })

    # Mouse movement (type 11)
    if mouse_data:
        messages.append({
            "type": 11,
            "offset": offset_ms + 1000,
            "screenviewOffset": 0,
            "mouseMove": mouse_data,
        })

    return {
        "type": 2,
        "offset": offset_ms,
        "screenviewOffset": 0,
        "count": len(messages),
        "fromWeb": True,
        "messages": messages,
    }


def send_tealeaf_data(session, page_url: str, dom_html: Optional[str] = None,
                      mouse_data: Optional[dict] = None, offset_ms: int = 0):
    """Send Tealeaf session data to PayPal."""
    if not mouse_data:
        # generate default mouse movement (simulate user scrolling/moving)
        mouse_data = generate_mouse_path(
            random.randint(100, 500), random.randint(100, 400),
            random.randint(300, 800), random.randint(200, 600),
        )

    payload = build_tealeaf_payload(page_url, offset_ms, mouse_data, dom_html)
    payload_json = json.dumps(payload).encode("utf-8")
    compressed = gzip.compress(payload_json)

    headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "X-Tealeaf-SaaS-AppKey": TEALEAF_APP_KEY,
        "Origin": "https://www.paypal.com",
    }
    if session.state.tltsid:
        headers["X-Tealeaf-SaaS-TLTSID"] = session.state.tltsid
    if session.state.tltdid:
        headers["X-Tealeaf-TLTDID"] = session.state.tltdid

    try:
        logger.info("Sending Tealeaf session data...")
        session.post(
            "https://www.paypal.com/platform/tealeaftarget",
            content=compressed,
            headers=headers,
        )
    except Exception as e:
        logger.warning(f"Tealeaf send failed: {e}")
