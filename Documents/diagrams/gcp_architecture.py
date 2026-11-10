"""Draws a clean, spacious GCP architecture diagram for the Maxis Voice Agent.

Uses official GCP icons in ./icons/ plus clean vector icons for Cloud Armor,
Cloud Load Balancing, Secret Manager, and the Maxis Voicebot Gateway.

Run:
    cd Maxis_Customer_Support_Voice
    PYTHONPATH=/Users/ashwinikm/.cache/uv/archive-v0/1lWUfTFAq5ygS9198vFxA/lib/python3.12/site-packages \
      /Users/ashwinikm/.venv/bin/python3.12 Documents/diagrams/gcp_architecture.py

Writes Documents/gcp-architecture.png.
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
OUT = HERE.parent / "gcp-architecture.png"
S = 2                 # 2x render scale (4200 x 2600 px)
W, H = 2100, 1300     # logical canvas size
ICON = 64

# Google Cloud palette
BLUE, BLUE_BG = "#1A73E8", "#E8F0FE"
GREEN, GREEN_BG = "#188038", "#E6F4EA"
YELLOW, YELLOW_BG = "#E37400", "#FEF7E0"
RED, RED_BG = "#D93025", "#FCE8E6"
PURPLE, PURPLE_BG = "#9334E6", "#F3E8FD"
TEAL, TEAL_BG = "#007B83", "#E4F7FB"
GREY, GREY_BG = "#5F6368", "#F8F9FA"
DARK_GREY, TEXT, WHITE = "#3C4043", "#202124", "#FFFFFF"


def font(size, bold=False):
    candidates = (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
                  else ["/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"])
    for name in candidates:
        try:
            return ImageFont.truetype(name, int(size * S))
        except OSError:
            continue
    return ImageFont.load_default(int(size * S))


img = Image.new("RGB", (W * S, H * S), WHITE)
d = ImageDraw.Draw(img)

F_TITLE = font(26, bold=True)
F_SUBTITLE = font(14, bold=False)
F_BOX = font(15, bold=True)
F_NODE = font(13, bold=True)
F_SUB = font(11.5, bold=False)
F_EDGE = font(11.5, bold=True)
F_LEGEND = font(13, bold=False)


def sc(*pts):
    return [int(round(p * S)) for p in pts]


def _dash(a, b, color, width, on=10, off=7):
    (x1, y1), (x2, y2) = a, b
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1
    ux, uy, t = (x2 - x1) / length, (y2 - y1) / length, 0.0
    while t < length:
        e = min(t + on, length)
        d.line(sc(x1 + ux * t, y1 + uy * t, x1 + ux * e, y1 + uy * e), fill=color, width=int(width * S))
        t = e + off


def box(x1, y1, x2, y2, label, fill, outline, dashed=False, radius=14):
    d.rounded_rectangle(sc(x1, y1, x2, y2), radius=radius * S, fill=fill,
                        outline=None if dashed else outline, width=2 * S)
    if dashed:
        for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
            _dash(a, b, outline, 2)
    d.text(sc(x1 + 16, y1 + 12), label, font=F_BOX, fill=outline)


def custom_icon(kind, size, planned=False):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    idraw = ImageDraw.Draw(im)
    c_main = (26, 115, 232, 255) if kind not in ("armor", "secret") else (217, 48, 37, 255)
    if kind == "gateway":
        c_main = (0, 123, 131, 255)
    cx, cy, r = size / 2, size / 2, size * 0.46
    pts = [
        (cx, cy - r), (cx + r * 0.87, cy - r * 0.5),
        (cx + r * 0.87, cy + r * 0.5), (cx, cy + r),
        (cx - r * 0.87, cy + r * 0.5), (cx - r * 0.87, cy - r * 0.5),
    ]
    bg_col = (232, 240, 254, 255) if kind == "lb" else (252, 232, 230, 255) if kind in ("armor", "secret") else (228, 247, 251, 255)
    idraw.polygon(pts, fill=bg_col, outline=c_main, width=max(2, size // 26))

    w = max(2, size // 20)
    if kind == "armor":
        sh = [
            (cx, cy - r * 0.55), (cx + r * 0.48, cy - r * 0.35),
            (cx + r * 0.42, cy + r * 0.15), (cx, cy + r * 0.58),
            (cx - r * 0.42, cy + r * 0.15), (cx - r * 0.48, cy - r * 0.35),
        ]
        idraw.polygon(sh, fill=c_main, outline=WHITE)
        idraw.line([(cx - r * 0.2, cy + r * 0.02), (cx - r * 0.04, cy + r * 0.20), (cx + r * 0.24, cy - r * 0.14)],
                   fill=WHITE, width=w + 2)
    elif kind == "lb":
        idraw.ellipse((cx - r * 0.16, cy - r * 0.55, cx + r * 0.16, cy - r * 0.23), fill=c_main)
        for ox in (-0.42, 0.0, 0.42):
            bx = cx + r * ox
            idraw.line([(cx, cy - r * 0.23), (bx, cy + r * 0.22)], fill=c_main, width=w)
            idraw.rectangle((bx - r * 0.13, cy + r * 0.22, bx + r * 0.13, cy + r * 0.48), fill=c_main)
    elif kind == "secret":
        idraw.rounded_rectangle((cx - r * 0.36, cy - r * 0.08, cx + r * 0.36, cy + r * 0.50),
                                radius=size // 14, fill=c_main)
        idraw.arc((cx - r * 0.24, cy - r * 0.48, cx + r * 0.24, cy + r * 0.06),
                  start=180, end=0, fill=c_main, width=w + 2)
        idraw.ellipse((cx - r * 0.08, cy + r * 0.10, cx + r * 0.08, cy + r * 0.26), fill=WHITE)
        idraw.line([(cx, cy + r * 0.24), (cx, cy + r * 0.38)], fill=WHITE, width=w)
    elif kind == "gateway":
        idraw.rounded_rectangle((cx - r * 0.42, cy - r * 0.32, cx + r * 0.42, cy + r * 0.32),
                                radius=size // 12, fill=c_main)
        idraw.line([(cx - r * 0.25, cy - r * 0.10), (cx + r * 0.25, cy - r * 0.10)], fill=WHITE, width=w)
        idraw.line([(cx - r * 0.25, cy + r * 0.10), (cx + r * 0.25, cy + r * 0.10)], fill=WHITE, width=w)

    if planned:
        alpha = im.getchannel("A").point(lambda a: int(a * 0.55))
        im.putalpha(alpha)
    return im


def card(x, y, icon, title, subtitle="", planned=False, w=205, h=148, border=None):
    """Draws a clean node card centered at (x, y). Returns (left, top, right, bottom)."""
    x1, y1, x2, y2 = x - w / 2, y - h / 2, x + w / 2, y + h / 2
    bcol = border or (GREY if planned else "#DADCE0")
    d.rounded_rectangle(sc(x1, y1, x2, y2), radius=12 * S, fill=WHITE,
                        outline=None if planned else bcol, width=2 * S)
    if planned:
        for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
            _dash(a, b, bcol, 2, 7, 5)

    iy = y1 + 12 + ICON / 2
    icon_path = HERE / "icons" / f"{icon}.png"
    if icon_path.exists():
        im = Image.open(icon_path).convert("RGBA").resize((ICON * S, ICON * S), Image.LANCZOS)
        if planned:
            alpha = im.getchannel("A").point(lambda a: int(a * 0.50))
            im.putalpha(alpha)
    else:
        im = custom_icon(icon, ICON * S, planned=planned)
    img.paste(im, (int((x - ICON / 2) * S), int((iy - ICON / 2) * S)), im)

    ty = iy + ICON / 2 + 8
    d.multiline_text(sc(x, ty), title, font=F_NODE, fill=GREY if planned else TEXT,
                     anchor="ma", align="center", spacing=2 * S)
    if subtitle:
        bb = d.multiline_textbbox(sc(x, ty), title, font=F_NODE, anchor="ma", align="center", spacing=2 * S)
        sy = bb[3] / S + 4
        d.multiline_text(sc(x, sy), subtitle, font=F_SUB, fill=GREY,
                         anchor="ma", align="center", spacing=3 * S)
    return x1, y1, x2, y2


def _head(a, b, color):
    (x1, y1), (x2, y2) = a, b
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    size, half = 13, 6.5
    bx, by = x2 - ux * size, y2 - uy * size
    d.polygon(sc(x2, y2, bx - uy * half, by + ux * half, bx + uy * half, by - ux * half), fill=color)


def arrow(points, color, label=None, at=None, style="solid", both=False, width=2.8):
    """Draws an orthogonal arrow through points, stopping cleanly at card borders."""
    for a, b in zip(points, points[1:]):
        if style == "solid":
            d.line(sc(*a, *b), fill=color, width=int(width * S))
        else:
            _dash(a, b, color, width, *((10, 6) if style == "dashed" else (4, 5)))
    _head(points[-2], points[-1], color)
    if both:
        _head(points[1], points[0], color)
    if label:
        lx, ly = at if at else ((points[0][0] + points[-1][0]) / 2, (points[0][1] + points[-1][1]) / 2)
        bb = d.multiline_textbbox(sc(lx, ly), label, font=F_EDGE, anchor="mm", align="center", spacing=2 * S)
        px, py = 7 * S, 4 * S
        d.rounded_rectangle((bb[0] - px, bb[1] - py, bb[2] + px, bb[3] + py),
                            radius=6 * S, fill=WHITE, outline=color, width=int(1.4 * S))
        d.multiline_text(sc(lx, ly), label, font=F_EDGE, fill=color, anchor="mm", align="center", spacing=2 * S)


# ==============================================================================
# 1. FRAMES
# ==============================================================================
d.text(sc(W / 2, 18), "Maxis Customer Support Voice Agent  —  Google Cloud Architecture",
       font=F_TITLE, fill=TEXT, anchor="ma")
d.text(sc(W / 2, 52),
       "Project: mxs-maxisapp-prd   •   Cloud Run & Gateways in asia-southeast1 (Singapore)   •   Gemini Live Model in us-central1",
       font=F_SUBTITLE, fill=GREY, anchor="ma")

# Left: Callers & Genesys
box(25, 90, 305, 555, "Callers & Genesys Cloud", GREY_BG, DARK_GREY)

# Main GCP Project box
box(335, 90, 2075, 1190, "Google Cloud Project: mxs-maxisapp-prd", "#FAFAFA", DARK_GREY)

# Region 1: asia-southeast1 (Singapore)
box(360, 138, 1655, 865, "Region: asia-southeast1 (Singapore)", "#F4F8FE", BLUE)

# Inside asia-southeast1:
#   - Edge Security (Proposed)
box(415, 185, 650, 545, "Edge Security (Proposed)", RED_BG, RED, dashed=True)
#   - Cloud Run 1: Genesys Adapter
box(685, 185, 955, 545, "Cloud Run: Genesys Adapter", BLUE_BG, BLUE)
#   - Cloud Run 2: Voice Agent (wide gap before x=1040 for WSS label)
box(1040, 185, 1630, 545, "Cloud Run: maxis-voice-agent (Google ADK)", BLUE_BG, BLUE)
#   - Bottom of Singapore: Maxis Gateways + Knowledge/Storage
box(685, 600, 1115, 840, "Maxis Backend Gateways", TEAL_BG, TEAL)
box(1145, 600, 1630, 840, "Knowledge & Transcripts", YELLOW_BG, YELLOW)

# Region 2: us-central1 (US)
box(1685, 138, 2050, 545, "Region: us-central1 (US)", GREEN_BG, GREEN)

# Bottom row: Secret Manager, IAM & Build, Observability
box(360, 915, 885, 1165, "Secret Manager", RED_BG, RED)
box(915, 915, 1455, 1165, "Build & Identity", "#FEF3E8", YELLOW)
box(1485, 915, 2050, 1165, "Cloud Observability (OpenTelemetry)", PURPLE_BG, PURPLE)

# ==============================================================================
# 2. CARDS
# ==============================================================================
# --- Callers & Genesys (Left column, x=165) ---
c_caller = card(165, 235, "mobile", "Phone Caller", "Calls Maxis (MSISDN)", w=225, h=135)
c_genesys = card(165, 445, "server", "Genesys Audio Connector", "8 kHz MULAW over WSS\nSigns X-API-KEY + HMAC", w=225, h=150, border=BLUE)

# --- Edge Perimeter (Proposed, x=532) ---
c_armor = card(532, 295, "armor", "Cloud Armor", "Genesys IP allowlist\n+ Rate limiting", planned=True, w=200, h=140, border=RED)
c_lb = card(532, 458, "lb", "HTTPS Load Balancer", "Managed TLS cert\nServerless NEG", planned=True, w=200, h=138, border=RED)

# --- Cloud Run 1: Adapter (x=820) ---
c_auth = card(820, 295, "python", "Signature Check", "src/auth.py\nAPI Key + HMAC-SHA256", w=220, h=140, border=RED)
c_bridge = card(820, 458, "run", "ces-genesys-adapter-adk-stg", "AudioHook ↔ /ces bridge\nAdds OIDC + Bearer token", w=220, h=138, border=BLUE)

# --- Cloud Run 2: Voice Agent (x=1190 and x=1475) ---
c_live = card(1190, 295, "python", "LiveVoiceSession", "ces_bridge.py (/ces)\nMULAW ↔ PCM streaming", w=220, h=140, border=BLUE)
c_orch = card(1190, 458, "python", "Orchestrator Agent", "maxis_agent\nGreeting, FAQ & routing", w=220, h=138, border=BLUE)
c_roam = card(1475, 458, "python", "Roaming Agent", "Verifies caller_id\nChecks status & passes", w=220, h=138, border=BLUE)

# --- Maxis Gateways (Singapore, y=735) ---
c_gw = card(900, 735, "gateway", "Voicebot Agent Gateway", "voicebot-cx-studio-agent-gateway\n(OIDC + X-Caller-Id)", w=365, h=150, border=TEAL)

# --- Knowledge & Transcripts (Singapore, y=735) ---
c_search = card(1265, 735, "vertex-ai", "Vertex AI Search", "Maxis FAQ datastore", w=195, h=150, border=GREEN)
c_gcs = card(1505, 735, "storage", "Cloud Storage", "Call transcript JSONs", w=195, h=150, border=YELLOW)

# --- Region 2: us-central1 (x=1867) ---
c_gemini = card(1867, 355, "vertex-ai", "Vertex AI Gemini Live", "gemini-3.8-live (us-central1)\nSpeech-to-speech model", w=300, h=165, border=GREEN)

# --- Bottom Row (y=1050) ---
c_sec_gen = card(500, 1050, "secret", "Genesys Secrets", "api-key-stg &\nclient-secret-stg", w=215, h=150, border=RED)
c_sec_ces = card(745, 1050, "secret", "Voice Agent Token", "voice-agent-ces-token\n(/ces Bearer auth)", w=215, h=150, border=RED)

c_build = card(1055, 1050, "container-registry", "Cloud Build & AR", "Source image builds", w=215, h=150, border=YELLOW)
c_iam = card(1310, 1050, "iam", "Service Accounts & IAM", "Dedicated SAs + Tag for\n--no-invoker-iam-check", w=235, h=150, border=YELLOW)

c_trace = card(1635, 1050, "monitoring", "Trace & Monitoring", "Latency & health metrics", w=225, h=150, border=PURPLE)
c_logs = card(1900, 1050, "logging", "Cloud Logging", "Structured JSON logs\n(auth_error, session)", w=225, h=150, border=PURPLE)

# ==============================================================================
# 3. CLEAN ORTHOGONAL ARROWS (routed exclusively in open gutters)
# ==============================================================================
# 1. Phone Caller -> Genesys Audio Connector
arrow([(165, c_caller[3]), (165, c_genesys[1])], DARK_GREY)

# 2. Genesys -> Cloud Armor (Proposed WSS path, routed in gutter x=385)
arrow([(c_genesys[2], 445), (385, 445), (385, 295), (c_armor[0], 295)], RED,
      "WSS + X-API-KEY\n+ Signature", (385, 370), style="dashed")

# 3. Cloud Armor -> HTTPS LB -> Adapter Signature Check (gutter x=668)
arrow([(532, c_armor[3]), (532, c_lb[1])], RED, style="dashed")
arrow([(c_lb[2], 458), (668, 458), (668, 295), (c_auth[0], 295)], RED, style="dashed")

# 5. Inside Adapter: Signature Check -> Adapter Bridge
arrow([(820, c_auth[3]), (820, c_bridge[1])], BLUE)

# 6. Adapter -> Voice Agent LiveVoiceSession (routed in wide gutter x=998)
arrow([(c_bridge[2], 458), (998, 458), (998, 295), (c_live[0], 295)], BLUE,
      "WSS /ces/us\n(OIDC + Bearer)", (998, 376), both=True)

# 7. Inside Voice Agent: LiveVoiceSession <-> Orchestrator <-> Roaming Agent
arrow([(1190, c_live[3]), (1190, c_orch[1])], BLUE, both=True)
arrow([(c_orch[2], 458), (c_roam[0], 458)], BLUE, "transfer", (1332, 430), both=True)

# 8. LiveVoiceSession <-> Vertex AI Gemini Live (us-central1)
arrow([(c_live[2], 275), (1670, 275), (1670, 355), (c_gemini[0], 355)], GREEN,
      "Bidi Audio Stream", (1475, 275), both=True, width=3.2)

# 9. Roaming Agent -> Maxis Voicebot Gateway (routed in clean horizontal gutter y=572)
arrow([(1475, c_roam[3]), (1475, 572), (900, 572), (900, c_gw[1])], TEAL,
      "HTTPS + OIDC + X-Caller-Id", (1055, 572))

# 10. Orchestrator -> Vertex AI Search (routed via x=1265, label in open space below box header)
arrow([(1235, c_orch[3]), (1235, c_search[1])], GREEN)

# 11. LiveVoiceSession -> Cloud Storage (routed along right side x=1565)
arrow([(c_live[2], 320), (1605, 320), (1605, c_gcs[1])], YELLOW,
      "Transcript JSON", (1605, 572))

# 12. Secret Manager -> Adapter & Voice Agent (routed in clean left gutter x=655 / x=672)
arrow([(500, c_sec_gen[1]), (500, 890), (655, 890), (655, 510), (c_bridge[0], 510)], RED, style="dotted")
arrow([(745, c_sec_ces[1]), (745, 880), (672, 880), (672, 560), (998, 560), (998, 510), (1040, 510)], RED, style="dotted")

# 13. Voice Agent -> Cloud Observability (routed in open column x=1765)
arrow([(1630, 480), (1765, 480), (1765, 915)], PURPLE,
      "Traces, Metrics & Logs", (1765, 700), style="dotted")

# ==============================================================================
# 4. LEGEND
# ==============================================================================
ly = 1245
d.rounded_rectangle(sc(335, 1215, 2075, 1275), radius=10 * S, fill=GREY_BG, outline="#DADCE0", width=int(1.5 * S))

d.line(sc(380, ly, 445, ly), fill=BLUE, width=int(2.8 * S))
_head((430, ly), (445, ly), BLUE)
d.text(sc(460, ly), "Deployed in asia-southeast1", font=F_LEGEND, fill=TEXT, anchor="lm")

d.line(sc(760, ly, 825, ly), fill=GREEN, width=int(3.2 * S))
_head((810, ly), (825, ly), GREEN)
d.text(sc(840, ly), "Gemini Live Stream & Vertex AI Search", font=F_LEGEND, fill=TEXT, anchor="lm")

_dash((1210, ly), (1275, ly), RED, 2.5, 10, 6)
_head((1260, ly), (1275, ly), RED)
d.text(sc(1290, ly), "Proposed Edge Security (Cloud Armor + HTTPS LB)", font=F_LEGEND, fill=TEXT, anchor="lm")

_dash((1740, ly), (1805, ly), PURPLE, 2.5, 4, 5)
_head((1790, ly), (1805, ly), PURPLE)
d.text(sc(1820, ly), "Secrets & Telemetry", font=F_LEGEND, fill=TEXT, anchor="lm")

img.save(OUT, optimize=True)
print("Wrote", OUT, img.size)
