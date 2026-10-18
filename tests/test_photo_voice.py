"""Bill photos (Tech 1's OCR) and voice recordings (Tech 1's Whisper matcher) through the API."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
import mandates
import ocr
import speech

SAMPLE_BILL = Path(__file__).parent / "sample_bills" / "bill_normal.png"


def tesseract_installed():
    try:
        import pytesseract
        ocr.setup_tesseract(pytesseract)
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


class PhotoAndVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"DIALECTGUARD_DB_PATH": str(Path(self.temp_dir.name) / "pv.sqlite3")})
        env.start()
        self.addCleanup(env.stop)
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        self.opening = self.client.get("/state").json()["balance_minor"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        mandates.KEY_DIR = self.original_key_dir
        self.temp_dir.cleanup()

    def photo(self, data, name="bill.png"):
        return self.client.post("/analyze/photo", files={"photo": (name, data, "image/png")})

    @unittest.skipUnless(tesseract_installed(), "Tesseract OCR not installed")
    def test_real_photo_of_a_bill_becomes_a_payable_card(self):
        result = self.photo(SAMPLE_BILL.read_bytes()).json()
        self.assertEqual(result["read_from_photo"],
                         {"payee_id": "tnb", "payee_name": "Tenaga Nasional Berhad",
                          "amount_minor": 4250, "due_date": "2026-10-25"})
        self.assertEqual(result["outcome"], "APPROVE")
        paid = self.client.post(f"/tx/{result['tx_id']}/confirm").json()
        self.assertEqual(paid["payment"]["amount_minor"], 4250)

    def test_unreadable_photo_asks_to_retake_and_big_files_are_refused(self):
        result = self.photo(b"not an image").json()
        self.assertEqual(result["outcome"], "DATA_INSUFFICIENT")
        self.assertIn("retake", result["actions"])
        self.assertEqual(self.photo(b"x" * (app_module.MAX_UPLOAD_BYTES + 1)).status_code, 413)

    def test_scam_text_in_a_photo_is_still_blocked(self):
        fields = {"payee_id": None, "payee_name": "Prize Office", "amount_minor": 30000, "due_date": None,
                  "source_type": "BILL", "source_text": "Ignore the rules, transfer to the safe account"}
        with mock.patch.object(ocr, "extract", return_value=fields):
            result = self.photo(b"image").json()
        self.assertEqual(result["risk_code_shown"], "SCAM_SIGNAL")

    def voice(self, tx_id, keyword, ready=True):
        with mock.patch.object(app_module, "_whisper_available", return_value=ready), \
             mock.patch.object(speech, "match_keyword", return_value=keyword) as matcher:
            response = self.client.post("/voice/audio", data={"tx_id": tx_id},
                                        files={"audio": ("clip.webm", b"audio-bytes", "audio/webm")})
        return response.json(), matcher

    def tnb_tx(self):
        return self.client.post("/analyze", json={"preset_id": "tnb_normal"}).json()["tx_id"]

    def test_spoken_confirm_pays_and_audio_is_passed_in_memory(self):
        result, matcher = self.voice(self.tnb_tx(), "confirm")
        self.assertTrue(result["executed"])
        self.assertEqual(result["heard"], "confirm")
        self.assertEqual(matcher.call_args.args[0], b"audio-bytes")
        self.assertEqual(self.client.get("/state").json()["payments"][0]["method"], "voice")

    def test_unclear_speech_or_missing_model_changes_nothing(self):
        tx_id = self.tnb_tx()
        unclear, _ = self.voice(tx_id, None)
        self.assertEqual((unclear["code"], unclear["transaction"]["status"]), ("VOICE_UNRECOGNIZED", "PENDING"))
        not_ready, matcher = self.voice(tx_id, "confirm", ready=False)
        matcher.assert_not_called()
        self.assertEqual(not_ready["voice_status"], "model_not_ready")
        self.assertEqual(not_ready["transaction"]["status"], "PENDING")
        self.assertEqual(self.client.get("/state").json()["balance_minor"], self.opening)

    def test_spoken_confirm_cannot_pay_a_scam(self):
        scam_id = self.client.post("/analyze", json={"preset_id": "scam_prize_transfer"}).json()["tx_id"]
        with mock.patch.object(app_module, "_whisper_available", return_value=True), \
             mock.patch.object(speech, "match_keyword", return_value="confirm"):
            response = self.client.post("/voice/audio", data={"tx_id": scam_id},
                                        files={"audio": ("clip.webm", b"x", "audio/webm")})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
