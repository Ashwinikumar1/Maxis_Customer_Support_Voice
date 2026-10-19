"""Draws the GCP architecture diagram for the root README (official GCP icons).

Only needs Pillow (no Graphviz):

    cd Documents/diagrams
    uv run --no-project --with pillow python gcp_architecture.py

Writes ../gcp-architecture.png. Icons in ./icons are the Google Cloud icons
bundled with the `diagrams` project (https://github.com/mingrammer/diagrams).
Solid lines = built today; dashed grey = planned.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
OUT = HERE.parent / "gcp-architecture.png"
S = 2                 # render scale (2x for crisp text on GitHub)
W, H = 1920, 1110     # logical canvas size
ICON = 84

# Google palette
BLUE, GREEN, YELLOW, RED = "#1A73E8", "#188038", "#E37400", "#D93025"
PURPLE, GREY, TEXT = "#9334E6", "#80868B", "#202124"


def font(size, bold=False):
  candidates = (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
                else ["/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"])
  for name in candidates:
    try:
      return ImageFont.truetype(name, size * S)
    except OSError:
      continue
  return ImageFont.load_default(size * S)


img = Image.new("RGB", (W * S, H * S), "white")
d = ImageDraw.Draw(img)
F_TITLE, F_BOX, F_NODE, F_EDGE, F_SMALL = font(28, True), font(15, True), font(14), font(13), font(13)


def sc(*pts):
  return [p * S for p in pts]


def box(x1, y1, x2, y2, label, fill, outline, dashed=False):
  d.rounded_rectangle(sc(x1, y1, x2, y2), radius=12 * S, fill=fill,
                      outline=None if dashed else outline, width=2 * S)
  if dashed:
    for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
      _dash(a, b, outline, 2)
  d.text(sc(x1 + 14, y1 + 10), label, font=F_BOX, fill=outline)


def node(x, y, icon, label, planned=False):
  im = Image.open(HERE / "icons" / f"{icon}.png").convert("RGBA").resize((ICON * S, ICON * S), Image.LANCZOS)
  if planned:  # fade planned components
    alpha = im.getchannel("A").point(lambda a: int(a * 0.45))
    im.putalpha(alpha)
  img.paste(im, (int((x - ICON / 2) * S), int((y - ICON / 2) * S)), im)
  d.multiline_text(sc(x, y + ICON / 2 + 6), label, font=F_NODE, fill=GREY if planned else TEXT,
                   anchor="ma", align="center", spacing=3 * S)


def _dash(a, b, color, width, on=9, off=6):
  (x1, y1), (x2, y2) = a, b
  length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1
  ux, uy, t = (x2 - x1) / length, (y2 - y1) / length, 0.0
  while t < length:
    e = min(t + on, length)
    d.line(sc(x1 + ux * t, y1 + uy * t, x1 + ux * e, y1 + uy * e), fill=color, width=width * S)
    t = e + off


def _head(a, b, color):
  (x1, y1), (x2, y2) = a, b
  length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1
  ux, uy = (x2 - x1) / length, (y2 - y1) / length
  size, half = 13, 6.5
  bx, by = x2 - ux * size, y2 - uy * size
  d.polygon(sc(x2, y2, bx - uy * half, by + ux * half, bx + uy * half, by - ux * half), fill=color)


def edge(points, color, label=None, at=None, style="solid", both=False, width=2, anchor="mm"):
  for a, b in zip(points, points[1:]):
    if style == "solid":
      d.line(sc(*a, *b), fill=color, width=width * S)
    else:
      _dash(a, b, color, width, *((9, 6) if style == "dashed" else (3, 5)))
  _head(points[-2], points[-1], color)
  if both:
    _head(points[1], points[0], color)
  if label:
    x, y = at
    bb = d.multiline_textbbox(sc(x, y), label, font=F_EDGE, anchor=anchor, align="center", spacing=2 * S)
    d.rectangle((bb[0] - 4 * S, bb[1] - 3 * S, bb[2] + 4 * S, bb[3] + 3 * S), fill="white")
    d.multiline_text(sc(x, y), label, font=F_EDGE, fill=color, anchor=anchor, align="center", spacing=2 * S)


# ---------------------------------------------------------------- frames
d.text(sc(W / 2, 22), "Maxis Customer Support Voice Agent  -  Google Cloud architecture",
       font=F_TITLE, fill=TEXT, anchor="ma")

box(40, 190, 250, 720, "Callers", "#F8F9FA", GREY)
box(450, 80, 1890, 1035, "Google Cloud project", "#F8F9FA", "#5F6368")
box(480, 140, 1160, 700, "Cloud Run service: maxis-voice-agent", "#E8F0FE", BLUE)
box(690, 190, 1140, 680, "Python app (Google ADK)", "white", BLUE, dashed=True)
box(1200, 140, 1520, 700, "Vertex AI", "#E6F4EA", GREEN)
box(1570, 140, 1860, 700, "Cloud Storage", "#FEF7E0", YELLOW)
box(480, 770, 1160, 1010, "Build & identity", "#FCE8E6", RED)
box(1200, 770, 1860, 1010, "Cloud Observability", "#F3E8FD", PURPLE)

# ---------------------------------------------------------------- nodes
node(145, 300, "client", "Browser UI\n(mic + speaker)")
node(145, 600, "mobile", "Phone caller\n(planned)", planned=True)
node(340, 600, "server", "Genesys Cloud\n(planned)", planned=True)
node(340, 880, "server", "Maxis Roaming APIs\n(planned)", planned=True)

node(585, 300, "run", "Cloud Run\n1 container, WSS")
node(810, 300, "python", "server.py\nUI + /ws")
node(1020, 300, "python", "LiveVoiceSession\nstreaming_core")
node(1020, 545, "python", "Orchestrator agent\nmaxis_agent")
node(810, 545, "python", "Roaming agent\n(planned)", planned=True)

node(1360, 300, "vertex-ai", "Gemini Live\nspeech-to-speech")
node(1360, 545, "vertex-ai", "Vertex AI Search\nMaxis FAQ")
node(1715, 420, "storage", "Transcript bucket\none JSON per call")

node(610, 880, "build", "Cloud Build")
node(820, 880, "container-registry", "Artifact Registry")
node(1030, 880, "iam", "Service account\nmaxis-voice-agent")
node(1420, 880, "monitoring", "Cloud Trace +\nCloud Monitoring")
node(1690, 880, "logging", "Cloud Logging")

# ---------------------------------------------------------------- edges
# live call path
edge([(190, 300), (540, 300)], GREEN, "PCM16 audio +\nJSON events (WSS)", (365, 268), both=True)
edge([(630, 300), (765, 300)], BLUE)
edge([(855, 300), (975, 300)], BLUE)
edge([(1020, 395), (1020, 500)], BLUE)
edge([(1065, 300), (1315, 300)], BLUE, "bidi audio stream", (1190, 282), both=True)
edge([(1065, 545), (1315, 545)], BLUE, "search_knowledge_base", (1190, 527))
edge([(1020, 255), (1020, 112), (1715, 112), (1715, 375)], YELLOW, "transcript saved at session end", (1370, 112))

# observability
edge([(1120, 700), (1120, 740), (1420, 740), (1420, 835)], PURPLE, style="dotted")
edge([(1420, 740), (1690, 740), (1690, 835)], PURPLE, "traces, metrics, logs", (1555, 740), style="dotted")

# build and identity
edge([(655, 880), (775, 880)], RED, "image", (715, 862))
edge([(820, 770), (820, 702)], RED, "deploy / runs as", (895, 736))

# planned
edge([(190, 600), (295, 600)], GREY, style="dashed")
edge([(385, 580), (510, 580), (510, 330), (545, 330)], GREY, "call audio", (450, 562), style="dashed")
edge([(965, 545), (855, 545)], GREY, "transfer", (910, 527), style="dashed")
edge([(765, 560), (625, 560), (625, 640), (387, 640)], GREY, "human handoff", (505, 658), style="dashed")
edge([(810, 640), (810, 725), (340, 725), (340, 833)], GREY, "account actions", (520, 725), style="dashed")

# ---------------------------------------------------------------- legend
lx, ly = 460, 1068
d.line(sc(lx, ly, lx + 50, ly), fill=BLUE, width=2 * S)
d.text(sc(lx + 60, ly), "built and deployed (test)", font=F_SMALL, fill=TEXT, anchor="lm")
_dash((lx + 260, ly), (lx + 310, ly), GREY, 2)
d.text(sc(lx + 320, ly), "planned: roaming agent, Genesys telephony, human handoff", font=F_SMALL, fill=TEXT, anchor="lm")
_dash((lx + 760, ly), (lx + 810, ly), PURPLE, 2, 3, 5)
d.text(sc(lx + 820, ly), "telemetry (OTLP via Telemetry API)", font=F_SMALL, fill=TEXT, anchor="lm")

img.save(OUT, optimize=True)
print("wrote", OUT)
