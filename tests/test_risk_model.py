"""Shadow risk model: correct features, and it can never change a decision."""

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
import mandates
import risk_model

PRESETS = ("tnb_normal", "tnb_unusually_high", "tnb_ocr_typo", "clinic_new_payee", "scam_prize_transfer")


class FeatureTests(unittest.TestCase):
    def test_features_are_scale_free_and_match_hand_calculation(self):
        self.assertEqual(
            risk_model.features(is_transfer=False, amount=4250, balance_before=85000, hour=14),
            [0.0, math.log1p(0.05), 0.0, 0.0],
        )
        # Sending the whole balance at 2am: empties it and is at night.
        self.assertEqual(
            risk_model.features(is_transfer=True, amount=500, balance_before=500, hour=2),
            [1.0, math.log1p(1.0), 1.0, 1.0],
        )
        # Same ratio at a different scale gives the same features.
        self.assertEqual(
            risk_model.features(is_transfer=True, amount=4_250_000, balance_before=85_000_000, hour=14),
            risk_model.features(is_transfer=True, amount=4250, balance_before=85000, hour=14),
        )
        # Zero balance is capped instead of dividing by zero.
        self.assertEqual(risk_model.features(is_transfer=True, amount=10, balance_before=0, hour=12)[1],
                         math.log1p(risk_model.RATIO_CAP))

    def test_score_is_a_deterministic_probability(self):
        first = risk_model.score(source_type="TRANSFER", amount_minor=80000, balance_minor=85000, hour=3)
        second = risk_model.score(source_type="TRANSFER", amount_minor=80000, balance_minor=85000, hour=3)
        self.assertEqual(first, second)
        self.assertTrue(0.0 <= first["score"] <= 1.0)
        self.assertEqual(first["mode"], "shadow")


class ShadowNeverDecidesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_env = os.environ.get("DIALECTGUARD_DB_PATH")
        os.environ["DIALECTGUARD_DB_PATH"] = str(Path(self.temp_dir.name) / "shadow.sqlite3")
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        mandates.KEY_DIR = self.original_key_dir
        if self.original_env is None:
            os.environ.pop("DIALECTGUARD_DB_PATH", None)
        else:
            os.environ["DIALECTGUARD_DB_PATH"] = self.original_env
        self.temp_dir.cleanup()

    def decisions(self):
        keys = ("outcome", "outcome_band", "risk_code_shown", "actions", "risk_score")
        results = {}
        for preset in PRESETS:
            body = self.client.post("/analyze", json={"preset_id": preset}).json()
            results[preset] = {key: body[key] for key in keys}
        return results

    def test_decisions_identical_whether_model_works_crashes_or_flags_everything(self):
        normal = self.decisions()
        with mock.patch.object(risk_model, "score", side_effect=RuntimeError("broken model")):
            crashed = self.decisions()
        flag_all = {**risk_model._model(), "threshold": 0.0}
        with mock.patch.object(risk_model, "_model", return_value=flag_all):
            flagged = self.decisions()
        self.assertEqual(normal, crashed)
        self.assertEqual(normal, flagged)

    def test_score_is_returned_and_logged(self):
        body = self.client.post("/analyze", json={"preset_id": "tnb_normal"}).json()
        self.assertEqual(body["shadow_model"]["mode"], "shadow")
        events = [log["event"] for log in self.client.get("/logs").json()["logs"]]
        self.assertIn("shadow_risk_score", events)
        with mock.patch.object(risk_model, "score", side_effect=RuntimeError("broken model")):
            body = self.client.post("/analyze", json={"preset_id": "tnb_normal"}).json()
        self.assertIsNone(body["shadow_model"])
        events = [log["event"] for log in self.client.get("/logs").json()["logs"]]
        self.assertIn("shadow_model_error", events)


if __name__ == "__main__":
    unittest.main()
