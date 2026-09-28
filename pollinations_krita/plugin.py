"""Pollinations dock for Krita: generate onto a new layer, edit the active
layer or selection onto a new layer above it, connect with the BYOP device
flow. The token is stored privately via token_store (owner-only file on POSIX,
DPAPI on Windows) and never inside the document."""

import os
import threading

try:
    from krita import Krita, DockWidget
    from PyQt5.QtCore import QBuffer, QIODevice, QTimer
    from PyQt5.QtGui import QImage, QDesktopServices
    from PyQt5.QtCore import QUrl
    from PyQt5.QtWidgets import (
        QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QTextEdit, QVBoxLayout, QWidget,
    )
except ImportError:  # allow the module to be imported outside Krita for tests
    Krita = None
    DockWidget = object

from .api import ApiError, AuthError, Cancelled, begin_authorization, generate, load_models, poll_authorization
from .token_store import TokenStore


class AppKeyDialog(QDialog if Krita else object):
    """Asks for the publishable pk_ App Key used for the device flow."""

    def __init__(self, parent=None):
        if Krita is None:
            raise RuntimeError("Not running inside Krita.")
        QDialog.__init__(self, parent)
        self.setWindowTitle("Connect Pollinations")
        self.setLayout(QVBoxLayout())
        self.layout().addWidget(QLabel(
            "Paste your publishable App Key (starts with pk_) from\n"
            "enter.pollinations.ai. You will approve the sign-in in your\n"
            "browser; generation is paid with your own Pollen."))
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("pk_…")
        self.layout().addWidget(self.key_input)
        buttons = QWidget()
        buttons.setLayout(QHBoxLayout())
        start = QPushButton("Start sign-in")
        start.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.layout().addWidget(start)
        buttons.layout().addWidget(cancel)
        self.layout().addWidget(buttons)
        self.app_key = ""
        start.clicked.connect(self._capture)

    def _capture(self):
        self.app_key = self.key_input.text().strip()


class PollinationsDock(DockWidget):
    def __init__(self):
        DockWidget.__init__(self)
        self.setWindowTitle("Pollinations")
        self.token_store = TokenStore()
        self.models = []
        self.thread_result = {}
        self.working = False
        self._build_ui()
        QTimer.singleShot(0, self.startup)

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        panel = QWidget(self)
        panel.setLayout(QVBoxLayout())
        self.setWidget(panel)

        self.status = QLabel("Loading…")
        panel.layout().addWidget(self.status)

        connect_row = QWidget()
        connect_row.setLayout(QHBoxLayout())
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_account)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.disconnect_account)
        connect_row.layout().addWidget(self.connect_button)
        connect_row.layout().addWidget(self.disconnect_button)
        panel.layout().addWidget(connect_row)

        models_row = QWidget()
        models_row.setLayout(QHBoxLayout())
        self.model_combo = QComboBox()
        reload_models = QPushButton("Reload models")
        reload_models.clicked.connect(lambda: self.reload_models())
        models_row.layout().addWidget(self.model_combo, stretch=1)
        models_row.layout().addWidget(reload_models)
        panel.layout().addWidget(models_row)

        panel.layout().addWidget(QLabel("Prompt"))
        self.prompt = QTextEdit()
        panel.layout().addWidget(self.prompt)

        self.generate_button = QPushButton("Generate to a new layer")
        self.generate_button.clicked.connect(lambda: self.run_job(self.job_generate))
        panel.layout().addWidget(self.generate_button)

        panel.layout().addWidget(QLabel("Edit the active layer / selection"))
        self.edit_model_combo = QComboBox()
        panel.layout().addWidget(self.edit_model_combo)
        self.edit_button = QPushButton("Edit onto a new layer")
        self.edit_button.clicked.connect(lambda: self.run_job(self.job_edit))
        panel.layout().addWidget(self.edit_button)

    def startup(self):
        self._refresh_connection()
        self.reload_models()

    def token(self):
        return self.token_store.load() or os.environ.get("POLLINATIONS_API_KEY", "")

    def _refresh_connection(self):
        connected = bool(self.token_store.load())
        self.disconnect_button.setEnabled(connected)
        self.disconnect_button.setToolTip("Stored connection: " + ("device flow token" if connected else "none"))

    def _set_busy(self, busy, message=""):
        self.working = busy
        for button in (self.generate_button, self.edit_button, self.connect_button):
            button.setEnabled(not busy)
        self.status.setText(message)

    # ------------------------------------------------------------- jobs

    def run_job(self, job):
        if self.working:
            return
        doc = Krita.instance().activeDocument()
        if doc is None:
            self.status.setText("Open a document first.")
            return
        self._set_busy(True, "Talking to Pollinations…")

        def run():
            try:
                result = job(doc)
                self.thread_result = {"ok": True, "message": result}
            except (ApiError, AuthError) as error:
                self.thread_result = {"ok": False, "message": str(error)}
            except Exception as error:  # noqa: BLE001 - surface anything unexpected
                self.thread_result = {"ok": False, "message": "Unexpected error: " + str(error)}

        worker = threading.Thread(target=run)
        worker.daemon = True
        self.thread_result = {}
        worker.start()
        QTimer.singleShot(250, lambda: self._watch_job(worker))

    def _watch_job(self, worker):
        if worker.is_alive():
            QTimer.singleShot(250, lambda: self._watch_job(worker))
            return
        ok = self.thread_result.get("ok")
        message = self.thread_result.get("message", "")
        self._set_busy(False, message if message else ("Done." if ok else "Failed."))
        if ok:
            self._refresh_connection()

    def _selected_model(self, combo):
        name = combo.currentText()
        for model in self.models:
            if model.get("name") == name:
                return model
        return None

    def job_generate(self, doc):
        model = self._selected_model(self.model_combo)
        if model is None:
            raise ApiError("Choose a model first.")
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            raise ApiError("Enter a prompt.")
        bounds = self._generation_bounds(doc)
        data = generate(self.token(), model, prompt)
        layer_name = "pollinations: " + prompt[:32]
        self._write_png_to_new_layer(doc, layer_name, data, bounds["x"], bounds["y"])
        return "Generated onto a new layer."

    def job_edit(self, doc):
        model = self._selected_model(self.edit_model_combo)
        if model is None:
            raise ApiError("Choose an edit model first.")
        if "image" not in model.get("input_modalities", []):
            raise ApiError("Choose a model that supports image input.")
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            raise ApiError("Enter what should change in the edit prompt.")
        node = doc.activeNode()
        if node is None or not hasattr(node, "pixelData"):
            raise ApiError("Select a paint layer to edit first.")
        bounds = self._generation_bounds(doc)
        source_png = self._read_layer_png(doc, node, bounds)
        data = generate(self.token(), model, prompt, source=source_png)
        layer_name = "pollinations edit: " + prompt[:28]
        self._write_png_to_new_layer(doc, layer_name, data, bounds["x"], bounds["y"])
        return "Edited image added as a new layer."

    # ------------------------------------------------------- layer helpers

    def _generation_bounds(self, doc):
        selection = doc.selection()
        if selection is not None:
            box = selection.bounds()
            return {"x": box.x(), "y": box.y(), "w": box.width(), "h": box.height()}
        return {"x": 0, "y": 0, "w": doc.width(), "h": doc.height()}

    def _require_rgba8(self, doc):
        if doc.colorSpaceModel() != "RGBA" or doc.colorDepth() != "U8":
            raise ApiError("Set the document to 8-bit RGBA (Image → Properties) to use Pollinations layers.")

    def _write_png_to_new_layer(self, doc, name, image_bytes, x, y):
        self._require_rgba8(doc)
        image = QImage()
        if not image.loadFromData(image_bytes):
            raise ApiError("The generated image could not be decoded.")
        image = image.convertToFormat(QImage.Format_ARGB32)
        layer = doc.createNode(name, "paintLayer")
        doc.rootNode().addChildNode(layer, None)
        bits = image.constBits()
        bits.setsize(image.byteCount())
        layer.setPixelData(bytes(bits), x, y, image.width(), image.height())
        doc.refreshHistoryProjection()

    def _read_layer_png(self, doc, node, bounds):
        self._require_rgba8(doc)
        data = bytes(node.pixelData(bounds["x"], bounds["y"], bounds["w"], bounds["h"]))
        image = QImage(data, bounds["w"], bounds["h"], bounds["w"] * 4, QImage.Format_ARGB32)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    # ------------------------------------------------------------- account

    def reload_models(self):
        def run():
            try:
                self.thread_result = {"ok": True, "models": load_models(self.token())}
            except (ApiError, AuthError) as error:
                self.thread_result = {"ok": False, "message": str(error)}
            except Exception as error:  # noqa: BLE001
                self.thread_result = {"ok": False, "message": "Unexpected error: " + str(error)}

        worker = threading.Thread(target=run)
        worker.daemon = True
        self.thread_result = {}
        self.status.setText("Loading the live model catalog…")
        worker.start()
        QTimer.singleShot(150, lambda: self._watch_models(worker))

    def _watch_models(self, worker):
        if worker.is_alive():
            QTimer.singleShot(150, lambda: self._watch_models(worker))
            return
        result = self.thread_result
        if result.get("ok"):
            self.models = result.get("models", [])
            self.model_combo.clear()
            self.edit_model_combo.clear()
            for model in self.models:
                self.model_combo.addItem(model["name"])
                if "image" in model.get("input_modalities", []):
                    self.edit_model_combo.addItem(model["name"])
            self.status.setText(
                f"{len(self.models)} image models; {self.edit_model_combo.count()} support image input.")
        else:
            self.status.setText(result.get("message", "Could not load models."))

    def connect_account(self):
        dialog = AppKeyDialog(self)
        if not dialog.exec_():
            return
        app_key = dialog.app_key
        self._set_busy(True, "Starting device sign-in…")

        def run():
            cancel = threading.Event()
            self._cancel = cancel
            try:
                code = begin_authorization(app_key)
                QDesktopServices.openUrl(QUrl(code["approval_url"]))
                self.thread_result = {"ok": False, "message": ""}
                self.thread_result = {"message_label": "Your code: " + code["user_code"] + " - approve it in the browser."}
                token = poll_authorization(code, cancel)
                self.token_store.save(token)
                self.thread_result = {"ok": True, "message": "Connected with your own Pollen."}
            except (ApiError, AuthError) as error:
                self.thread_result = {"ok": False, "message": str(error)}
            except Cancelled:
                self.thread_result = {"ok": False, "message": "Connection cancelled."}
            except Exception as error:  # noqa: BLE001
                self.thread_result = {"ok": False, "message": "Unexpected error: " + str(error)}

        worker = threading.Thread(target=run)
        worker.daemon = True
        self.thread_result = {}
        worker.start()
        QTimer.singleShot(250, lambda: self._watch_connect(worker))

    def _watch_connect(self, worker):
        if worker.is_alive():
            QTimer.singleShot(250, lambda: self._watch_connect(worker))
            return
        if not self.thread_result.get("ok") and self.thread_result.get("message_label"):
            self.status.setText(self.thread_result["message_label"])
        ok = self.thread_result.get("ok")
        message = self.thread_result.get("message", "")
        self._set_busy(False, message if message else ("Connected." if ok else "Failed."))
        if ok:
            self._refresh_connection()
            self.reload_models()

    def disconnect_account(self):
        self.token_store.clear()
        self._refresh_connection()
        self.status.setText("Disconnected. Generation now uses POLLINATIONS_API_KEY if set.")
