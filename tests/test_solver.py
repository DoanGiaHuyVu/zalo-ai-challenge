import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import solver


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


class ModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.config = solver.load_config(CONFIG_PATH)

    def test_paths_are_relative_to_config_not_working_directory(self):
        self.assertEqual(Path(self.config["model"]["path"]), CONFIG_PATH.parent / "models/qwen3-8b-q4_k_m.gguf")

    def test_missing_and_corrupt_weights_never_initialize_or_fetch(self):
        with tempfile.TemporaryDirectory() as temporary, patch("llama_cpp.Llama") as native, patch("socket.create_connection", side_effect=AssertionError("Network used")):
            cfg = copy.deepcopy(self.config)
            path = Path(temporary) / "model.gguf"
            cfg["model"].update(path=str(path), size_bytes=4, sha256=hashlib.sha256(b"GGUF").hexdigest())
            with self.assertRaisesRegex(FileNotFoundError, "Local model not found"):
                solver.load_model(cfg)
            path.write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                solver.load_model(cfg)
            path.write_bytes(b"FAIL")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                solver.load_model(cfg)
            native.assert_not_called()

    def test_closed_thinking_prefix_is_required(self):
        model = SimpleNamespace(metadata={"tokenizer.chat_template": "{{ '<think>\\n\\n</think>\\n\\n' if enable_thinking == false else '<think>' }}"}, token_eos=lambda: 1, token_bos=lambda: -1, detokenize=lambda tokens, **kwargs: b"<|im_end|>")
        self.assertTrue(callable(solver._non_thinking_handler(model)))
        model.metadata["tokenizer.chat_template"] = "<think>"
        with self.assertRaisesRegex(ValueError, "did not close"):
            solver._non_thinking_handler(model)
        model.metadata = {}
        with self.assertRaisesRegex(ValueError, "no embedded chat template"):
            solver._non_thinking_handler(model)

    def test_initialization_failure_closes_model(self):
        model = Mock(metadata={})
        with patch("solver.verify_model_artifact", return_value=Path("local.gguf")), patch("llama_cpp.llama_supports_gpu_offload", return_value=True), patch("llama_cpp.Llama", return_value=model) as native:
            with self.assertRaisesRegex(ValueError, "no embedded chat template"):
                solver.load_model(self.config)
            model.close.assert_called_once()
            self.assertEqual(native.call_args.kwargs["n_gpu_layers"], -1)
            self.assertEqual(native.call_args.kwargs["n_ctx"], 16384)

    def response(self, content, reason="stop"):
        return {"choices": [{"message": {"content": content}, "finish_reason": reason}], "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}}

    def test_json_generation_passes_schema_settings_and_reports_usage(self):
        model = SimpleNamespace(create_chat_completion=Mock(return_value=self.response('{"ok":true,"language":"vi"}')))
        metrics = {}
        result = solver.generate_json(model, [], solver.SMOKE_SCHEMA, self.config, metrics=metrics)
        self.assertEqual(result, {"ok": True, "language": "vi"})
        args = model.create_chat_completion.call_args.kwargs
        self.assertEqual(args["response_format"], {"type": "json_object", "schema": solver.SMOKE_SCHEMA})
        self.assertEqual((args["temperature"], args["seed"], args["max_tokens"]), (0, 42, 1024))
        self.assertEqual(metrics["usage"]["completion_tokens"], 12)
        self.assertGreaterEqual(metrics["generation_seconds"], 0)

    def test_bad_or_truncated_responses_fail_clearly(self):
        for content, reason, expected in [('{"ok":true}', 'length', 'truncated'), ('<think>text</think>{}', 'stop', 'invalid JSON'), ('[]', 'stop', 'JSON object'), ('{"x":NaN}', 'stop', 'invalid JSON'), (None, 'stop', 'no JSON content'), ('{}', 'tool_calls', 'Unexpected generation stop')]:
            with self.subTest(content=content, reason=reason):
                model = SimpleNamespace(create_chat_completion=Mock(return_value=self.response(content, reason)))
                with self.assertRaisesRegex(ValueError, expected):
                    solver.generate_json(model, [], solver.SMOKE_SCHEMA, self.config)

    def test_smoke_closes_model_after_generation_failure(self):
        model = Mock()
        with patch("solver.load_model", return_value=model), patch("solver.generate_json", side_effect=ValueError("invalid JSON")):
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                solver._smoke(self.config)
        model.close.assert_called_once()

    def test_smoke_timeout_preserves_native_log_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config))
            failure = subprocess.TimeoutExpired(cmd=["model-worker"], timeout=300, stderr=b"native initialization stalled")
            with patch("sys.argv", ["solver.py", "--config", str(path), "--smoke"]), patch("solver.subprocess.run", side_effect=failure) as worker, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(solver.main(), 1)
                self.assertEqual(worker.call_args.kwargs["timeout"], 300)
            report = json.loads((Path(temporary) / "artifacts/t2-smoke.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertIn("300 seconds", report["error"])
            self.assertEqual((Path(temporary) / "artifacts/t2-metal.log").read_text(), "native initialization stalled")

    def test_smoke_rejects_success_without_gpu_offload_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config))
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"status":"passed"}', stderr="CPU only")
            with patch("sys.argv", ["solver.py", "--config", str(path), "--smoke"]), patch("solver.subprocess.run", return_value=completed), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(solver.main(), 1)
            report = json.loads((Path(temporary) / "artifacts/t2-smoke.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertIn("GPU layer offload", report["error"])


if __name__ == "__main__":
    unittest.main()
