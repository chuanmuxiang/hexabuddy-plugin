#!/usr/bin/env python3
"""
Translate selected text and push it to the desk buddy.

Two output shapes, chosen by the length of the selection:
  * a WORD or short PHRASE  -> a FLASHCARD on the buddy screen:
        中文 (meaning) / english / IPA      (rendered here, POSTed to /card), and spoken.
  * a longer sentence/text  -> the translation spoken aloud via /say (no card).

Language rule:
  English  -> Chinese
  Chinese  -> English
  other    -> English + Chinese

LLM: the active provider in ~/.siliclub/settings.json (agent.active), others as fallbacks (B-13).

Usage:  translate-to-buddy.py "word or phrase"     (or pipe text on stdin)
Server and desktop key: hooks/config.py (environment → state.json → the bundled connection.json).
"""
import base64, importlib.util, json, os, pathlib, ssl, sys, time, urllib.error, urllib.request

_spec = importlib.util.spec_from_file_location("buddy_config", pathlib.Path(__file__).resolve().parents[1] / "hooks/config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)

SILICLUB = os.path.expanduser("~/.siliclub/settings.json")
HISTORY  = str(config.home() / "translations.log")
CACHE    = str(config.home() / "card-cache.json")


def server_base():
    return os.environ.get("HEXABUDDY_SERVER", "")


def _load_server_env():
    """Resolve server, CA and desktop key once into the environment (the Quick Action runs
    with a minimal environment); this Mac must be connected."""
    conn = config.connection()
    key = config.credential(conn)
    if not key:
        raise RuntimeError("This Mac is not connected to Hexabuddy. Run /hexabuddy-workspace:connect.")
    os.environ["HEXABUDDY_SERVER"] = conn["server"]
    os.environ["HEXABUDDY_CA_CERT"] = conn["ca_cert"]
    os.environ["HEXABUDDY_API_KEY"] = key


def _log_history(src, out):
    """Persistent on-disk history that survives server/app restarts (tab-separated)."""
    try:
        flat = lambda t: t.replace("\n", " ").replace("\t", " ")
        with open(HISTORY, "a") as f:
            f.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), flat(src), flat(out)))
    except Exception:
        pass


def server_ssl_context():
    """Trust the configured server certificate without weakening HTTPS verification."""
    certificate = os.environ.get("HEXABUDDY_CA_CERT", "")
    return ssl.create_default_context(cafile=os.path.expanduser(certificate) if certificate else None)


def server_headers():
    key = os.environ.get("HEXABUDDY_API_KEY", "")
    if not config.secure(server_base()):
        raise RuntimeError("Desktop keys require an HTTPS server URL.")
    return {"Authorization": "Bearer " + key, "content-type": "application/json"}


class NoServerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Server redirected the request. Check the configured server address.")


def server_opener():
    return urllib.request.build_opener(NoServerRedirect, urllib.request.HTTPSHandler(context=server_ssl_context()))


def _post(url, body, timeout=10, tries=2):
    """POST JSON to the server with this Mac's desktop key, retrying once on a network blip."""
    data = json.dumps(body).encode()
    hdr = server_headers()
    opener = server_opener()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr, method="POST")
            return opener.open(req, timeout=timeout).read()
        except Exception as e:                       # noqa: BLE001 — retry any transport error
            last = e
            if i + 1 < tries:
                time.sleep(0.4)
    raise last


def load_llms():
    """All configured providers, active first — the callers fall through the list, so one
    provider's network blip (SSL handshake timeout etc.) doesn't kill the translation."""
    if not os.path.isfile(SILICLUB):
        raise RuntimeError("Translate → Buddy needs an LLM configured in ~/.siliclub/settings.json; this Mac has none.")
    s = json.load(open(SILICLUB))["agent"]
    order = [s["active"]] + [n for n in s["providers"] if n != s["active"]]
    out = []
    for name in order:
        p = s["providers"].get(name) or {}
        if not p.get("api_key"):
            continue
        out.append({
            "name": name,
            "key": p["api_key"],
            "base": p["base_url"].rstrip("/"),
            "proto": p.get("api_protocol", "anthropic"),
            "model": p.get("active_model") or p.get("model"),
        })
    return out


def call_llm(llm, prompt, max_tokens=1024, timeout=40, no_think=False):
    """no_think: ask the provider to skip its reasoning pass (faster, and thinking counts against
    max_tokens). Providers that reject the field get one retry without it."""
    if llm["proto"] == "anthropic":
        url = llm["base"] + "/v1/messages"
        hdr = {"x-api-key": llm["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": llm["model"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if no_think:
            body["thinking"] = {"type": "disabled"}
        # content may include a leading "thinking" block — take the first text block.
        def pick(r):
            for b in r.get("content", []):
                if b.get("type") == "text" and b.get("text"):
                    return b["text"]
            return next((b["text"] for b in r.get("content", []) if b.get("text")), "")
    else:  # openai
        url = llm["base"] + "/chat/completions"
        hdr = {"Authorization": "Bearer " + llm["key"], "content-type": "application/json"}
        body = {"model": llm["model"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        pick = lambda r: r["choices"][0]["message"]["content"]
    def send(b):
        req = urllib.request.Request(url, data=json.dumps(b).encode(), headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return pick(json.load(f)).strip()
    try:
        return send(body)
    except urllib.error.HTTPError as e:
        if not no_think or e.code != 400:
            raise
        body.pop("thinking", None)                            # provider doesn't know the field
        return send(body)


def call_llm_any(llms, prompt, max_tokens=1024, timeout=40, no_think=False):
    """Try the active provider twice, then each fallback provider once."""
    last = None
    for i, llm in enumerate(llms):
        for attempt in range(2 if i == 0 else 1):
            try:
                return call_llm(llm, prompt, max_tokens, timeout, no_think)
            except Exception as e:                    # noqa: BLE001 — transport/provider error
                last = e
                print(f"({llm['name']} failed: {e})", file=sys.stderr)
                time.sleep(0.4)
    raise last


# ---- language / length heuristics -------------------------------------------------

def _cjk(text):
    return sum(1 for c in text if "一" <= c <= "鿿")


def is_short(text):
    """A word or short phrase -> flashcard; longer -> spoken sentence."""
    t = text.strip()
    c = _cjk(t)
    if c:                                   # Chinese: no spaces, count characters
        return c <= 6 and len(t) <= 12
    return len(t.split()) <= 3 and len(t) <= 24


# ---- SENTENCE path: translate + speak (/say) --------------------------------------

RULE = (
    "You translate a short selection for a tiny desk display. Apply this rule:\n"
    "- if the text is English -> translate to Simplified Chinese;\n"
    "- if the text is Chinese -> translate to English;\n"
    "- otherwise -> translate to BOTH English and Simplified Chinese, on two short lines.\n"
    "Output ONLY the translation(s): no quotes, no pinyin, no explanation, no labels "
    "(unless both languages are needed, then just the two lines). Keep it concise."
)


def say(text, ttl=10):
    _post(server_base() + "/say", {"text": text, "ttl": ttl}, timeout=8)


# ---- WORD path: flashcard (/card) -------------------------------------------------
# The card shows the Chinese meaning (no pinyin), the English word and its IPA.

CARD_RULE = (
    "Make a flashcard for this word/phrase for a tiny desk display. "
    "Return ONLY compact JSON (no markdown, no code fence) with exactly these keys:\n"
    '  {"zh": "…", "en": "…", "ipa": "…"}\n'
    "- zh: the Simplified Chinese meaning (NO pinyin)\n"
    "- en: the English word/phrase\n"
    "- ipa: IPA of the English, wrapped in slashes (e.g. /ˈæp.əl/)\n"
    "Whether the input is Chinese or English, fill in ALL three fields. "
    "Keep each field short enough for a small screen."
)

W, H = 284, 240
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"


def card_fields(llms, text):
    """{zh, en, ipa} for a word. The LLM round trip is the whole latency, so answers are cached
    per word and asked for without a reasoning pass (a reasoning pass can exhaust max_tokens and
    return no text); an empty reply is retried once with room to think."""
    key = text.strip().lower()
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    hit = cache.get(key)
    if hit and all(hit.get(k) for k in ("zh", "en")):
        return hit

    q = f"{CARD_RULE}\n\nInput:\n{text}"
    raw = call_llm_any(llms, q, max_tokens=200, timeout=20, no_think=True)
    if not raw.strip():                   # provider ignored no_think and thought past the budget
        print("(empty reply — retrying with room to think)", file=sys.stderr)
        raw = call_llm_any(llms, q, max_tokens=1024, timeout=30)
    s = raw.strip()
    if s.startswith("```"):                                   # strip an accidental code fence
        s = s.strip("`")
    s = s[s.find("{"): s.rfind("}") + 1]
    d = json.loads(s)
    out = {k: str(d.get(k, "")).strip() for k in ("zh", "en", "ipa")}
    try:                                                      # remember it; next time is instant
        cache[key] = out
        json.dump(cache, open(CACHE, "w"), ensure_ascii=False)
    except Exception:
        pass
    return out


def render_mask(zh, en, ipa):
    from PIL import Image, ImageDraw, ImageFont                # only needed on the card path
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)

    def fit_font(text, nominal, max_w):
        size = nominal
        while size > 12:
            f = ImageFont.truetype(FONT, size)
            if d.textlength(text, font=f) <= max_w:
                return f
            size -= 2
        return ImageFont.truetype(FONT, 12)

    for text, size, y in [(zh, 44, 34), (en, 38, 112), (ipa, 26, 182)]:
        if not text:
            continue
        f = fit_font(text, size, W - 20)
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, fill=255, font=f)
    # CONTIGUOUS MSB-first row-major 1-bit mask: bit index = y*W + x  (the buddy indexes i>>3;
    # NOT PIL's row-padded tobytes()). W*H = 68160 bits = 8520 bytes -> one WS frame.
    px = img.load()
    bits = bytearray((W * H + 7) // 8)
    for y in range(H):
        row = y * W
        for x in range(W):
            if px[x, y] >= 128:
                idx = row + x
                bits[idx >> 3] |= 0x80 >> (idx & 7)
    return bytes(bits)


def card(fields, say_text):
    mask = render_mask(fields["zh"], fields["en"], fields["ipa"])
    # Text fields ride along so the server's vocab history (and the phone app's
    # text view) keep the words, not just the rendered bitmap.
    body = {"mask": base64.b64encode(mask).decode(),
            "zh": fields.get("zh", ""), "en": fields.get("en", ""), "ipa": fields.get("ipa", "")}
    if say_text:
        body["say"] = say_text
    _post(server_base() + "/card", body, timeout=10)


def main():
    text = (" ".join(sys.argv[1:]) or sys.stdin.read()).strip()
    if not text:
        sys.exit(0)                 # empty selection — exit clean (no Services error dialog)
    text = text[:400]
    os.umask(0o077)                 # history and card cache: owner-only
    _load_server_env()
    llms = load_llms()

    if is_short(text):
        f = None
        try:
            f = card_fields(llms, text)
        except Exception as e:                                # LLM/JSON error -> fall back to /say below
            print(f"(card translate failed: {e})", file=sys.stderr)
        if f and (f["zh"] or f["en"]):
            line = f"{f['zh']} / {f['en']} {f['ipa']}"
            print(line)
            # Speak the word and its meaning, English first. IPA is skipped:
            # synthesizers read phonetic symbols as noise.
            en, zh = f["en"].strip(), f["zh"].strip()
            spoken = f"{en}，{zh}" if en and zh else (en or zh)
            try:
                card(f, say_text=spoken)
            except urllib.error.HTTPError as e:
                if e.code != 423:
                    raise RuntimeError(f"Could not save vocabulary to Buddy: {e}") from e
                # Vocabulary is switched off for this Mac on the phone: speak, save no card.
                say(spoken, ttl=60)
            except Exception as e:
                # Speech alone does not save vocabulary. Let Services report failure.
                raise RuntimeError(f"Could not save vocabulary to Buddy: {e}") from e
            _log_history(text, line)
            return

    try:
        out = (call_llm_any(llms, f"{RULE}\n\nText:\n{text}") or "").strip()
    except Exception as e:                            # every provider down — fail politely
        print(f"(translate failed: {e})")
        return
    if not out:
        print("(no translation returned)")
        return          # never POST empty text (the server 400s on it)
    print(out)
    _log_history(text, out)
    say(out)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:   # not connected, no LLM, save failed: one clear line for Services
        print(f"Translate → Buddy: {error}", file=sys.stderr)
        sys.exit(1)
