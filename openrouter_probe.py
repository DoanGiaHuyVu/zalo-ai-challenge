"""Development-only Qwen3-14B comparison using the existing local data tools.

The remote provider receives business context/previews, never gold answers. The
script-only numeric finisher checks references and shape, not diagnostic meaning
or answer correctness. It is deliberately separate from production qualification.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib import error, request

import solver


MODEL = "qwen/qwen3-14b"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_QUESTION = "q_0cb7c9b93048"
ROOT = Path(__file__).resolve().parent


def wire_action_schema(first_action):
    """XGrammar cannot enforce uniqueItems; the full host validator still does.

    Only the transport copy changes. Keep the model's prompt and local action
    policy intact, and record both schema identities in each call.
    """
    def copy_schema(value):
        if isinstance(value, dict):
            return {key: copy_schema(item) for key, item in value.items() if key != "uniqueItems"}
        if isinstance(value, list):
            return [copy_schema(item) for item in value]
        return value
    return copy_schema(solver.get_action_schema(first_action=first_action))


def redact(value, secret):
    """Also sanitize provider error/content strings before persisting them."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    return value


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise error.HTTPError(req.full_url, code, "OpenRouter redirect refused", headers, fp)


class OpenRouterProvider:
    def __init__(self, api_key, config, token_counter, *, deadline, checkpoint=None, opener=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Set OPENROUTER_API_KEY in your environment; do not put it in source or chat.")
        self._api_key = api_key
        self.config = deepcopy(config)
        self.token_counter = token_counter
        self.deadline = deadline
        self.checkpoint = checkpoint
        self.opener = opener or request.build_opener(_NoRedirect()).open
        self.calls = []

    def _checkpoint(self):
        if self.checkpoint:
            self.checkpoint(redact(deepcopy(self.calls), self._api_key))

    def __call__(self, messages, *, first_action):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("OpenRouter question deadline exhausted before HTTP request")
        if len(self.calls) >= self.config["agent"]["max_turns"]:
            raise RuntimeError("OpenRouter call budget exhausted; no extra request sent")
        schema = solver.get_action_schema(first_action=first_action)
        wire_schema = wire_action_schema(first_action)
        payload = {"model": MODEL, "messages": messages, "stream": False,
                   "temperature": self.config["generation"]["temperature"],
                   "seed": self.config["generation"]["seed"],
                   "max_tokens": self.config["generation"]["max_output_tokens"],
                   "reasoning": {"enabled": False, "exclude": True},
                   "provider": {"require_parameters": True},
                   "response_format": {"type": "json_schema", "json_schema": {
                       "name": "datamate_action", "strict": True, "schema": wire_schema}}}
        call = {"number": len(self.calls) + 1, "first_action": first_action,
                "status": "running", "requested_model": MODEL,
                "estimated_prompt_tokens": self.token_counter(messages),
                "token_count_method": "local_8b_vocabulary_estimate_not_remote_qualification",
                "messages_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
                "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                "wire_schema_sha256": hashlib.sha256(json.dumps(wire_schema, sort_keys=True).encode()).hexdigest(),
                "host_only_schema_constraints": ["uniqueItems"]}
        if messages:
            state = json.loads(messages[-1]["content"])
            if "working_state" in state:
                call["preceding_working_state"] = state["working_state"]
        self.calls.append(call)
        started = time.monotonic()
        self._checkpoint()
        try:
            req = request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json",
                         "X-OpenRouter-Title": "DataMate experimental probe"}, method="POST")
            # Socket timeout plus parent watchdog: a slow response cannot extend
            # the question indefinitely. There are no automatic HTTP retries.
            timeout = min(60.0, self.deadline - time.monotonic())
            if timeout <= 0:
                raise RuntimeError("OpenRouter deadline exhausted during request preparation")
            with self.opener(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"),
                    object_pairs_hook=solver._unique_json_object, parse_constant=solver._reject_constant)
            if not isinstance(data, dict):
                raise RuntimeError("OpenRouter response must be a JSON object")
            if "error" in data:
                raise RuntimeError(f"OpenRouter returned an error: {redact(data['error'], self._api_key)}")
            call.update(response_id=data.get("id"), returned_model=data.get("model"),
                        provider=data.get("provider"), usage=data.get("usage", {}))
            if not isinstance(data.get("model"), str) or data["model"].lower() != MODEL:
                raise RuntimeError("OpenRouter returned a different or unidentified model; no fallback accepted")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise RuntimeError("OpenRouter returned no completion choice")
            choice = choices[0]
            content = choice.get("message", {}).get("content")
            call.update(raw_content=content, finish_reason=choice.get("finish_reason"))
            if not isinstance(call["usage"], dict):
                raise RuntimeError("OpenRouter returned malformed usage metadata")
            actual = call["usage"].get("prompt_tokens")
            if type(actual) is int:
                call["prompt_estimate_difference"] = actual - call["estimated_prompt_tokens"]
                if actual + payload["max_tokens"] > self.config["generation"]["context_tokens"]:
                    raise RuntimeError("Reported remote prompt exceeds nominal harness budget; estimate was insufficient")
            if choice.get("finish_reason") == "length":
                raise ValueError("OpenRouter action was truncated at the output/context limit")
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"Unexpected OpenRouter finish reason: {choice.get('finish_reason')!r}")
            action = solver.parse_action(content, first_action=first_action)
            call.update(status="passed", action=action)
            return action
        except error.HTTPError as exc:
            # Do not dump headers or request objects. Provider messages help
            # diagnose unsupported schema/parameters without silently degrading.
            try:
                detail = exc.read(65536).decode("utf-8", errors="replace")
            except Exception:
                detail = "Provider error body unavailable"
            finally:
                exc.close()
            message = f"OpenRouter HTTP {exc.code}: {redact(detail, self._api_key)}"
            call.update(status="failed", error=message)
            raise RuntimeError(message) from None
        except Exception as exc:
            message = redact(str(exc), self._api_key)
            call.update(status="failed", error=message)
            if isinstance(exc, ValueError):
                raise ValueError(message) from None
            raise RuntimeError(message) from None
        finally:
            call["duration_seconds"] = time.monotonic() - started
            self._checkpoint()


def experimental_finish(action, *, question, store, results, source_catalog):
    """Project one numeric cell; no diagnostic semantics/accuracy certification."""
    args = action["args"]
    if args["answer_type"] not in ("number", "percent") or args.get("layout") != "scalar":
        raise ValueError("Experimental callback supports scalar number/percent only; repair or abstain")
    candidate, check = args["result_ref"], args["check_ref"]
    if results[candidate]["kind"] != "compute" or results[check]["kind"] != "check" or results[check].get("candidate_ref") != candidate:
        raise ValueError("Experimental finish needs an executed check bound to this compute result")
    store.describe(check)
    metadata = store.describe(candidate)
    columns = args["columns"]
    if len(columns) != 1 or type(columns[0]) is not int or not 1 <= columns[0] <= len(metadata["schema"]):
        raise ValueError("Experimental scalar requires one valid column position")
    rows = store.iter_rows(candidate)
    first = next(rows, None)
    if first is None or next(rows, None) is not None:
        raise ValueError("Experimental scalar result must contain exactly one row")
    value = first[columns[0] - 1]
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("Experimental number must be a finite integer/float; use an explicit SQL cast if needed")
    return {"id": question["id"], "type": args["answer_type"], "answer": value}


def run_probe(root, question, config, api_key, progress):
    started = time.monotonic()
    report = {"status": "running", "phase": "initializing", "active_request_started": started,
              "model": MODEL, "question": question, "split_root": str(root), "calls": [],
              "experimental": True, "business_correctness": "unverified",
              "answer_validation": "reference_binding_and_numeric_shape_only",
              "diagnostic_semantics": "not_enforced_T17_deferred",
              "context_admission": "estimated_not_exact_remote_token_qualification",
              "reasoning_requested": False, "reasoning_enforcement": "provider_dependent",
              "config": config, "source_sha256": {
                  name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                  for name in ("openrouter_probe.py", "solver.py", "data_tools.py")}}

    def save():
        solver._save_progress(progress, redact(report, api_key))

    save()
    try:
        from llama_cpp import Llama
        artifact = solver.verify_model_artifact(config)
        report["tokenizer_artifact"] = config["model"]
        business = root / "data" / question["business_id"]
        inventory = solver.build_inventory(business)
        paths = [root / "questions.jsonl", root / "rulebook.md", root / "calendar.csv", business / "README.md"]
        paths.extend(business / entry["file"] for entry in inventory["files"])
        report["input_sha256"] = {}
        for path in paths:
            with path.open("rb") as stream:
                report["input_sha256"][path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        with closing(Llama(model_path=str(artifact), vocab_only=True, n_gpu_layers=0, use_mmap=True,
                           n_ctx=config["generation"]["context_tokens"], seed=config["generation"]["seed"], verbose=False)) as tokenizer:
            report["initialization_seconds"] = time.monotonic() - started
            question_started = time.monotonic()
            report.update(phase="question", active_request_started=question_started)
            save()

            def checkpoint(calls):
                report["calls"] = calls
                save()

            counter = lambda messages: solver.count_prompt_tokens(tokenizer, messages)
            provider = OpenRouterProvider(api_key, config, counter,
                deadline=question_started + config["timeouts"]["question_mac_seconds"], checkpoint=checkpoint)
            outcome = solver.run_question(root, question, action_provider=provider, token_counter=counter,
                                          config=config, finish_handler=experimental_finish)
            report.update(outcome=outcome, question_seconds=time.monotonic() - question_started)
            accepted = outcome["stop_reason"] == "finish_accepted_by_handler"
            report["status"] = "experimental_answer" if accepted else "abstained"
            if not accepted:
                last = outcome.get("trajectory", [{}])[-1] if outcome.get("trajectory") else {}
                observation = last.get("observation", {})
                report["reason"] = (outcome.get("error") or observation.get("error", {}).get("message")
                                    or observation.get("reason") or outcome["stop_reason"])
            if outcome["stop_reason"] in {"provider_error", "context_error", "backend_error", "question_deadline"}:
                report["status"] = "failed"
                report["error"] = report["reason"]
    except Exception as exc:
        report.update(status="failed", error=redact(str(exc), api_key))
    finally:
        report.update(phase="complete", active_request_started=None, total_worker_seconds=time.monotonic() - started)
        save()
    return redact(report, api_key)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question-id", default=DEFAULT_QUESTION)
    parser.add_argument("--split-root", type=Path, default=ROOT / "train")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker != (args.report_file is not None) or (args._worker and args.output_dir is not None):
        parser.error("Internal worker/report flags must be paired and cannot use --output-dir")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    try:
        root, question = solver._select_probe_question(args.split_root, args.question_id)
        config = solver.load_config(args.config)
        if not key:
            raise ValueError("OPENROUTER_API_KEY is not set. Configure it in your environment, not source or chat.")
        if args._worker:
            print(json.dumps(run_probe(root, question, config, key, args.report_file), ensure_ascii=False, allow_nan=False))
            return 0
        destination = args.output_dir or ROOT / "artifacts" / "openrouter" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=False)  # Never overwrite an earlier attempt.
        progress = destination / "progress.json"
        command = [sys.executable, str(Path(__file__).resolve()), "--_worker", "--report-file", str(progress),
                   "--question-id", args.question_id, "--split-root", str(root), "--config", str(args.config.resolve())]
        report, log = solver._run_feasibility_worker(command, progress, overall_seconds=600,
            request_seconds=config["timeouts"]["question_mac_seconds"], initialization_seconds=300)
        report = redact(report, key)
        solver._save_progress(destination / "report.json", report)
        (destination / "runtime.log").write_text(redact(log, key), encoding="utf-8")
        outcome = report.get("outcome", {})
        print(json.dumps({"status": report["status"], "experimental": True, "business_correctness": "unverified",
                          "submission": outcome.get("submission"), "stop_reason": outcome.get("stop_reason"),
                          "turns_used": outcome.get("turns_used"), "seconds": report.get("total_seconds"),
                          "error": report.get("error"), "reason": report.get("reason"),
                          "report": str(destination / "report.json")},
                         ensure_ascii=False, allow_nan=False))
        return 1 if report["status"] == "failed" else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"OpenRouter probe error: {redact(str(exc), key)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
