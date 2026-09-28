# Pollinations for Krita

Generate and edit images with [Pollinations](https://pollinations.ai) inside Krita, paying with
your own Pollen. Submitted for Pollinations quest #15571.

A Krita Python plug-in (a dock) — standard library only on the API side, mirroring the request
and error-handling shapes of the official Pollinations GIMP plug-in.

## Features

- **Generate to a new layer** — pick any image model from the live `/image/models` catalog
  (filtered to image-output models exactly like the GIMP plug-in), write a prompt, and the image
  is placed on a new paint layer at the selection (or over the whole canvas)
- **Edit the active layer / selection** — the layer/selection bounds are read as PNG, sent to
  `/v1/images/edits` with a `data:image/png;base64` body (the same shape the GIMP plug-in sends),
  and the result is added as a new layer above; the source layer is never modified. Only
  image-input-capable models are offered
- **Player-funded sign-in** — BYOP device flow: paste your publishable `pk_` App Key, approve in
  your browser (`authorization_pending` arrives as HTTP 400 and is handled as flow state),
  `slow_down` backs off, `access_denied`/`expired_token` get clear messages. The token is stored
  privately: owner-only file on POSIX, DPAPI on Windows — never inside your document.
  A development API key via the `POLLINATIONS_API_KEY` environment variable is also supported
- **Actionable errors** — expired sign-in, low Pollen balance (top-up hint), model permissions,
  rate limits, and network failures all appear in the dock status line

## Install

1. Copy (or clone) this repo into Krita's Python plug-in folder:
   - Linux: `~/.local/share/krita/pykrita/`
   - Windows: `%APPDATA%\krita\pykrita\`
   - macOS: `~/Library/Application Support/krita/pykrita/`
2. Restart Krita, then enable **Pollinations** under
   Settings → Configure Krita → Python Plugin Manager.
3. Open the **Pollinations** dock (Settings → Dockers → Pollinations).
4. Click **Connect**, paste your `pk_` App Key, approve in the browser — or set
   `POLLINATIONS_API_KEY` before starting Krita.

## Short demo

1. Open an 8-bit RGBA document, select a rectangle.
2. In the dock: model `tongyi-mai/z-image-turbo`, prompt “a tiny pixel-art garden on an alien
   planet” → **Generate to a new layer**. A new paint layer appears with the garden, placed at
   the selection.
3. Make the new layer active, pick `openai/gpt-image-2` in the edit combo, prompt “make it a
   night scene” → **Edit onto a new layer**. The edited version lands on a layer above, source
   untouched.
4. **Disconnect** clears the stored token; the status line confirms.

## Verification

```bash
python3 -m unittest discover -s tests   # 16 unit tests: API layer + token store
python3 tests/live.py                   # live catalog + generation + edit through the same api.py
```

The unit suite covers request header/token building, the device-flow state machine (400 pending,
slow_down back-off, denied, expiry, approval-URL host check, `pk_` validation), model-catalog
filtering, the generations/edits route split with the PNG data-URI body, resolution validation,
b64 result decoding with the empty-image guard, error mapping (401 → AuthError, 402 → Pollen
top-up hint, 403/429/500), and the token store round trip with 0600 permissions on POSIX.

The live suite (with `POLLINATIONS_API_KEY`) verified against the real API: the filtered catalog
loads (18 image models), a 149 KB JPEG generated and decoded, and `/v1/images/edits` returned a
207 KB edited image for a real PNG source — all through the plug-in's own code. The device-flow
endpoints were live-probed separately (real device code issued; pending poll returns HTTP 400 as
documented); a full end-to-end sign-in needs a registered `pk_` App Key.

The dock code (plugin.py) targets Krita 5.x with PyQt5 and compiles cleanly; running it inside
the actual Krita needs a Krita environment.

## License

MIT
