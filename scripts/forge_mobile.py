"""
Forge Mobile — interface mobile + prompt partagé entre appareils.

Page : http://<pc>:7860/mobile/  (ou https://<pc>.<tailnet>.ts.net/mobile/ via « tailscale serve --bg 7860 »)
La génération passe par l'API de Forge : lancer Forge avec --api.
L'état partagé (prompt, LoRAs, format, hires...) est stocké dans
shared_state.json, à la racine de l'extension.
"""

import hashlib
import io
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import time
import traceback
from pathlib import Path

_LOG = Path(__file__).resolve().parent.parent / "forge_mobile.log"


def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


_log("import du script")


def _patch_api_defaults():
    """Forge plante au démarrage si une extension lève une erreur pendant que l'API
    calcule ses valeurs par défaut : tout ce qui suit (Extra Networks, extensions,
    /mobile) n'est alors jamais initialisé. On rend ce calcul tolérant et on note
    l'extension fautive dans forge_mobile.log."""
    try:
        from modules.api import api as api_module
    except Exception:
        _log("patch API : import impossible\n" + traceback.format_exc())
        return
    Api = api_module.Api
    if getattr(Api.init_default_script_args, "_forge_mobile_safe", False):
        return

    def safe_init_default_script_args(self, script_runner):
        import gradio as gr

        last = 1
        for script in script_runner.scripts:
            last = max(last, script.args_to)
        args = [None] * last
        args[0] = 0
        with gr.Blocks():
            for script in script_runner.scripts:
                try:
                    ui = script.ui(script.is_img2img)
                    if not ui:
                        continue
                    values = [getattr(elem, "value", None) for elem in ui]
                    size = script.args_to - script.args_from
                    values = (values + [None] * size)[:size]
                    args[script.args_from:script.args_to] = values
                except Exception:
                    _log(f"Extension ignorée par l'API (erreur dans ui) : {getattr(script, 'filename', '?')}\n"
                         + traceback.format_exc())
        return args

    safe_init_default_script_args._forge_mobile_safe = True
    Api.init_default_script_args = safe_init_default_script_args
    _log("patch API appliqué")


_patch_api_defaults()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from modules import script_callbacks, shared

EXT_DIR = Path(__file__).resolve().parent.parent
HTML_FILE = EXT_DIR / "mobile.html"
STATE_FILE = EXT_DIR / "shared_state.json"
INDEX_FILE = EXT_DIR / "gallery_index.json"
FAV_FILE = EXT_DIR / "favorites.json"
THUMB_DIR = EXT_DIR / "thumbs"
PRESET_FILE = EXT_DIR / "presets.json"
SETTINGS_FILE = EXT_DIR / "settings.json"
HISTORY_FILE = EXT_DIR / "queue_history.json"
TAGS_FILE = EXT_DIR / "tags" / "danbooru.csv"
ASSETS_DIR = EXT_DIR / "assets"
SW_FILE = EXT_DIR / "sw.js"
TRASH_NAME = "_corbeille"

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
LORA_EXT = {".safetensors", ".pt", ".ckpt"}

DEFAULT_STATE = {
    "prompt": "",
    "negative": "",
    "width": 832,
    "height": 1216,
    "loras": [],
    "hires": {
        "enabled": False,
        "upscaler": "R-ESRGAN 4x+ Anime6B",
        "scale": 1.5,
        "denoise": 0.35,
        "steps": 0,
        "tiled": True,
    },
    "sampler": "DPM++ 2M",
    "scheduler": "Karras",
    "steps": 28,
    "cfg": 5.0,
    "seed": -1,
    "batch_count": 1,
}

_lock = threading.Lock()
_STARTED = time.time()


# ---------------------------------------------------------------- état partagé

def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    state = json.loads(json.dumps(DEFAULT_STATE))
    saved = data.get("state", {}) if isinstance(data, dict) else {}
    for key, value in saved.items():
        if key not in DEFAULT_STATE:
            continue
        if isinstance(DEFAULT_STATE[key], dict) and isinstance(value, dict):
            state[key].update({k: v for k, v in value.items() if k in DEFAULT_STATE[key]})
        else:
            state[key] = value
    return {
        "version": int(data.get("version", 0)) if isinstance(data, dict) else 0,
        "updated_at": data.get("updated_at", 0) if isinstance(data, dict) else 0,
        "state": state,
    }


def _write_state(new_state):
    with _lock:
        current = _read_state()
        merged = current["state"]
        for key, value in new_state.items():
            if key not in DEFAULT_STATE:
                continue
            if isinstance(DEFAULT_STATE[key], dict) and isinstance(value, dict):
                merged[key].update({k: v for k, v in value.items() if k in DEFAULT_STATE[key]})
            else:
                merged[key] = value
        record = {"version": current["version"] + 1, "updated_at": time.time(), "state": merged}
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        return record


# ---------------------------------------------------------------- dossiers

def _resolve(path):
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _walk_files(root, exts):
    """Parcourt root en ignorant les dossiers cachés (.tmp.drivedownload...) et la corbeille."""
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != TRASH_NAME]
        for name in files:
            if os.path.splitext(name)[1].lower() in exts:
                yield os.path.join(dirpath, name)


def _output_roots():
    opts = shared.opts
    roots = []
    for key in ("outdir_samples", "outdir_txt2img_samples", "outdir_img2img_samples"):
        value = getattr(opts, key, "") or ""
        if value:
            root = _resolve(value)
            if root not in roots:
                roots.append(root)
    if not roots:
        roots.append(_resolve("outputs/txt2img-images"))
    return roots


def _lora_root():
    custom = getattr(shared.cmd_opts, "lora_dir", None)
    if custom:
        return _resolve(custom)
    try:
        from modules.paths import models_path
        return Path(models_path).resolve() / "Lora"
    except Exception:
        return _resolve("models/Lora")


def _list_loras():
    root = _lora_root()
    loras = []
    infos = _read_lora_info()
    if root.is_dir():
        for dirpath, _, files in os.walk(root):
            for name in files:
                stem, ext = os.path.splitext(name)
                if ext.lower() in LORA_EXT:
                    rel_dir = os.path.relpath(dirpath, root)
                    loras.append({
                        "name": stem,
                        "folder": "" if rel_dir == "." else rel_dir.replace("\\", "/"),
                        "words": infos.get(stem, {}).get("words", []),
                    })
    loras.sort(key=lambda x: x["name"].lower())
    return loras


def _recent(limit):
    items = []
    for root in _output_roots():
        if not root.is_dir():
            continue
        for full in _walk_files(root, IMAGE_EXT):
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            items.append((mtime, full))
    items.sort(reverse=True)
    return [{"path": path, "mtime": mtime} for mtime, path in items[:limit]]


def _safe_image(path):
    try:
        real = Path(path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Chemin invalide")
    if real.suffix.lower() not in IMAGE_EXT or not real.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable")
    for root in _output_roots():
        try:
            real.relative_to(root)
            return real
        except ValueError:
            continue
    raise HTTPException(status_code=403, detail="Hors du dossier outputs")


THUMB_SIZES = {"thumb": (384, 76), "preview": (1600, 86)}  # taille max, qualité webp


def _cached_resize(real, kind="thumb", mtime=None):
    """Miniature (galerie) ou aperçu (visionneuse) en WebP, mis en cache dans thumbs/<kind>/."""
    from PIL import Image

    size, quality = THUMB_SIZES[kind]
    folder = THUMB_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    mtime = mtime if mtime is not None else real.stat().st_mtime
    key = hashlib.sha1(f"{real}|{mtime}|{size}".encode("utf-8")).hexdigest()
    out = folder / f"{key}.webp"
    if not out.exists():
        with Image.open(real) as img:
            if kind == "preview" and max(img.size) <= size and real.suffix.lower() in (".jpg", ".jpeg", ".webp"):
                return real
            img.draft("RGB", (size, size))
            img = img.convert("RGB")
            img.thumbnail((size, size), Image.LANCZOS, reducing_gap=2.0)
            tmp = out.with_suffix(".tmp")
            img.save(tmp, "WEBP", quality=quality, method=4)
            os.replace(tmp, out)
    return out


def _thumbnail(real):
    return _cached_resize(real, "thumb")


def _thumb_warmer():
    """Prépare les miniatures en tâche de fond (les plus récentes d'abord) pour une galerie fluide."""
    # ancien cache (miniatures 512 px à la racine de thumbs/) : on le vide une fois
    try:
        for old in THUMB_DIR.glob("*.webp"):
            old.unlink()
    except OSError:
        pass
    time.sleep(20)
    while True:
        try:
            index = _refresh_index()
            made = 0
            for path, rec in sorted(index.items(), key=lambda kv: -kv[1].get("mtime", 0)):
                try:
                    folder = THUMB_DIR / "thumb"
                    key = hashlib.sha1(f"{Path(path).resolve()}|{os.path.getmtime(path)}|384".encode("utf-8")).hexdigest()
                    if (folder / f"{key}.webp").exists():
                        continue
                    if _busy():
                        time.sleep(5)  # on laisse le GPU/CPU à la génération
                    _cached_resize(Path(path).resolve(), "thumb")
                    made += 1
                    time.sleep(0.03)
                except Exception:
                    continue
            if made:
                _log(f"miniatures : {made} préparées")
        except Exception:
            _log("miniatures : erreur\n" + traceback.format_exc())
        time.sleep(60)


def _parameters(real):
    from PIL import Image

    with Image.open(real) as img:
        text = img.info.get("parameters")
        if not text:
            exif = img.getexif()
            raw = exif.get(0x9286) if exif else None  # UserComment (jpg/webp)
            if isinstance(raw, bytes):
                text = raw.decode("utf-16-be", errors="ignore") if raw.startswith(b"UNICODE") else raw.decode("utf-8", errors="ignore")
                text = text.replace("UNICODE", "", 1).strip("\x00 ")
            elif raw:
                text = str(raw)
    return text or ""


# ---------------------------------------------------------------- galerie (index des métadonnées)

PARAM_RE = re.compile(r'\s*(\w[\w \-/]+):\s*("(?:\\.|[^\\"])+"|[^,]*)(?:,|$)')
LORA_RE = re.compile(r"<lora:([^:>]+)(?::([^:>]+))?[^>]*>", re.I)
INDEX_VERSION = 2
_index_lock = threading.Lock()
_index = None
_index_scan_at = 0.0


def _parse_parameters(text):
    lines = (text or "").strip().split("\n")
    settings = {}
    if lines and len(PARAM_RE.findall(lines[-1])) >= 3:
        settings = {k.strip(): v.strip().strip('"') for k, v in PARAM_RE.findall(lines[-1])}
        lines = lines[:-1]
    body = "\n".join(lines)
    marker = "\nNegative prompt:"
    i = body.find(marker)
    if i >= 0:
        pos, neg = body[:i], body[i + len(marker):]
    elif body.startswith("Negative prompt:"):
        pos, neg = "", body[len("Negative prompt:"):]
    else:
        pos, neg = body, ""
    return pos.strip(), neg.strip(), settings


def _prompt_tags(pos):
    s = LORA_RE.sub(",", pos)
    s = re.sub(r"\bBREAK\b", ",", s)
    tags = []
    for part in re.split(r"[,\n]", s):
        t = re.sub(r":\s*-?[\d.]+", "", part)
        t = re.sub(r"[()\[\]{}]", "", t)
        t = re.sub(r"\s+", " ", t).strip().lower()
        if t and len(t) <= 60 and t not in tags:
            tags.append(t)
    return tags


def _index_record(path, mtime):
    from PIL import Image

    rec = {"path": path, "mtime": mtime, "w": 0, "h": 0, "prompt": "", "negative": "", "tags": [],
           "loras": [], "model": "", "seed": "", "sampler": "", "scheduler": "", "steps": "", "cfg": "",
           "hires": False, "size": ""}
    try:
        with Image.open(path) as img:
            rec["w"], rec["h"] = img.size
        text = _parameters(Path(path))
    except Exception:
        return rec
    pos, neg, st = _parse_parameters(text)
    loras = []
    for name, weight in LORA_RE.findall(pos):
        if not any(l["name"] == name for l in loras):
            loras.append({"name": name, "weight": weight or ""})
    rec.update({
        "prompt": pos, "negative": neg, "tags": _prompt_tags(pos), "loras": loras,
        "model": st.get("Model", ""), "seed": st.get("Seed", ""), "sampler": st.get("Sampler", ""),
        "scheduler": st.get("Schedule type", ""), "steps": st.get("Steps", ""), "cfg": st.get("CFG scale", ""),
        "hires": "Hires upscale" in st, "size": st.get("Size", ""),
    })
    return rec


def _refresh_index(force=False):
    global _index, _index_scan_at
    with _index_lock:
        if _index is None:
            try:
                with open(INDEX_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _index = data.get("items", {}) if data.get("version") == INDEX_VERSION else {}
            except (OSError, ValueError):
                _index = {}
        now = time.time()
        if not force and now - _index_scan_at < 4:
            return _index
        seen, changed = set(), False
        for root in _output_roots():
            if not root.is_dir():
                continue
            for full in _walk_files(root, IMAGE_EXT):
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                seen.add(full)
                rec = _index.get(full)
                if not rec or rec.get("mtime") != mtime:
                    _index[full] = _index_record(full, mtime)
                    changed = True
        for path in [p for p in _index if p not in seen]:
            del _index[path]
            changed = True
        if changed:
            tmp = INDEX_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": INDEX_VERSION, "items": _index}, f, ensure_ascii=False)
            os.replace(tmp, INDEX_FILE)
        _index_scan_at = now
        return _index


def _read_favs():
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _write_favs(favs):
    tmp = FAV_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(favs), f, ensure_ascii=False)
    os.replace(tmp, FAV_FILE)


QUALITY_TAGS = {
    "masterpiece", "best quality", "amazing quality", "very aesthetic", "absurdres", "newest", "highres",
    "high quality", "good quality", "normal quality", "ultra-detailed", "highly detailed", "8k", "4k",
    "high resolution", "very awa", "score_9", "score_8_up", "score_7_up", "safe", "aesthetic",
}


def _count(values, top=None):
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    out = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return out[:top] if top else out


def _day(mtime):
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def _gallery(q="", lora="", model="", tags="", fav=0, sort="new", offset=0, limit=60, day=""):
    index = _refresh_index()
    favs = _read_favs()
    terms = [t for t in q.lower().split() if t]
    tag_list = [t for t in tags.split("|") if t]
    lora_l = lora.lower()

    def keep(r, use_day=True):
        if use_day and day and _day(r["mtime"]) != day:
            return False
        if fav and r["path"] not in favs:
            return False
        if lora_l and lora_l not in [l["name"].lower() for l in r["loras"]]:
            return False
        if model and r.get("model") != model:
            return False
        if tag_list and not all(t in r["tags"] for t in tag_list):
            return False
        if terms:
            hay = (r["prompt"] + " " + r.get("model", "") + " " + " ".join(l["name"] for l in r["loras"])).lower()
            if not all(t in hay for t in terms):
                return False
        return True

    res = [r for r in list(index.values()) if keep(r)]
    res.sort(key=lambda r: r["mtime"], reverse=(sort != "old"))
    facets = {
        "days": sorted(_count([_day(r["mtime"]) for r in (res if not day else [r for r in index.values() if keep(r, False)])]), reverse=True),
        "loras": _count([l["name"] for r in res for l in r["loras"]]),
        "models": _count([r.get("model", "") for r in res]),
        "tags": _count([t for r in res for t in r["tags"] if t not in tag_list and t not in QUALITY_TAGS], top=40),
    }
    page = res[offset:offset + limit]
    return {
        "total": len(res), "all": len(index), "facets": facets,
        "items": [dict(r, fav=r["path"] in favs, day=_day(r["mtime"])) for r in page],
    }


# ---------------------------------------------------------------- fichiers JSON simples

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- presets

PRESET_FIELDS = ("prompt", "negative", "loras", "width", "height", "hires", "sampler", "scheduler", "steps", "cfg", "batch_count")
_preset_lock = threading.Lock()


def _read_presets():
    data = _read_json(PRESET_FILE, {})
    items = data.get("presets", []) if isinstance(data, dict) else []
    return [p for p in items if isinstance(p, dict) and p.get("name")]


def _save_preset(name, state):
    name = str(name).strip()[:60]
    if not name:
        raise HTTPException(status_code=400, detail="Nom vide")
    clean = {k: state[k] for k in PRESET_FIELDS if k in state}
    with _preset_lock:
        items = [p for p in _read_presets() if p["name"] != name]
        items.append({"name": name, "state": clean, "saved_at": time.time()})
        items.sort(key=lambda p: p["name"].lower())
        _write_json(PRESET_FILE, {"presets": items})
    return items


def _delete_preset(name):
    with _preset_lock:
        items = [p for p in _read_presets() if p["name"] != name]
        _write_json(PRESET_FILE, {"presets": items})
    return items


# ---------------------------------------------------------------- réglages (notifications)

DEFAULT_SETTINGS = {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "notify": "end", "click_url": "",
                    "civitai_key": "", "civitai_host": ""}


def _read_settings():
    data = _read_json(SETTINGS_FILE, {})
    out = dict(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        out.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    return out


def _ntfy(title, message, tags=None, sync=False):
    """Notification push via ntfy (appli Android/iOS). Jamais d'image ni de prompt dans le message."""
    s = _read_settings()
    topic = str(s.get("ntfy_topic", "")).strip()
    if not topic:
        return False, "aucun topic"
    body = {"topic": topic, "title": title, "message": message, "tags": tags or []}
    if s.get("click_url"):
        body["click"] = s["click_url"]
    server = (str(s.get("ntfy_server") or "https://ntfy.sh")).rstrip("/")

    def send():
        try:
            req = urllib.request.Request(server, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            return True, "ok"
        except Exception as e:
            _log(f"ntfy : échec ({e})")
            return False, str(e)[:200]

    if sync:
        return send()
    threading.Thread(target=send, daemon=True).start()
    return True, "envoyé"


def _duration(sec):
    sec = int(sec)
    return f"{sec // 60} min {sec % 60:02d}" if sec >= 60 else f"{sec} s"


# ---------------------------------------------------------------- file d'attente

_q_cond = threading.Condition()
_Q = {"pending": [], "running": None, "done": [], "groups": {}, "paused": False, "seq": 0, "rev": 0,
      "run": {"start": 0, "images": 0, "jobs": 0}}
_q_thread = None


def _q_bump():
    _Q["rev"] += 1
    _q_cond.notify_all()


def _q_load():
    data = _read_json(HISTORY_FILE, {})
    if isinstance(data, dict):
        _Q["done"] = data.get("done", [])[:60]
        _Q["groups"] = data.get("groups", {})
        _Q["seq"] = int(data.get("seq", 0))
        pending = [j for j in data.get("pending", []) if isinstance(j, dict) and isinstance(j.get("payload"), dict)]
        if pending:  # file restée en attente avant un redémarrage : on la garde, en pause
            for j in pending:
                j["status"] = "pending"
                j.pop("interrupted", None)
            _Q["pending"] = pending
            _Q["paused"] = True
            _q_start()


def _q_save():
    groups = dict(sorted(_Q["groups"].items(), key=lambda kv: kv[1].get("created", 0))[-20:])
    _Q["groups"] = groups
    try:
        pending = list(_Q["pending"])
        if _Q["running"] and not _Q["running"].get("interrupted"):  # l'image en cours repart en tête si Forge redémarre
            pending.insert(0, dict(_Q["running"]))
        _write_json(HISTORY_FILE, {"done": _Q["done"][:60], "groups": groups, "seq": _Q["seq"], "pending": pending})
    except OSError:
        pass


def _api_base():
    port = getattr(shared.cmd_opts, "port", None) or 7860
    return f"http://127.0.0.1:{port}"


def _run_job(job):
    payload = dict(job["payload"])
    payload.update(save_images=True, send_images=False, do_not_save_grid=True)
    # pas de copie JPG automatique des grosses images (option Forge « Save copy of large images as JPG »)
    payload["override_settings"] = dict(payload.get("override_settings") or {}, export_for_4chan=False)
    req = urllib.request.Request(_api_base() + "/sdapi/v1/txt2img", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=None) as r:
        data = json.loads(r.read() or b"{}")
    try:
        info = json.loads(data.get("info") or "{}")
        return info.get("seed")
    except (ValueError, TypeError):
        return None


def _worker():
    _log("file d'attente : démarrée")
    while True:
        with _q_cond:
            while _Q["paused"] or not _Q["pending"]:
                _q_cond.wait()
            job = _Q["pending"].pop(0)
            job.update(status="running", started=time.time())
            _Q["running"] = job
            _q_save()
            if not _Q["run"]["start"]:
                _Q["run"] = {"start": job["started"], "images": 0, "jobs": 0}
            _q_bump()
        t0 = time.time()
        err = seed = None
        try:
            seed = _run_job(job)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                detail = ""
            err = f"HTTP {e.code} {detail}"
        except Exception as e:
            err = str(e)[:300]
        images = []
        try:
            idx = _refresh_index(force=True)
            prev = set(_Q["done"][0].get("images") or []) if _Q["done"] else set()
            images = sorted((p for p, r in idx.items() if r.get("mtime", 0) >= t0 - 0.2 and p not in prev),
                            key=lambda p: idx[p]["mtime"])
        except Exception:
            _log("file d'attente : index\n" + traceback.format_exc())
        if err:
            _log(f"file d'attente : erreur job {job['id']} : {err}")
        with _q_cond:
            job.update(finished=time.time(), images=images, seed=seed, error=err,
                       status="error" if err else ("interrupted" if job.get("interrupted") else "done"))
            job.pop("interrupted", None)
            _Q["running"] = None
            _Q["done"].insert(0, job)
            del _Q["done"][60:]
            g = _Q["groups"].get(job.get("group") or "")
            if g is not None:
                g.setdefault("cells", {})[job.get("cell", "")] = {"status": job["status"], "images": images, "job": job["id"]}
            _Q["run"]["images"] += len(images)
            _Q["run"]["jobs"] += 1
            remaining = len(_Q["pending"])
            run = dict(_Q["run"])
            if remaining == 0:
                _Q["run"] = {"start": 0, "images": 0, "jobs": 0}
            _q_bump()
            _q_save()
        _notify_job(job, remaining, run)


def _notify_job(job, remaining, run):
    mode = _read_settings().get("notify", "end")
    if mode == "off":
        return
    if job["status"] == "error":
        _ntfy("Forge · erreur", "Une génération a échoué.", ["warning"])
        return
    if job["status"] == "interrupted" and remaining == 0:
        return
    n = len(job.get("images") or [])
    if mode == "each" and remaining:
        _ntfy("Forge · image prête" if n == 1 else f"Forge · {n} images prêtes",
              f"Encore {remaining} dans la file.", ["art"])
    elif remaining == 0:
        total = run["images"]
        _ntfy("Forge · terminé", f"{total} image{'s' if total > 1 else ''} en {_duration(time.time() - run['start'])}.",
              ["white_check_mark"])


def _q_start():
    global _q_thread
    if _q_thread is None or not _q_thread.is_alive():
        _q_thread = threading.Thread(target=_worker, name="forge-mobile-queue", daemon=True)
        _q_thread.start()


def _q_public(job):
    if not job:
        return None
    out = dict(job)  # le payload sert à « Remettre » le prompt et les réglages
    p = job.get("payload", {})
    out["n"] = p.get("n_iter", 1)
    return out


def _q_snapshot():
    with _q_cond:
        return {
            "rev": _Q["rev"], "paused": _Q["paused"],
            "running": _q_public(_Q["running"]),
            "pending": [_q_public(j) for j in _Q["pending"]],
            "done": [_q_public(j) for j in _Q["done"][:30]],
            "groups": _Q["groups"],
        }


def _q_add(jobs, group=None):
    ids = []
    with _q_cond:
        gid = None
        if group:
            _Q["seq"] += 1
            gid = f"g{_Q['seq']}"
            _Q["groups"][gid] = {"id": gid, "created": time.time(), "label": str(group.get("label", ""))[:80],
                                 "x": group.get("x"), "y": group.get("y"), "seed": group.get("seed"), "cells": {}}
        for j in jobs:
            payload = j.get("payload")
            if not isinstance(payload, dict):
                continue
            _Q["seq"] += 1
            job = {"id": f"j{_Q['seq']}", "label": str(j.get("label", ""))[:120], "payload": payload,
                   "created": time.time(), "status": "pending", "group": gid, "cell": str(j.get("cell", ""))}
            _Q["pending"].append(job)
            ids.append(job["id"])
            if gid:
                _Q["groups"][gid]["cells"][job["cell"]] = {"status": "pending", "images": [], "job": job["id"]}
        _q_bump()
        _q_save()
    _q_start()
    return {"ids": ids, "group": gid}


def _q_action(action, jid=None):
    with _q_cond:
        pend = _Q["pending"]
        i = next((k for k, j in enumerate(pend) if j["id"] == jid), -1)
        if action == "cancel" and i >= 0:
            job = pend.pop(i)
            g = _Q["groups"].get(job.get("group") or "")
            if g:
                g["cells"].pop(job.get("cell", ""), None)
        elif action in ("up", "down", "top") and i >= 0:
            job = pend.pop(i)
            k = 0 if action == "top" else max(0, min(len(pend), i + (-1 if action == "up" else 1)))
            pend.insert(k, job)
        elif action == "clear":
            for job in pend:
                g = _Q["groups"].get(job.get("group") or "")
                if g:
                    g["cells"].pop(job.get("cell", ""), None)
            pend.clear()
        elif action == "pause":
            _Q["paused"] = True
        elif action == "resume":
            _Q["paused"] = False
        elif action == "requeue":
            src = next((j for j in _Q["done"] if j["id"] == jid), None)
            if src:
                _Q["seq"] += 1
                pend.append({"id": f"j{_Q['seq']}", "label": src.get("label", ""), "payload": src["payload"],
                             "created": time.time(), "status": "pending", "group": None, "cell": ""})
        elif action in ("interrupt", "stopall"):
            if action == "stopall":
                pend.clear()
            if _Q["running"]:
                _Q["running"]["interrupted"] = True
            try:
                shared.state.interrupt()
            except Exception:
                _log("interrupt impossible\n" + traceback.format_exc())
        elif action == "forget":
            _Q["done"] = [j for j in _Q["done"] if j["id"] != jid] if jid else []
        _q_bump()
        _q_save()
    if action in ("resume", "requeue"):
        _q_start()
    return _q_snapshot()


# ---------------------------------------------------------------- autocomplétion des tags

_tags = None
_tags_lock = threading.Lock()


def _load_tags():
    global _tags
    with _tags_lock:
        if _tags is not None:
            return _tags
        import csv

        rows = []
        try:
            with open(TAGS_FILE, "r", encoding="utf-8", newline="") as f:
                for r in csv.reader(f):
                    if len(r) < 3 or not r[0]:
                        continue
                    try:
                        count = int(r[2] or 0)
                    except ValueError:
                        count = 0
                    aliases = [a.strip().lower() for a in (r[3] if len(r) > 3 else "").split(",") if a.strip()]
                    rows.append((r[0], int(r[1] or 0) if r[1].isdigit() else 0, count, aliases))
        except OSError:
            _log(f"tags : fichier absent ({TAGS_FILE})")
        rows.sort(key=lambda t: -t[2])
        by_name = sorted(range(len(rows)), key=lambda i: rows[i][0])
        names = [rows[i][0] for i in by_name]
        alias = sorted((a, i) for i, r in enumerate(rows) for a in r[3])
        _tags = {"rows": rows, "names": names, "by_name": by_name, "alias": alias, "alias_keys": [a for a, _ in alias]}
        _log(f"tags : {len(rows)} chargés")
        return _tags


def _wildcards():
    out = []
    ext_root = EXT_DIR.parent
    try:
        for d in ext_root.iterdir():
            wc = d / "wildcards"
            if d.name.lower().startswith("sd-dynamic-prompts") and wc.is_dir():
                for full in _walk_files(wc, {".txt"}):
                    rel = os.path.relpath(full, wc).replace("\\", "/")[:-4]
                    out.append(rel)
    except OSError:
        pass
    return sorted(set(out))


def _tag_search(q, limit=12):
    import bisect
    import heapq

    raw = q.strip()
    if raw.startswith("__"):
        key = raw.strip("_").lower()
        return [{"name": f"__{w}__", "cat": "wildcard", "count": 0, "alias": ""}
                for w in _wildcards() if key in w.lower()][:limit]
    q = raw.lower().replace(" ", "_").replace("\\(", "(").replace("\\)", ")")
    if len(q) < 2:
        return []
    t = _load_tags()
    rows = t["rows"]
    found, seen = [], set()

    def add(i, alias=""):
        if i not in seen and len(found) < limit:
            seen.add(i)
            found.append((i, alias))

    # préfixe du nom et préfixe d'un alias (boob → breasts), fusionnés par popularité
    lo = bisect.bisect_left(t["names"], q)
    hi = bisect.bisect_left(t["names"], q + "\uffff")
    cands = [(i, "") for i in heapq.nsmallest(limit, (t["by_name"][k] for k in range(lo, hi)))]
    lo = bisect.bisect_left(t["alias_keys"], q)
    hi = bisect.bisect_left(t["alias_keys"], q + "\uffff")
    cands += heapq.nsmallest(limit * 2, ((i, a) for a, i in t["alias"][lo:hi]), key=lambda ia: ia[0])
    for i, a in sorted(cands, key=lambda ia: (ia[0], ia[1] != "")):  # index bas = plus populaire
        add(i, a)
    if len(found) < limit and len(q) >= 3:
        for i, r in enumerate(rows):
            if q in r[0]:
                add(i)
                if len(found) >= limit:
                    break
    return [{"name": rows[i][0], "cat": rows[i][1], "count": rows[i][2], "alias": a} for i, a in found]


# ---------------------------------------------------------------- corbeille / doublons / grilles

def _grid_roots():
    opts = shared.opts
    roots = []
    for key in ("outdir_grids", "outdir_txt2img_grids", "outdir_img2img_grids"):
        value = getattr(opts, key, "") or ""
        if value:
            r = _resolve(value)
            if r not in roots:
                roots.append(r)
    for default in ("outputs/txt2img-grids", "outputs/img2img-grids"):
        r = _resolve(default)
        if r not in roots and r.is_dir():
            roots.append(r)
    return roots


def _owning_root(real, roots):
    for root in roots:
        try:
            real.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _trash_dir_for(root):
    return root.parent / TRASH_NAME


def _trash_dirs():
    out = []
    for root in _output_roots() + _grid_roots():
        d = _trash_dir_for(root)
        if d not in out:
            out.append(d)
    return out


def _move_to_trash(paths):
    roots = _output_roots() + _grid_roots()
    moved = []
    favs = _read_favs()
    for p in paths:
        try:
            real = Path(p).resolve()
        except (OSError, ValueError):
            continue
        root = _owning_root(real, roots)
        if root is None or not real.is_file() or real.suffix.lower() not in IMAGE_EXT:
            continue
        dest = _trash_dir_for(root) / real.relative_to(root.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        k = 1
        while dest.exists():
            dest = dest.with_name(f"{real.stem}~{k}{real.suffix}")
            k += 1
        shutil.move(str(real), str(dest))
        favs.discard(str(real))
        moved.append({"from": str(real), "to": str(dest)})
    if moved:
        _write_favs(favs)
        _refresh_index(force=True)
        _log(f"corbeille : {len(moved)} fichier(s) déplacé(s)")
    return moved


def _restore(items):
    trash = _trash_dirs()
    roots = _output_roots() + _grid_roots()
    restored = 0
    for it in items:
        try:
            src, dst = Path(it["to"]).resolve(), Path(it["from"]).resolve()
        except (KeyError, OSError, ValueError, TypeError):
            continue
        if _owning_root(src, trash) is None or _owning_root(dst, roots) is None:
            continue
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored += 1
    if restored:
        _refresh_index(force=True)
    return restored


def _trash_info():
    count = size = 0
    for d in _trash_dirs():
        if d.is_dir():
            for dirpath, _, files in os.walk(d):
                for name in files:
                    count += 1
                    try:
                        size += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        pass
    return {"count": count, "bytes": size, "dirs": [str(d) for d in _trash_dirs() if d.is_dir()]}


def _find_duplicates():
    """Fichiers strictement identiques (même contenu). On garde le plus ancien, sans ' (1)' dans le nom."""
    by_size = {}
    for root in _output_roots():
        if root.is_dir():
            for full in _walk_files(root, IMAGE_EXT):
                try:
                    by_size.setdefault(os.path.getsize(full), []).append(full)
                except OSError:
                    pass
    extra, size = [], 0
    for sz, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = {}
        for p in paths:
            h = hashlib.sha1()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            by_hash.setdefault(h.hexdigest(), []).append(p)
        for group in by_hash.values():
            if len(group) > 1:
                group.sort(key=lambda p: (" (" in os.path.basename(p), os.path.getmtime(p), len(p)))
                extra.extend(group[1:])
                size += sz * (len(group) - 1)
    return extra, size


def _list_grids():
    files, size = [], 0
    for root in _grid_roots():
        if root.is_dir():
            for full in _walk_files(root, IMAGE_EXT):
                files.append(full)
                try:
                    size += os.path.getsize(full)
                except OSError:
                    pass
    return files, size


# ---------------------------------------------------------------- bibliothèque de tags (auto, depuis les métadonnées)

TAGLIB_CATS = [
    ("persos", "Persos & LoRAs"),
    ("poses", "Poses & cadrage"),
    ("tenues", "Tenues"),
    ("lieux", "Lieux & fonds"),
    ("ambiance", "Lumière & ambiance"),
    ("expressions", "Expressions"),
    ("apparence", "Apparence"),
    ("style", "Style"),
    ("autres", "Autres"),
]


def _kw(words):
    return re.compile(r"\b(?:" + "|".join(words) + r")\b")


TAGLIB_RULES = [
    ("apparence", _kw([r"\w*hair", "eyes", "skin", "breasts"])),
    ("ambiance", _kw([r"light\w*", "lit", r"glow\w*", "shadows?", "color scheme", "colou?rs?", "atmosphere", "bokeh",
                      "depth of field", "cinematic", "dramatic", "dim", r"backlight\w*", "sunlight", "moonlight",
                      "film grain", "hdr", "vibrant", "pastel", "monochrome", "fog", "mist", "haze", "sparkles?",
                      "particles", "lens flare", "chromatic aberration", "contrast", "moody", "warm", "cold tones?"])),
    ("expressions", _kw([r"smil\w*", r"blush\w*", "open mouth", "closed mouth", "closed eyes", r"tongue\w*", "crying", "tears",
                         "angry", r"frown\w*", "surprised", r"expression\w*", "smirk", r"grin\w*", r"pout\w*", r"wink\w*",
                         "embarrassed", "happy", "sad", r"laugh\w*", "shy", "seductive", "half-closed eyes", "parted lips",
                         "drooling", "saliva", "teeth", "nose blush", "ahegao", "naughty face", "expressionless"])),
    ("poses", _kw(["sitting", "standing", "lying", "kneeling", "squatting", "crouching", r"leaning\w*", "yokozuwari",
                   "seiza", "wariza", "indian style", "on back", "on stomach", "on side", "all fours", r"spread\w*",
                   "crossed", "arms?", "legs? up", "hands?", r"holding\w*", "pose", "posing", "from behind", "from side",
                   "from above", "from below", r"looking\w*", "view", "close-up", "portrait", "cowboy shot", "full body",
                   "upper body", "lower body", "wide shot", "dutch angle", "pov", "profile", "walking", "running",
                   "jumping", "stretching", "arched back", "bent over", "top-down", "bottom-up", "straddling",
                   "riding", "hugging", "head tilt", "contrapposto", "foreshortening", "feet", "soles", "thighs together",
                   "knees up", "legs apart", "on bed", "on floor", "on couch", "against wall", r"facing \w+"])),
    ("tenues", _kw([r"dress\w*", r"shirt\w*", r"skirt\w*", "pants", "shorts", "jacket", "coat", "hoodie", "sweater",
                    "uniform", "bikini", "swimsuit", "lingerie", "bra", "panties", "underwear", "thighhighs",
                    "stockings", "pantyhose", "socks", "boots", "shoes", "heels", "gloves", "hat", "cap", "glasses",
                    "choker", "collar", "necklace", "earrings", "jewelry", "armou?r", "kimono", "costume", "outfit",
                    "clothes", "clothing", "naked", "nude", "topless", "bottomless", "bodysuit", "leotard", "apron",
                    "cape", "scarf", "tie", "belt", "corset", "harness", "mask", "crop top", "tank top", r"garter\w*",
                    "ribbon", "bow", "hair ornament", "headband", "veil", "tiara", "crown", "bell", "barefoot",
                    "bare feet", "bare shoulders", "cleavage", "sleeves?", "hood", "suit", "maid", "nurse", "school",
                    "sweatpants", "jeans", "leggings", "fishnets", "latex", "lace"])),
    ("lieux", _kw(["background", "indoors", "outdoors", "room", "bedroom", "bathroom", "kitchen", "office", "classroom",
                   r"city\w*", "street", "alley", "forest", "beach", "ocean", "sea", "sky", "night", "sunset", "sunrise",
                   "rain", "snow", "castle", "palace", "temple", "house", "building", "rooftop", "window", "bed",
                   "couch", "sofa", "garden", "field", "mountains?", "cave", "space", "spaceship", "ship", "cafe",
                   "restaurant", "bar", "library", "scenery", "landscape", "interior", "cyberpunk", "futuristic",
                   "sci-fi", "fantasy", "ruins", "dungeon", "throne", "shower", "bath", "pool", "onsen", "hallway",
                   "stairs", "balcony", "park", "train", "car", "village", "tavern", "studio", "stage", "underwater",
                   "jungle", "desert", "lake", "river", "waterfall", "station", "elven", "medieval", "galaxy"])),
    ("apparence", _kw([r"\w*hair\w*", "eyes", "breasts", "skin", "body", "ears", "tail", "horns", "wings", "nails",
                       "makeup", "lips", "freckles", "moles?", "muscular", "petite", "slim", "curvy", r"thick\w*",
                       "tall", "short", "1girl", "1boy", r"\d+girls", r"\d+boys", "solo", "girl", "boy", "woman", "man",
                       "elf", "catgirl", "animal ears", "bangs", "ponytail", "twintails", "braids?", "ahoge", "pupils",
                       "eyelashes", "abs", "navel", "midriff", r"tattoo\w*", "piercing", "markings", "scar", "mature",
                       "milf", "loli", "teen", "fang", "pointy ears", r"dark-skinned \w+", r"tan\w*"])),
    ("style", _kw(["style", "art", "anime", "realistic", "semi-realistic", r"photo\w*", "painting", "sketch",
                   "watercolor", "comic", "halftone", "cel shading", "3d", "illustration", "lineart", "manga",
                   "retro", "vintage", "chibi", "pixel", "oil", "digital", "traditional media", "concept art",
                   "artist", r"by \w+"])),
]


def _lib_tokens(prompt):
    s = LORA_RE.sub(",", prompt or "")
    s = re.sub(r"\bBREAK\b", ",", s)
    out = []
    for part in re.split(r"[,\n]", s):
        t = re.sub(r"(?<!\\)[()\[\]{}]", "", part)
        t = re.sub(r":\s*-?[\d.]+\s*$", "", t.strip())
        t = t.replace("\\(", "(").replace("\\)", ")")
        t = re.sub(r"\s+", " ", t).strip().lower()
        if t and len(t) <= 60 and not re.fullmatch(r"[\d.\s:-]+", t) and t not in out:
            out.append(t)
    return out


def _tag_category(tag, dan):
    if tag in QUALITY_TAGS:
        return None
    cat = dan.get(tag.replace(" ", "_"))
    if cat == 4 or cat == 3:
        return "persos"
    if cat == 1:
        return "style"
    if cat == 5:
        return None
    for name, rx in TAGLIB_RULES:
        if rx.search(tag):
            return name
    return "autres"


_taglib_cache = {"key": None, "data": None}


def _taglib():
    index = _refresh_index()
    key = (len(index), max((r["mtime"] for r in index.values()), default=0))
    if _taglib_cache["key"] == key:
        return _taglib_cache["data"]
    t = _load_tags()
    dan = {r[0]: r[1] for r in t["rows"]} if t["rows"] else {}
    counts, with_lora = {}, {}
    for r in index.values():
        toks = _lib_tokens(r.get("prompt", ""))
        names = {l["name"]: l.get("weight", "") for l in r.get("loras", [])}
        for tok in toks:
            counts[tok] = counts.get(tok, 0) + 1
            if names:
                d = with_lora.setdefault(tok, {})
                for n, w in names.items():
                    d.setdefault(n, []).append(w)
    cats = {c: [] for c, _ in TAGLIB_CATS}
    for tok, n in counts.items():
        if n < 2:
            continue
        cat = _tag_category(tok, dan)
        if cat is None:
            continue
        lora = None
        assoc = with_lora.get(tok, {})
        known = dan.get(tok.replace(" ", "_"))
        if assoc and (known is None or known in (3, 4)):  # perso, série ou mot-clé inconnu de Danbooru
            name, ws = max(assoc.items(), key=lambda kv: len(kv[1]))
            share = len(ws) / n
            if share >= 0.8 and n >= 3:  # tag quasi toujours utilisé avec ce LoRA = mot-clé du LoRA
                nums = sorted(float(w) for w in ws if re.fullmatch(r"-?\d+(\.\d+)?", str(w)))
                lora = {"name": name, "weight": nums[len(nums) // 2] if nums else 0.8}
                if cat == "autres":
                    cat = "persos"
        cats[cat].append({"tag": tok, "n": n, **({"lora": lora} if lora else {})})
    out = []
    for c, label in TAGLIB_CATS:
        items = sorted(cats[c], key=lambda x: (-x["n"], x["tag"]))[:120]
        if items:
            out.append({"id": c, "label": label, "tags": items})
    data = {"cats": out, "images": len(index)}
    _taglib_cache.update(key=key, data=data)
    return data


# ---------------------------------------------------------------- import de LoRA (glisser-déposer)

def _refresh_forge_loras():
    import sys

    for modname in ("networks", "sd_forge_lora.networks"):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "list_available_networks"):
            try:
                mod.list_available_networks()
                return True
            except Exception:
                _log("rafraîchissement des LoRAs Forge : erreur\n" + traceback.format_exc())
    return False


# ---------------------------------------------------------------- Civitai (recherche + téléchargement de LoRAs)

CIVITAI_DEFAULT = "https://civitai.com"
LORA_INFO_FILE = EXT_DIR / "lora_info.json"
_dl_lock = threading.Lock()
_downloads = {}


def _civitai_host():
    host = str(_read_settings().get("civitai_host") or CIVITAI_DEFAULT).strip().rstrip("/")
    return host if host.startswith("http") else "https://" + host


def _civitai_get(path, params=None, timeout=25):
    url = _civitai_host() + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": "ForgeMobile/1.0", "Accept": "application/json"}
    key = str(_read_settings().get("civitai_key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _lora_files():
    root = _lora_root()
    if not root.is_dir():
        return set()
    return {os.path.basename(p).lower() for p in _walk_files(root, LORA_EXT)}


def _civitai_search(q="", base="", nsfw=True, sort="Most Downloaded", cursor=""):
    params = {"limit": 24, "types": "LORA", "nsfw": "true" if nsfw else "false"}
    if q:
        params["query"] = q
    if base:
        params["baseModels"] = base
    if sort:
        params["sort"] = sort
    if cursor:
        params["cursor"] = cursor
    try:
        data = _civitai_get("/api/v1/models", params)
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Civitai a répondu {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Civitai injoignable ({str(e)[:120]})")
    installed = _lora_files()
    items = []
    for m in data.get("items", []):
        versions = []
        for v in m.get("modelVersions", [])[:8]:
            files = [f for f in v.get("files", []) if str(f.get("name", "")).lower().endswith((".safetensors", ".pt", ".ckpt"))]
            f = next((f for f in files if f.get("primary")), files[0] if files else None)
            if not f:
                continue
            img = next((i for i in v.get("images", []) if i.get("type", "image") == "image" and i.get("url")), None)
            versions.append({
                "id": v.get("id"), "name": v.get("name", ""), "base": v.get("baseModel", ""),
                "words": [w for w in (v.get("trainedWords") or []) if isinstance(w, str)][:12],
                "file": f.get("name", ""), "sizeKB": f.get("sizeKB") or 0,
                "image": img.get("url") if img else None,
                "installed": str(f.get("name", "")).lower() in installed,
            })
        if versions:
            stats = m.get("stats") or {}
            items.append({
                "id": m.get("id"), "name": m.get("name", ""), "creator": (m.get("creator") or {}).get("username", ""),
                "downloads": stats.get("downloadCount", 0), "likes": stats.get("thumbsUpCount", 0),
                "versions": versions,
            })
    meta = data.get("metadata") or {}
    return {"items": items, "next": meta.get("nextCursor") or ""}


def _read_lora_info():
    data = _read_json(LORA_INFO_FILE, {})
    return data if isinstance(data, dict) else {}


def _download_worker(key, url, dest, info):
    tmp = dest.with_name(dest.name + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ForgeMobile/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            with _dl_lock:
                _downloads[key]["total"] = total
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                with _dl_lock:
                    _downloads[key]["done"] += len(chunk)
                    if _downloads[key].get("cancel"):
                        raise RuntimeError("annulé")
        if tmp.stat().st_size < 1024:
            raise RuntimeError("fichier vide")
        os.replace(tmp, dest)
        infos = _read_lora_info()
        infos[dest.stem] = info
        _write_json(LORA_INFO_FILE, infos)
        _refresh_forge_loras()
        with _dl_lock:
            _downloads[key]["status"] = "done"
        _log(f"Civitai : {dest.name} téléchargé")
    except urllib.error.HTTPError as e:
        msg = "clé API Civitai requise (Réglages)" if e.code in (401, 403) else f"Civitai a répondu {e.code}"
        with _dl_lock:
            _downloads[key].update(status="error", error=msg)
        _log(f"Civitai : échec {dest.name} ({e.code})")
    except Exception as e:
        with _dl_lock:
            _downloads[key].update(status="error", error=str(e)[:160])
        _log(f"Civitai : échec {dest.name} ({e})")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _civitai_download(body):
    try:
        version = int(body.get("version"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'version' manquant")
    fname = os.path.basename(str(body.get("file") or f"civitai_{version}.safetensors").replace("\\", "/"))
    if os.path.splitext(fname)[1].lower() not in LORA_EXT:
        fname += ".safetensors"
    root = _lora_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / fname
    if dest.exists():
        raise HTTPException(status_code=409, detail="Déjà installé")
    key = str(version)
    with _dl_lock:
        if key in _downloads and _downloads[key]["status"] == "running":
            return _downloads[key]
        _downloads[key] = {"version": version, "file": fname, "name": dest.stem, "title": str(body.get("name", ""))[:120],
                           "total": 0, "done": 0, "status": "running", "error": "", "started": time.time()}
    params = {}
    token = str(_read_settings().get("civitai_key") or "").strip()
    if token:
        params["token"] = token  # le téléchargement redirige vers un stockage externe : le jeton passe par l'URL
    url = _civitai_host() + f"/api/download/models/{version}" + ("?" + urllib.parse.urlencode(params) if params else "")
    info = {"words": [w for w in (body.get("words") or []) if isinstance(w, str)][:12], "base": str(body.get("base", "")),
            "civitai_version": version, "title": str(body.get("name", ""))[:120]}
    threading.Thread(target=_download_worker, args=(key, url, dest, info), daemon=True).start()
    return _downloads[key]


# ---------------------------------------------------------------- TIPO (suggestions de tags par IA locale, sur le processeur)

TIPO_MODEL = "KBlueLeaf/TIPO-500M-ft"
_tipo = {"install": "", "state": "", "error": "", "lock": threading.Lock()}


def _tipo_available():
    import importlib.util

    try:
        return importlib.util.find_spec("kgen") is not None
    except Exception:
        return False


def _tipo_install():
    import importlib
    import subprocess
    import sys

    _tipo.update(install="running", error="")
    _log("TIPO : installation de tipo-kgen")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "tipo-kgen", "--no-deps"],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            importlib.invalidate_caches()
            _tipo["install"] = "done"
            _log("TIPO : installé")
        else:
            _tipo.update(install="error", error=(r.stderr or r.stdout or "")[-500:])
            _log("TIPO : échec de l'installation\n" + _tipo["error"])
    except Exception as e:
        _tipo.update(install="error", error=str(e)[:300])


def _tipo_load():
    import torch
    import kgen.models as km
    from transformers import LlamaForCausalLM, LlamaTokenizer

    if km.text_model is not None:
        return
    _tipo["state"] = "loading"
    _log(f"TIPO : chargement de {TIPO_MODEL} (premier lancement = téléchargement ~1 Go)")
    t0 = time.time()
    km.text_model = LlamaForCausalLM.from_pretrained(TIPO_MODEL, torch_dtype=torch.float32).requires_grad_(False).eval()
    km.tokenizer = LlamaTokenizer.from_pretrained(TIPO_MODEL)
    km.current_model_name = TIPO_MODEL.split("/")[-1]
    _tipo["state"] = "ready"
    _log(f"TIPO : prêt en {time.time() - t0:.0f} s")


def _tipo_suggest(prompt, describe="", length="short", seed=0):
    import random as _random

    if not _tipo_available():
        raise HTTPException(status_code=409, detail="TIPO n'est pas installé")
    import kgen.executor.tipo as tipo_mod
    from kgen.executor.tipo import parse_tipo_request, tipo_runner
    from kgen.formatter import seperate_tags

    # kgen coupe les ajouts quand le prompt d'entrée est déjà long (bug : il compare à la limite des phrases) :
    # on lève ses limites et on plafonne nous-mêmes le nombre de suggestions.
    tipo_mod.TARGET_TIPO_MAX = {k: 1000 for k in ("very_short", "short", "long", "very_long")}
    tipo_mod.TARGET_TIPO_NL_MAX = {k: 1000 for k in ("very_short", "short", "long", "very_long")}
    cap = {"very_short": 6, "short": 12, "long": 24}.get(length, 12)
    length = {"very_short": "long", "short": "very_long", "long": "very_long"}.get(length, "very_long")
    toks = _lib_tokens(prompt)
    tag_map = seperate_tags(toks)
    describe = (describe or "").strip()
    with _tipo["lock"]:
        try:
            _tipo_load()
        except Exception as e:
            _tipo.update(state="error", error=str(e)[:300])
            _log("TIPO : chargement impossible\n" + traceback.format_exc())
            raise HTTPException(status_code=500, detail="Chargement du modèle TIPO impossible : " + str(e)[:200])
        meta, _ops, general, nl = parse_tipo_request(tag_map, describe, expand_tags=True, expand_prompt=False,
                                                     generate_extra_nl_prompt=False, tag_length_target=length,
                                                     add_quality=False)
        ops = [["short_to_tag", length, True]] if describe else [[None, length, True]]
        t0 = time.time()
        parsed, _timing = tipo_runner(meta, ops, general, describe, seed=seed or _random.randint(1, 2 ** 31))
        took = time.time() - t0
    t = _load_tags()
    dan = t.get("dan")
    if dan is None:
        dan = t["dan"] = {r[0]: r[1] for r in t["rows"]}
    have, out = set(toks), []
    for key in ("characters", "copyrights", "artist", "special", "general", "meta"):
        vals = parsed.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        for tag in vals:
            tl = str(tag).strip().lower()
            if not tl or tl in have or tl in QUALITY_TAGS or key == "meta" and tl in ("highres", "absurdres"):
                continue
            have.add(tl)
            cat = "persos" if key in ("characters", "copyrights") else "style" if key == "artist" else (_tag_category(tl, dan) or "autres")
            out.append({"tag": tl, "cat": cat})
    if not out:
        _log(f"TIPO : aucune suggestion (entrée {len(toks)} tags, sortie {sum(len(v) for v in parsed.values() if isinstance(v, list))})")
    return {"suggestions": out[:cap], "seconds": round(took, 1)}


# ---------------------------------------------------------------- serveur (redémarrer / libérer la VRAM)

def _busy():
    try:
        return bool(shared.state.job_count) or bool(shared.state.job)
    except Exception:
        return False


def _vram():
    try:
        from backend import memory_management as mm
        dev = mm.get_torch_device()
        return round(mm.get_free_memory(dev) / 1024 ** 3, 1), round(mm.get_total_memory(dev) / 1024 ** 3, 1)
    except Exception:
        return None, None


def _free_vram():
    from backend import memory_management as mm

    mm.unload_all_models()
    mm.soft_empty_cache(force=True)
    free, total = _vram()
    _log(f"VRAM libérée : {free} / {total} Go libres")
    return free


def _restart_later():
    from modules import restart

    if not restart.is_restartable():
        raise HTTPException(status_code=400, detail="Forge n'a pas été lancé par webui.bat : redémarrage impossible")
    with _q_cond:
        _q_save()

    def go():
        time.sleep(0.8)  # laisse partir la réponse HTTP
        _log("redémarrage demandé depuis /mobile")
        try:
            shared.state.interrupt()
        except Exception:
            pass
        restart.restart_program()

    threading.Thread(target=go, daemon=True).start()


# ---------------------------------------------------------------- appli installable (PWA)

def _manifest():
    return {
        "name": "Forge Mobile", "short_name": "Forge", "id": "/mobile/",
        "start_url": "/mobile/", "scope": "/mobile/", "display": "standalone",
        "background_color": "#0b0b12", "theme_color": "#0b0b12", "orientation": "any",
        "icons": [
            {"src": "/mobile/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/mobile/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/mobile/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


# ---------------------------------------------------------------- routes

def on_app_started(_demo, app: FastAPI):
    _log(f"app_started appelé (app={type(app).__name__})")
    _q_load()
    threading.Thread(target=_load_tags, name="forge-mobile-tags", daemon=True).start()
    threading.Thread(target=_thumb_warmer, name="forge-mobile-thumbs", daemon=True).start()
    try:
        _register_routes(app)
        _log("routes /mobile enregistrées")
    except Exception:
        _log("ERREUR à l'enregistrement des routes :\n" + traceback.format_exc())
        raise


def _register_routes(app):
    def page():
        return HTMLResponse(
            HTML_FILE.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    app.add_api_route("/mobile/", page, methods=["GET"], response_class=HTMLResponse)

    @app.get("/mobile")
    def to_slash():
        return RedirectResponse("/mobile/", status_code=307)

    @app.get("/mobile/manifest.webmanifest")
    def manifest():
        return JSONResponse(_manifest(), media_type="application/manifest+json")

    @app.get("/mobile/sw.js")
    def service_worker():
        return Response(SW_FILE.read_text(encoding="utf-8"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/mobile/"})

    for icon in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        def make(icon=icon):
            def serve():
                return FileResponse(ASSETS_DIR / icon, media_type="image/png", headers={"Cache-Control": "max-age=604800"})
            return serve
        app.add_api_route(f"/mobile/{icon}", make(), methods=["GET"])

    # --- file d'attente
    @app.get("/mobile/api/queue")
    def get_queue():
        return JSONResponse(_q_snapshot(), headers={"Cache-Control": "no-store"})

    @app.post("/mobile/api/queue")
    def post_queue(body: dict):
        jobs = body.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise HTTPException(status_code=400, detail="'jobs' manquant")
        if len(jobs) > 100:
            raise HTTPException(status_code=400, detail="100 jobs maximum")
        return _q_add(jobs, body.get("group") if isinstance(body.get("group"), dict) else None)

    @app.post("/mobile/api/queue/action")
    def post_queue_action(body: dict):
        return _q_action(str(body.get("action", "")), body.get("id"))

    # --- presets
    @app.get("/mobile/api/presets")
    def get_presets():
        return {"presets": _read_presets()}

    @app.post("/mobile/api/presets")
    def post_preset(body: dict):
        if not isinstance(body.get("state"), dict):
            raise HTTPException(status_code=400, detail="'state' manquant")
        return {"presets": _save_preset(body.get("name", ""), body["state"])}

    @app.post("/mobile/api/presets/delete")
    def delete_preset(body: dict):
        return {"presets": _delete_preset(str(body.get("name", "")))}

    # --- tags
    @app.get("/mobile/api/tags")
    def get_tags(q: str = "", limit: int = 12):
        return {"tags": _tag_search(q, max(1, min(limit, 30)))}

    # --- corbeille
    @app.post("/mobile/api/trash")
    def post_trash(body: dict):
        paths = body.get("paths") or []
        if not isinstance(paths, list):
            raise HTTPException(status_code=400, detail="'paths' doit être une liste")
        return {"moved": _move_to_trash([str(p) for p in paths[:2000]])}

    @app.post("/mobile/api/trash/restore")
    def post_restore(body: dict):
        items = body.get("items") or []
        return {"restored": _restore(items if isinstance(items, list) else [])}

    @app.get("/mobile/api/trash")
    def get_trash():
        return _trash_info()

    @app.get("/mobile/api/cleanup")
    def get_cleanup():
        dupes, dsize = _find_duplicates()
        grids, gsize = _list_grids()
        return {"dupes": {"count": len(dupes), "bytes": dsize}, "grids": {"count": len(grids), "bytes": gsize},
                "trash": _trash_info()}

    @app.post("/mobile/api/cleanup")
    def post_cleanup(body: dict):
        what = body.get("what")
        if what == "dupes":
            files = _find_duplicates()[0]
        elif what == "grids":
            files = _list_grids()[0]
        else:
            raise HTTPException(status_code=400, detail="'what' = dupes ou grids")
        return {"moved": _move_to_trash(files)}

    # --- TIPO
    @app.get("/mobile/api/tipo/status")
    def tipo_status():
        return {"available": _tipo_available(), "install": _tipo["install"], "state": _tipo["state"], "error": _tipo["error"]}

    @app.post("/mobile/api/tipo/install")
    def tipo_install():
        if _tipo["install"] != "running":
            threading.Thread(target=_tipo_install, daemon=True).start()
        return {"ok": True}

    @app.post("/mobile/api/tipo/suggest")
    def tipo_suggest(body: dict):
        return _tipo_suggest(str(body.get("prompt", "")), str(body.get("describe", "")),
                             str(body.get("length", "short")), int(body.get("seed") or 0))

    # --- diagnostic : où en est chaque thread de Forge (en cas de blocage)
    @app.get("/mobile/api/debug/threads")
    def debug_threads():
        import sys

        names = {t.ident: t.name for t in threading.enumerate()}
        out = []
        for tid, frame in sys._current_frames().items():
            stack = traceback.format_stack(frame)[-14:]
            out.append(f"--- {names.get(tid, tid)}\n" + "".join(stack))
        try:
            st = shared.state
            head = f"job={st.job} interrupted={st.interrupted} step={st.sampling_step}/{st.sampling_steps} job_no={st.job_no}/{st.job_count}\n"
        except Exception:
            head = ""
        return Response(head + "\n".join(out), media_type="text/plain; charset=utf-8")

    # --- serveur
    @app.get("/mobile/api/server")
    def get_server():
        free, total = _vram()
        return {"busy": _busy(), "vram_free": free, "vram_total": total, "started": _STARTED}

    @app.post("/mobile/api/server")
    def post_server(body: dict):
        action = body.get("action")
        if action == "free":
            if _busy():
                raise HTTPException(status_code=409, detail="Génération en cours")
            return {"ok": True, "vram_free": _free_vram()}
        if action == "restart":
            _restart_later()
            return {"ok": True}
        raise HTTPException(status_code=400, detail="action = free ou restart")

    # --- réglages / notifications
    def public_settings(s):
        out = {k: v for k, v in s.items() if k != "civitai_key"}
        out["civitai_key_set"] = bool(str(s.get("civitai_key", "")).strip())
        return out

    @app.get("/mobile/api/settings")
    def get_settings():
        return public_settings(_read_settings())

    @app.post("/mobile/api/settings")
    def post_settings(body: dict):
        s = _read_settings()
        for k, v in body.items():
            if k not in DEFAULT_SETTINGS or not isinstance(v, str):
                continue
            if k == "civitai_key" and not v.strip():
                continue  # champ vide = on garde la clé existante
            s[k] = "" if (k == "civitai_key" and v.strip() == "-") else v.strip()
        _write_json(SETTINGS_FILE, s)
        return public_settings(s)

    # --- Civitai
    @app.get("/mobile/api/civitai/search")
    def civitai_search(q: str = "", base: str = "Illustrious", nsfw: int = 1, sort: str = "Most Downloaded", cursor: str = ""):
        return _civitai_search(q.strip(), base, bool(nsfw), sort, cursor)

    @app.post("/mobile/api/civitai/download")
    def civitai_download(body: dict):
        return _civitai_download(body)

    @app.get("/mobile/api/civitai/downloads")
    def civitai_downloads():
        with _dl_lock:
            return {"downloads": sorted(_downloads.values(), key=lambda d: -d["started"])[:20]}

    @app.post("/mobile/api/notify/test")
    def post_notify_test():
        ok, msg = _ntfy("Forge · test", "Les notifications fonctionnent.", ["bell"], sync=True)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True}

    @app.get("/mobile/api/state")
    def get_state():
        return JSONResponse(_read_state(), headers={"Cache-Control": "no-store"})

    @app.post("/mobile/api/state")
    def post_state(body: dict):
        state = body.get("state")
        if not isinstance(state, dict):
            raise HTTPException(status_code=400, detail="'state' manquant")
        return _write_state(state)

    @app.post("/mobile/api/model")
    def post_model(body: dict):
        title = body.get("title")
        if not title:
            raise HTTPException(status_code=400, detail="'title' manquant")
        try:
            from modules_forge import main_entry
            main_entry.checkpoint_change(title)  # Forge : met à jour les paramètres de chargement
        except ImportError:
            shared.opts.set("sd_model_checkpoint", title)
            shared.opts.save(shared.config_filename)
        _log(f"modèle sélectionné : {title}")
        return {"ok": True, "title": shared.opts.sd_model_checkpoint}

    @app.get("/mobile/api/models")
    def get_models():
        """Liste des checkpoints sans passer par /sdapi/v1/sd-models (qui peut planter chez Forge)."""
        items = []
        try:
            from modules import sd_models
            for x in list(sd_models.checkpoints_list.values()):
                try:
                    items.append({"title": x.title, "model_name": x.model_name})
                except Exception:
                    _log("modèle illisible\n" + traceback.format_exc())
        except Exception:
            _log("liste des modèles : erreur\n" + traceback.format_exc())
        if not items:  # secours : les fichiers du dossier
            try:
                from modules.paths import models_path
                root = Path(getattr(shared.cmd_opts, "ckpt_dir", None) or Path(models_path) / "Stable-diffusion")
                for full in _walk_files(root, {".safetensors", ".ckpt", ".gguf"}):
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    items.append({"title": rel, "model_name": os.path.splitext(os.path.basename(rel))[0]})
            except Exception:
                _log("liste des modèles (dossier) : erreur\n" + traceback.format_exc())
        items.sort(key=lambda m: m["model_name"].lower())
        return {"models": items, "current": getattr(shared.opts, "sd_model_checkpoint", "") or ""}

    @app.get("/mobile/api/taglib")
    def get_taglib():
        return _taglib()

    @app.post("/mobile/api/tagcat")
    def post_tagcat(body: dict):
        tags = [str(t)[:80].lower() for t in (body.get("tags") or [])[:400]]
        t = _load_tags()
        dan = t.get("dan")
        if dan is None:
            dan = t["dan"] = {r[0]: r[1] for r in t["rows"]}
        lib = _taglib_cache.get("data") or {}
        persos = {x["tag"] for c in lib.get("cats", []) if c["id"] == "persos" for x in c["tags"]}
        out = {}
        for tag in tags:
            if tag in persos:
                out[tag] = "persos"
            else:
                out[tag] = "qualite" if tag in QUALITY_TAGS else (_tag_category(tag, dan) or "autres")
        return {"cats": out}

    @app.post("/mobile/api/lora/upload")
    async def upload_lora(request: Request, name: str, overwrite: int = 0):
        fname = os.path.basename(name.replace("\\", "/"))
        if os.path.splitext(fname)[1].lower() not in LORA_EXT or fname.startswith("."):
            raise HTTPException(status_code=400, detail="Fichier .safetensors attendu")
        root = _lora_root()
        root.mkdir(parents=True, exist_ok=True)
        dest = root / fname
        if dest.exists() and not overwrite:
            raise HTTPException(status_code=409, detail="Ce LoRA existe déjà")
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    f.write(chunk)
                    size += len(chunk)
            if size < 1024:
                raise HTTPException(status_code=400, detail="Fichier vide ou incomplet")
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        refreshed = _refresh_forge_loras()
        _log(f"LoRA importé : {fname} ({size // (1024 * 1024)} Mo), Forge rafraîchi : {refreshed}")
        return {"ok": True, "name": os.path.splitext(fname)[0], "bytes": size}

    @app.get("/mobile/api/loras")
    def get_loras():
        return {"root": str(_lora_root()), "loras": _list_loras()}

    @app.get("/mobile/api/recent")
    def get_recent(limit: int = 24):
        return {"images": _recent(max(1, min(limit, 100)))}

    @app.get("/mobile/api/thumb")
    def get_thumb(path: str):
        return FileResponse(_thumbnail(_safe_image(path)), headers={"Cache-Control": "max-age=604800"})

    @app.get("/mobile/api/preview")
    def get_preview(path: str):
        return FileResponse(_cached_resize(_safe_image(path), "preview"), headers={"Cache-Control": "max-age=604800"})

    @app.get("/mobile/api/jpg")
    def get_jpg(path: str, q: int = 88):
        """Export JPG compressé à la volée (pleine résolution, sans métadonnées), rien n'est écrit sur le disque."""
        from PIL import Image

        real = _safe_image(path)
        buf = io.BytesIO()
        with Image.open(real) as img:
            img.convert("RGB").save(buf, "JPEG", quality=max(40, min(int(q), 95)), optimize=True, progressive=True)
        name = urllib.parse.quote(real.stem + ".jpg")
        return Response(buf.getvalue(), media_type="image/jpeg",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    @app.get("/mobile/api/image")
    def get_image(path: str):
        return FileResponse(_safe_image(path))

    @app.get("/mobile/api/gallery")
    def get_gallery(q: str = "", lora: str = "", model: str = "", tags: str = "", fav: int = 0,
                    sort: str = "new", offset: int = 0, limit: int = 60, day: str = ""):
        return _gallery(q, lora, model, tags, fav, sort, max(0, offset), max(1, min(limit, 240)), day)

    @app.get("/mobile/api/record")
    def get_record(path: str):
        real = str(_safe_image(path))
        index = _refresh_index()
        rec = index.get(real) or next((r for p, r in index.items() if os.path.normcase(p) == os.path.normcase(real)), None)
        if rec is None:
            rec = _index_record(real, os.path.getmtime(real))
        return dict(rec, fav=rec["path"] in _read_favs(), day=_day(rec["mtime"]))

    @app.post("/mobile/api/fav")
    def post_fav(body: dict):
        path = str(_safe_image(body.get("path", "")))
        favs = _read_favs()
        if body.get("fav"):
            favs.add(path)
        else:
            favs.discard(path)
        _write_favs(favs)
        return {"path": path, "fav": path in favs}

    @app.get("/mobile/api/info")
    def get_info(path: str):
        return {"parameters": _parameters(_safe_image(path))}


script_callbacks.on_app_started(on_app_started)
_log("callback app_started enregistré")
