# CLAUDE.md — Forge Mobile

Notes for Claude (or any contributor) picking up this project in a new session.

## Owner & working style

- Owner: Pierre (GitHub `PierreOnGit`), French, writes in a FR/EN mix. Answer **in French, concise and direct**.
- He tests on his own PC; prefer shipping a working change + a one-line "what to do" (reload page / restart Forge) over long explanations.
- **Privacy rule (strict):** never open or view the owner's generated images and never print the contents of his prompts. Work on metadata only (file names, sizes, dates, counts, settings). Screenshots for docs use fake demo images and neutral LoRA names.

## What it is

A Forge extension serving a liquid-glass web UI at `/mobile/` (desktop + phone). Phone reaches the PC through Tailscale.
Target setup: Forge `f2.0.1v1.10.1` (portable pack `webui_forge_cu121_torch231`, Python 3.10, torch 2.3.1, Gradio 4.40), started with `--api`; SDXL / Illustrious models; RTX 4070 Laptop **8 GB VRAM**.

## Layout

| File | Role |
|---|---|
| `scripts/forge_mobile.py` | Server (~2k lines). Registers FastAPI routes in `script_callbacks.on_app_started`. |
| `mobile.html` | Whole UI in one file (~3k lines): HTML + CSS + vanilla JS, no build step. |
| `sw.js`, `assets/` | PWA service worker and icons. |
| `tags/danbooru.csv` | Autocomplete tag list (from a1111-sd-webui-tagcomplete, MIT). |
| `docs/*.jpg` | README screenshots (English UI, fake images). |

Runtime files written next to the extension (all git-ignored): `shared_state.json`, `presets.json`, `settings.json`, `queue_history.json`, `gallery_index.json`, `favorites.json`, `lora_info.json`, `forge_mobile.log`, `thumbs/`.

## Server — key mechanics

- **Queue**: a worker thread POSTs to `http://127.0.0.1:{port}/sdapi/v1/txt2img` with `save_images=True, do_not_save_grid=True` and `override_settings.export_for_4chan=False` (no auto JPG copies). History in `queue_history.json`; pending jobs survive restarts (the running one does not).
- **Tiled VAE**: Forge's built-in *Never OOM* script via `alwayson_scripts: {"Never OOM Integrated": {"args": [false, tiled]}}` (`hires.tiled`, default on). VAE "freezes" on 8 GB were NVIDIA sysmem fallback → advise *CUDA Sysmem Fallback Policy = Prefer No Sysmem Fallback* for Forge's `system\python\python.exe` only.
- **Models**: `/mobile/api/models` reads files directly (Forge's `/sdapi/v1/sd-models` is broken in this build). Switch via `modules_forge.main_entry.checkpoint_change`.
- **Restart**: `modules.restart.restart_program` (relies on webui.bat restart loop).
- **Gallery**: indexes PNG metadata (`gallery_index.json`); thumbnails 384 px WebP / previews 1600 px WebP cached in `thumbs/`, warmed by a background thread; `/mobile/api/jpg` = on-the-fly compressed JPG download (no metadata, nothing written). Trash = `outputs/_corbeille` with restore.
- **TIPO** (prompt suggestions): `tipo-kgen` installed from the UI with `sys.executable -m pip install tipo-kgen --no-deps`; model `KBlueLeaf/TIPO-500M-ft` loaded with transformers in fp32 on **CPU**. kgen bug: `post_generate_process` truncates against `TARGET_TIPO_NL_MAX` → we monkeypatch `TARGET_TIPO_MAX` / `TARGET_TIPO_NL_MAX` to 1000 and cap ourselves (light→6 using "long", medium→12 / generous→24 using "very_long"). UI: ✨ Enrich = extra tags as chips (never rewrites the prompt), 💬 Describe = sentence → tags.
- **Civitai**: search `/api/v1/models?types=LORA…`, download `/api/download/models/{versionId}?token=` (key stored in `settings.json`, exposed to the UI only as `civitai_key_set`). Trigger words saved to `lora_info.json`.
- **Debug**: `/mobile/api/debug/threads` dumps every thread stack — first thing to read when Forge "hangs".
- **No auth** on `/mobile/*` (same as Forge API). Never suggest exposing it to the internet.

## UI — key mechanics

- Shared state synced between devices through `/mobile/api/state`.
- **i18n**: French source strings; client-side `EN` dictionary + `EN_PAT` regexes, `tr()`, MutationObserver translating text nodes and `placeholder/title/aria-label`. Areas excluded via `I18N_SKIP`. Composite strings are split on ` · `. Language selector `#segLang` (auto/fr/en, localStorage `forgeMobileLang`). **Every new French string needs an `EN` entry.**
- Desktop: wide gallery mode, draggable floating dock with magnet (Generate / Resume / Pause), paginated gallery with internal scroll.
- Mobile: Studio / Gallery tabs (`body.gwide`), photo mode (swipe, pinch & double-tap zoom, swipe-down close, `history.pushState` so Android back closes it).
- Prompt editor: colored overlay (`hlRender`), autocomplete (Enter/Tab pick), prompt checker, tag library built from image metadata.

## Dev & test workflow

1. Edit, then syntax-check: extract `<script>` blocks and `node --check`; `python3 -W error -m py_compile scripts/forge_mobile.py`.
2. Test against a fake Forge (stub `modules`/`modules_forge` + FastAPI app mounting the extension) with Playwright — never against the owner's real data.
3. Deploy to the PC (when the session is linked to it): copy files into `<forge>\webui\extensions\sd-forge-mobile\`, verify with `md5sum`.
4. Tell the owner: HTML-only change → reload page; Python change → **⚙️ → Restart Forge**.
5. Commit with clear English messages and push to `origin main`.

## Roadmap (not started — "for later")

- **Native-ish mobile app**: Capacitor or Android TWA wrapping `mobile.html` (real push, share-to-app, APK).
- **Pairing by QR code**: ⚙️ shows a QR with `http(s)://<pc>:7860/mobile/` + a token → solves discovery *and* adds authentication.
- Auto-discovery fallback: mDNS (`_forgemobile._tcp` via `zeroconf`), then Tailscale peers, remember last working address.
- Server-side messages (ntfy, HTTP error details) are still partly French only.
