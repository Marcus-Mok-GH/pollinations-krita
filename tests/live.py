"""Live verification: the plug-in's own api.py against the real Pollinations
API. Uses POLLINATIONS_API_KEY when present; otherwise exits with an explicit
skip. Run: python3 tests/live.py"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pollinations_krita import api

KEY = os.environ.get("POLLINATIONS_API_KEY", "")


def check(condition, name):
    print(("[ok] " if condition else "[FAIL] ") + name)
    return 0 if condition else 1


def main():
    if not KEY:
        print("[skip] live checks (no POLLINATIONS_API_KEY in the environment)")
        return
    failures = 0

    models = api.load_models(KEY)
    failures += check(len(models) > 10, f"live image model catalog loads and filters ({len(models)} image models)")

    generated = api.generate(KEY, {"name": "tongyi-mai/z-image-turbo", "input_modalities": ["text"]},
                             "a tiny pixel-art garden on an alien planet")
    failures += check(len(generated) > 10000 and generated[:3] == b"\xff\xd8\xff",
                      f"live generation decoded a {len(generated)}-byte JPEG via b64_json")

    source = Path(__file__).parent / "assets" / "source.png"
    model = next((m for m in models if m["name"] == "openai/gpt-image-2"), None)
    if model is None:
        print("[skip] live edit (openai/gpt-image-2 not in the account's catalog)")
    else:
        try:
            edited = api.generate(KEY, model, "replace the background with a night sky", source=source.read_bytes())
            failures += check(len(edited) > 1000, f"live edit via /v1/images/edits decoded a {len(edited)}-byte image")
        except api.ApiError as error:
            message = str(error)
            if "Pollen" in message or "budget" in message:
                print("[skip] live edit (account balance does not cover this edit model)")
            else:
                failures += check(False, "live edit failed: " + message)

    print("RESULT: " + ("PASS" if failures == 0 else "FAIL") + f" ({failures} failures)")
    sys.exit(1 if failures else 0)


main()
