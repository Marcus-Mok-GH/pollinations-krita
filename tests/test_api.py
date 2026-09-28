"""Unit tests for the Krita plug-in's API layer and token store. Standard
library only (unittest). Run: python3 -m unittest discover -s tests"""

import base64
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pollinations_krita import api
from pollinations_krita import token_store


def response(status=200, body=b"{}", url="https://gen.pollinations.ai"):
    return mock.Mock(read=lambda: None, close=lambda: None) if False else None


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def make_urlopen(status, body):
    def fake_urlopen(request, timeout=None):
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(body))
        return FakeResponse(body)
    return fake_urlopen


class RequestTests(unittest.TestCase):
    def test_headers_and_token(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.header_items())
            captured["url"] = request.full_url
            return FakeResponse(b"{}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            api.request_json("https://gen.pollinations.ai/image/models", token="sk_t")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer sk_t")
        self.assertIn("User-agent", captured["headers"])

    def test_device_400_body_becomes_flow_state(self):
        body = json.dumps({"error": "authorization_pending"}).encode()
        with mock.patch("urllib.request.urlopen", make_urlopen(400, body)) as _:
            result = api.request_json("https://enter.pollinations.ai/api/device/token",
                                      {"device_code": "dc"})
        self.assertEqual(result, {"error": "authorization_pending"})

    def test_error_mapping(self):
        for status, expected in [(401, api.AuthError), (402, api.ApiError), (403, api.ApiError), (429, api.ApiError), (500, api.ApiError)]:
            body = json.dumps({"error": {"message": "boom"}}).encode()
            with mock.patch("urllib.request.urlopen", make_urlopen(status, body)):
                with self.assertRaises(expected) as ctx:
                    api.request_json("https://gen.pollinations.ai/v1/images/generations", {})
            if status == 402:
                self.assertIn("Pollen", str(ctx.exception))


class DeviceFlowTests(unittest.TestCase):
    def code_body(self):
        return json.dumps({
            "device_code": "dc", "user_code": "CODE-1234",
            "verification_uri": "https://enter.pollinations.ai/device",
            "verification_uri_complete": "https://enter.pollinations.ai/device?user_code=CODE-1234",
            "interval": 5, "expires_in": 600,
        }).encode()

    def test_begin_authorization_validates_pk(self):
        with self.assertRaises(api.ApiError):
            api.begin_authorization("sk_wrong")

    def test_begin_authorization_host_checks_approval_url(self):
        body = json.dumps({"device_code": "dc", "user_code": "C",
                          "verification_uri": "https://evil.example/device", "expires_in": 600}).encode()
        with mock.patch("urllib.request.urlopen", make_urlopen(200, body)):
            with self.assertRaises(api.ApiError):
                api.begin_authorization("pk_ok")

    def test_poll_grants_after_pending_and_slowdown(self):
        with mock.patch("urllib.request.urlopen", make_urlopen(200, self.code_body())):
            code = api.begin_authorization("pk_ok")
        code["deadline"] = code["deadline"]  # keep; sleeps are fake
        states = [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": "sk_granted"},
        ]

        def fake_wait(seconds):
            return False  # never cancelled

        cancel = threading.Event()
        cancel.wait = fake_wait

        def token_urlopen(request, timeout=None):
            payload = json.loads(request.data.decode())
            return FakeResponse(json.dumps(states.pop(0)).encode())

        with mock.patch("urllib.request.urlopen", token_urlopen):
            token = api.poll_authorization(code, cancel)
        self.assertEqual(token, "sk_granted")

    def test_poll_denied(self):
        with mock.patch("urllib.request.urlopen", make_urlopen(200, self.code_body())):
            code = api.begin_authorization("pk_ok")

        def token_urlopen(request, timeout=None):
            return FakeResponse(json.dumps({"error": "access_denied"}).encode())

        cancel = threading.Event()
        cancel.wait = lambda seconds: False
        with mock.patch("urllib.request.urlopen", token_urlopen):
            with self.assertRaises(api.ApiError):
                api.poll_authorization(code, cancel)


class ModelAndGenerateTests(unittest.TestCase):
    CATALOG = [
        {"name": "tongyi-mai/z-image-turbo", "input_modalities": ["text"], "output_modalities": ["image"]},
        {"name": "minimax/minimax-h3", "input_modalities": ["text"], "output_modalities": ["image", "video"]},
        {"name": "openai/gpt-image-2", "input_modalities": ["text", "image"], "output_modalities": ["image"]},
        "junk-entry",
    ]

    def test_load_models_filters_video_and_junk(self):
        with mock.patch("urllib.request.urlopen", make_urlopen(200, json.dumps(self.CATALOG).encode())):
            models = api.load_models("sk_t")
        self.assertEqual([m["name"] for m in models], ["tongyi-mai/z-image-turbo", "openai/gpt-image-2"])

    def test_generate_uses_generations_route_and_b64(self):
        png = b"\x89PNGfakebytes"
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["route"] = request.full_url
            captured["payload"] = json.loads(request.data.decode())
            return FakeResponse(json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = api.generate("sk_t", {"name": "tongyi-mai/z-image-turbo", "input_modalities": ["text"]}, "a cat")
        self.assertEqual(data, png)
        self.assertTrue(captured["route"].endswith("/v1/images/generations"))
        self.assertEqual(captured["payload"]["response_format"], "b64_json")

    def test_edit_uses_edits_route_with_png_data_uri(self):
        source = b"PNGSOURCE"
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["route"] = request.full_url
            captured["payload"] = json.loads(request.data.decode())
            return FakeResponse(json.dumps({"data": [{"b64_json": base64.b64encode(b"RESULT").decode()}]}).encode())

        model = {"name": "openai/gpt-image-2", "input_modalities": ["text", "image"]}
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = api.generate("sk_t", model, "make it red", source=source)
        self.assertEqual(data, b"RESULT")
        self.assertTrue(captured["route"].endswith("/v1/images/edits"))
        self.assertTrue(captured["payload"]["image"].startswith("data:image/png;base64,"))

    def test_edit_requires_image_input_model(self):
        with self.assertRaises(api.ApiError):
            api.generate("sk_t", {"name": "text-only", "input_modalities": ["text"]}, "x", source=b"SRC")

    def test_resolution_validation(self):
        model = {"name": "m", "input_modalities": ["text"], "resolutions": ["1k", "2k"]}
        with self.assertRaises(api.ApiError):
            api.generate("sk_t", model, "x", resolution="8k")

    def test_empty_prompt(self):
        with self.assertRaises(api.ApiError):
            api.generate("sk_t", {"name": "m", "input_modalities": ["text"]}, "   ")

    def test_invalid_result_body(self):
        with mock.patch("urllib.request.urlopen", make_urlopen(200, json.dumps({"data": [{"b64_json": ""}]}).encode())):
            with self.assertRaises(api.ApiError):
                api.generate("sk_t", {"name": "m", "input_modalities": ["text"]}, "x")


class TokenStoreTests(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = token_store.TokenStore(directory)
            store.save("sk_roundtrip")
            self.assertEqual(store.load(), "sk_roundtrip")
            if os.name == "posix":
                mode = stat.S_IMODE(os.stat(store.path).st_mode)
                self.assertEqual(mode, 0o600)
            store.clear()
            self.assertIsNone(store.load())

    def test_rejects_non_sk_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = token_store.TokenStore(directory)
            with self.assertRaises(ValueError):
                store.save("not-a-token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
