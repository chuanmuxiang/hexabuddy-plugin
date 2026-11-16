#!/usr/bin/env python3
"""
Pull the translate vocabulary off the server.

Every word Translate → Buddy has saved lands in your vocabulary history on the server,
with one cached pronunciation per word. This reads it back over HTTPS so you can review it,
back it up, or feed it to a flashcard app.

Usage:
  vocab-export.py                        # newest-first table on stdout
  vocab-export.py --json -o vocab.json   # full backup
  vocab-export.py --csv  -o vocab.csv    # Anki / Quizlet import (en,zh,ipa,added,ts)
  vocab-export.py --audio ./pronounce    # also save each word's WAV
  vocab-export.py --since 2026-09-01     # or --since 7d
  vocab-export.py --grep hedge           # substring match over en / zh / ipa

Server, CA and desktop key: hooks/config.py (environment → state.json → the bundled connection.json);
this Mac must be connected (/hexabuddy-workspace:connect).
"""
import argparse, csv, importlib.util, json, os, pathlib, re, ssl, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request

_spec = importlib.util.spec_from_file_location("buddy_config", pathlib.Path(__file__).resolve().parents[1] / "hooks/config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


def die(msg):
    print("vocab-export: " + msg, file=sys.stderr)
    sys.exit(1)


def server_config():
    """(server, CA file, desktop key) — the same resolution the hooks use."""
    try:
        conn = config.connection()
        key = config.credential(conn)
    except (RuntimeError, ValueError) as error:
        die(str(error))
    if not key:
        die("this Mac is not connected. Run /hexabuddy-workspace:connect.")
    return conn["server"], conn["ca_cert"], key


def server_get(server, ca_cert, key, path, timeout=20, soft=False):
    """GET one server path. soft=True downgrades failures to None (per-word audio, where
    one bad word must not abort the whole export)."""
    req = urllib.request.Request(server + path, headers={"Authorization": "Bearer " + key})
    context = ssl.create_default_context(cafile=ca_cert or None)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if soft:
            return None
        if e.code in (401, 403):
            die("server rejected the desktop key (HTTP %d). Run /hexabuddy-workspace:status." % e.code)
        die("GET %s -> HTTP %d %s" % (path, e.code, e.reason))
    except Exception as e:                      # URLError, timeout, refused connection
        if soft:
            return None
        die("cannot reach the server at %s — %s" % (server, e))


# ── filters ──────────────────────────────────────────────────────────────────
def since_ms(spec):
    """--since accepts 2026-09-01 or 7d/12h. Returns epoch ms."""
    m = re.fullmatch(r"(\d+)([dh])", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return int((time.time() - n * (86400 if unit == "d" else 3600)) * 1000)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(spec, fmt)) * 1000)
        except ValueError:
            continue
    die("--since wants YYYY-MM-DD or Nd/Nh, not %r" % spec)


def select(items, args):
    out = items
    if args.since:
        floor = since_ms(args.since)
        out = [it for it in out if (it.get("ts") or 0) >= floor]
    if args.grep:
        needle = args.grep.lower()
        out = [it for it in out
               if needle in (it.get("en", "") + it.get("zh", "") + it.get("ipa", "")).lower()]
    if args.limit:
        out = out[:args.limit]
    return out


# ── output ───────────────────────────────────────────────────────────────────
def added(it):
    ts = it.get("ts") or 0
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000)) if ts else ""


def _w(s):
    """Display width — the zh column is CJK, which is two cells wide in a terminal."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, n):
    return s + " " * max(0, n - _w(s))


def as_table(items, fh):
    if not items:
        print("(no vocabulary yet)", file=fh)
        return
    rows = []
    for it in items:
        en, ipa, zh = it.get("en", ""), it.get("ipa", ""), it.get("zh", "")
        if not (en or zh):
            en = "(bitmap only)"          # pre-text entry: the Mac rendered a card, kept no words
        rows.append((str(it["i"]), added(it), en, ipa, zh))
    head = ("#", "added", "english", "ipa", "chinese")
    width = [max(_w(r[c]) for r in (rows + [head])) for c in range(5)]
    line = lambda r: "  ".join(_pad(r[c], width[c]) for c in range(5)).rstrip()
    print(line(head), file=fh)
    print("  ".join("-" * width[c] for c in range(5)), file=fh)
    for r in rows:
        print(line(r), file=fh)


def as_json(items, fh):
    json.dump([{**it, "added": added(it)} for it in items], fh, ensure_ascii=False, indent=2)
    fh.write("\n")


def as_csv(items, fh):
    w = csv.writer(fh)
    w.writerow(["en", "zh", "ipa", "added", "ts"])
    skipped = 0
    for it in items:
        if not (it.get("en") or it.get("zh")):
            skipped += 1                  # bitmap-only: nothing to put on a flashcard
            continue
        w.writerow([it.get("en", ""), it.get("zh", ""), it.get("ipa", ""), added(it), it.get("ts", 0)])
    if skipped:
        print("skipped %d bitmap-only entries (no text to export)" % skipped, file=sys.stderr)


# ── pronunciation ────────────────────────────────────────────────────────────
def save_audio(server, ca_cert, key, items, outdir):
    """One WAV per word from GET /vocab/audio. The server serves its cached PCM; a word it
    has never spoken is synthesized on demand, so this can cost TTS calls the first time."""
    os.makedirs(outdir, exist_ok=True)
    saved = cached = missed = 0
    for it in items:
        word = (it.get("en") or it.get("zh") or "").strip()
        if not word:
            missed += 1
            continue
        path = os.path.join(outdir, re.sub(r"[/\\:\0]", "_", word)[:80] + ".wav")
        if os.path.exists(path):
            cached += 1
            continue
        q = urllib.parse.urlencode({"w": word, "fmt": "wav"})
        data = server_get(server, ca_cert, key, "/vocab/audio?" + q, timeout=60, soft=True)
        if not data:
            print("  ! no audio for %r" % word, file=sys.stderr)
            missed += 1
            continue
        with open(path, "wb") as f:
            f.write(data)
        saved += 1
    print("audio: %d saved, %d already there, %d unavailable -> %s"
          % (saved, cached, missed, outdir), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Export the server's translate vocabulary.")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON array (full backup)")
    fmt.add_argument("--csv", action="store_true", help="CSV: en,zh,ipa,added,ts")
    ap.add_argument("-o", "--out", metavar="FILE", help="write here instead of stdout")
    ap.add_argument("--audio", metavar="DIR", help="also save each word's WAV pronunciation")
    ap.add_argument("--since", metavar="WHEN", help="only words from YYYY-MM-DD, or Nd / Nh ago")
    ap.add_argument("--grep", metavar="TEXT", help="only words matching TEXT (en/zh/ipa)")
    ap.add_argument("--limit", type=int, metavar="N", help="at most N (newest first)")
    args = ap.parse_args()

    server, ca_cert, key = server_config()
    body = server_get(server, ca_cert, key, "/vocab/list")
    try:
        items = json.loads(body)["items"]           # newest first; i is the DELETE /vocab?i= index
    except Exception:
        die("unexpected reply from %s/vocab/list: %s" % (server, body[:200]))
    picked = select(items, args)

    write = as_json if args.json else as_csv if args.csv else as_table
    if args.out:
        with open(args.out, "w", newline="") as fh:
            write(picked, fh)
        print("%d of %d words -> %s" % (len(picked), len(items), args.out), file=sys.stderr)
    else:
        write(picked, sys.stdout)
        sys.stdout.flush()                # else the stderr note below races ahead of the table
        if len(picked) != len(items):
            print("(%d of %d words)" % (len(picked), len(items)), file=sys.stderr)

    if args.audio:
        save_audio(server, ca_cert, key, picked, args.audio)


if __name__ == "__main__":
    main()
