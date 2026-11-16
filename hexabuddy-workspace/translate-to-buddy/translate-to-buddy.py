#!/usr/bin/env python3
"""
Translate selected text on the Hexabuddy server and push it to the desk buddy.

Two output shapes, chosen by the length of the selection:
  * a WORD or short PHRASE  -> POST /translate/card -> {zh, en, ipa}, then POST /card (the server
        renders the flashcard: 中文 meaning / English / IPA) and speaks the word and its meaning.
  * a longer sentence/text  -> POST /translate/text (Chinese -> English, otherwise English -> Chinese),
        then POST /say (spoken, no card).

No model key or image library on this Mac: translation and the card bitmap are the server's, and
count against the account's daily translation quota.

Usage:  translate-to-buddy.py "word or phrase"     (or pipe text on stdin)
Server and desktop key: hooks/config.py (environment → state.json → the bundled connection.json).
"""
import importlib.util, json, os, pathlib, ssl, sys, time, urllib.error, urllib.request

_spec = importlib.util.spec_from_file_location("buddy_config", pathlib.Path(__file__).resolve().parents[1] / "hooks/config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


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


def _post(path, body, timeout=10, tries=2):
    """POST JSON with this Mac's desktop key and return the JSON reply. A network blip is retried
    (tries); an HTTP answer (4xx/5xx) never is, so a translation is not charged twice."""
    data = json.dumps(body).encode()
    hdr = server_headers()
    opener = server_opener()
    for i in range(tries):
        try:
            req = urllib.request.Request(server_base() + path, data=data, headers=hdr, method="POST")
            return json.loads(opener.open(req, timeout=timeout).read() or b"{}")
        except urllib.error.HTTPError as error:
            error.close()                   # the status is the answer; the body is not read
            raise
        except (urllib.error.URLError, OSError):
            if i + 1 == tries:
                raise
            time.sleep(0.4)


QUOTA = "the daily translation limit for this account is reached; try again tomorrow."


def _quota(error):
    return isinstance(error, urllib.error.HTTPError) and error.code == 429


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


def languages(text):
    """(source, target): Chinese -> English, anything else -> Chinese."""
    return ("zh-CN", "en-US") if _cjk(text) else ("en-US", "zh-CN")


# ---- server calls -----------------------------------------------------------------

def say(text, ttl=10):
    _post("/say", {"text": text, "ttl": ttl}, timeout=8)


def card_fields(text):
    """{zh, en, ipa} from the server's model."""
    reply = _post("/translate/card", {"text": text}, timeout=30, tries=1)
    return {k: str(reply.get(k) or "").strip() for k in ("zh", "en", "ipa")}


def card(fields, say_text):
    # No mask: the server renders the flashcard from the text fields, files it in the
    # vocabulary history and speaks say_text.
    body = {"zh": fields["zh"], "en": fields["en"], "ipa": fields["ipa"]}
    if say_text:
        body["say"] = say_text
    _post("/card", body, timeout=10)


def translate_text(text):
    source, target = languages(text)
    reply = _post("/translate/text", {"text": text, "source": source, "target": target}, timeout=45, tries=1)
    return str(reply.get("text") or "").strip()


def main():
    text = (" ".join(sys.argv[1:]) or sys.stdin.read()).strip()
    if not text:
        sys.exit(0)                 # empty selection — exit clean (no Services error dialog)
    text = text[:400]
    _load_server_env()

    if is_short(text):
        f = None
        try:
            f = card_fields(text)
        except Exception as e:                                # noqa: BLE001
            if _quota(e):
                raise RuntimeError(QUOTA) from None
            print(f"(card translate failed: {e})", file=sys.stderr)  # fall back to speaking a translation
        if f and f["zh"] and f["en"]:
            print(f"{f['zh']} / {f['en']} {f['ipa']}".rstrip())
            # Speak the word and its meaning, English first. IPA is skipped:
            # synthesizers read phonetic symbols as noise.
            spoken = f"{f['en']}，{f['zh']}"
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
            return

    try:
        out = translate_text(text)
    except Exception as e:                                    # noqa: BLE001
        if _quota(e):
            raise RuntimeError(QUOTA) from None
        raise RuntimeError(f"Could not translate: {e}") from e
    if not out:
        raise RuntimeError("The server returned no translation.")   # never POST empty text (/say 400s on it)
    print(out)
    say(out)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:   # not connected, quota, translate or save failed: one clear line for Services
        print(f"Translate → Buddy: {error}", file=sys.stderr)
        sys.exit(1)
