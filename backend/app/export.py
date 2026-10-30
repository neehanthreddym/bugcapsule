"""Review four exact in-memory files, then explicitly approve a standalone ZIP.

Only intact session-issued preserved results are accepted. This is credential
scrubbing, not guaranteed anonymization: untargeted JSON, query values, schema
labels and path identifiers may remain private. Review every file before export.
No baseline, provider prose, bindings, captured headers or metadata are exported.
"""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shlex
from urllib.parse import urlencode, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.agent.execution import ExecutionResult, verified_export_result
from app.engine.replace import _prepare
from app.engine.scrub import scrub_transactions
from app.replay_template import server


FILENAMES = ("transformed.har", "server.py", "verification.json", "README.md")
LIMITATIONS = (
    "Verification covers only the explicitly configured failure.",
    "Credential scrubbing and synthetic replacements do not guarantee anonymization.",
    "Untargeted business values, schema labels, query values and path identifiers may remain private; review all files.",
    "Replay is limited to these recorded JSON transactions; it does not reproduce a complete application.",
    "Replay supports JSON bodies up to 1 MiB and HAR data up to 16 MiB; interim and bodyless response statuses are unsupported.",
)


class ExportError(ValueError):
    """Fixed export diagnostic, without captured values or filesystem paths."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def _digest(files):
    digest = hashlib.sha256()
    for name, data in files:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


@dataclass(frozen=True, repr=False)
class CapsulePreview:
    """Private immutable file bytes; preview is an exact JSON filename/text map."""

    _files: tuple[tuple[str, bytes], ...]
    _token: object = field(default_factory=object, compare=False)

    @property
    def contents(self) -> dict[str, str]:
        return {name: data.decode("utf-8") for name, data in self._files}

    @property
    def preview_json(self) -> str:
        return _json(self.contents)

    @property
    def digest(self) -> str:
        return _digest(self._files)


@dataclass(frozen=True, repr=False)
class ExportApproval:
    _token: object
    _digest: str


def _valid_preview(preview):
    if (not isinstance(preview, CapsulePreview)
            or tuple(name for name, _ in preview._files) != FILENAMES
            or any(type(data) is not bytes for _, data in preview._files)):
        raise ExportError("Invalid capsule snapshot.")


def approve_export(preview: CapsulePreview) -> ExportApproval:
    """Call only after human review of the complete preview and digest."""
    _valid_preview(preview)
    return ExportApproval(preview._token, preview.digest)


def _har(transactions):
    entries = []
    # Reapply the single existing credential policy, never copy raw entries.
    for tx in scrub_transactions(transactions).transactions:
        work = _prepare(tx)
        raw_request = work.entry["request"]
        if "postData" in raw_request and not work.has_request_json:
            raise ExportError("Export supports JSON request bodies only.")
        parts = urlsplit(tx.url)
        if (not parts.path.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in parts.path)
                or raw_request["method"] != tx.method
                or work.entry["response"]["status"] != tx.status):
            raise ExportError("Unsupported or conflicting routing data.")
        query = [{"name": item["name"], "value": item["value"]}
                 for item in raw_request["queryString"]]
        query_text = urlencode([(item["name"], item["value"]) for item in query])
        request = {
            "method": tx.method,
            "url": "https://replay.example.test" + parts.path + ("?" + query_text if query_text else ""),
            "httpVersion": "HTTP/1.1", "headers": [], "queryString": query,
            "headersSize": -1, "bodySize": 0,
        }
        if work.has_request_json:
            text = _json(work.request_json)
            request["postData"] = {"mimeType": "application/json", "text": text}
            request["bodySize"] = len(text.encode("utf-8"))
        text = _json(work.response_json)
        size = len(text.encode("utf-8"))
        entries.append({
            "startedDateTime": "2000-01-01T00:00:00Z", "time": 0,
            "request": request,
            "response": {"status": tx.status, "statusText": "", "httpVersion": "HTTP/1.1",
                         "headers": [], "content": {"size": size, "mimeType": "application/json", "text": text},
                         "redirectURL": "", "headersSize": -1, "bodySize": size},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
        })
    har = {"log": {"version": "1.2", "creator": {"name": "BugCapsule", "version": "1"}, "entries": entries}}
    server.build_routes(har)  # Fail closed on ambiguous keys, types and unsupported status/body.
    if len(_json(har).encode("utf-8")) > server.MAX_HAR_BYTES:
        raise ExportError("Capsule data exceeds replay limit.")
    return har


def _report(verification):
    # The verifier's schema contains only fixed codes, numeric counts and safe
    # locations, never configured selectors. Copy its fields explicitly.
    def location(loc):
        return {"entry_id": loc.entry_id, "target": loc.target,
                "refs": list(loc.refs), "record": list(loc.record)}

    def evidence(value):
        return {
            "observations": value.observations, "failures": value.failures,
            "distinct_identities": value.distinct_identities,
            "record_counts": [{"location": location(item.location), "count": item.count} for item in value.record_counts],
            "equality_groups": [[location(loc) for loc in group] for group in value.equality_groups],
            "fields": [{"location": location(item.location), "state": item.state,
                        "value_type": item.value_type} for item in value.fields],
            "definition_locations": [location(loc) for loc in value.definition_locations],
            "resolutions": [{"location": location(item.location), "state": item.state,
                             "definitions": [location(loc) for loc in item.definitions]} for item in value.resolutions],
        }
    return {"check": verification.check, "outcome": verification.outcome, "reason": verification.reason,
            "baseline": evidence(verification.baseline), "transformed": evidence(verification.transformed),
            "limitations": list(LIMITATIONS)}


def _readme(har):
    commands = []
    for entry in har["log"]["entries"]:
        request = entry["request"]
        parts = urlsplit(request["url"])
        url = "http://127.0.0.1:8000" + parts.path + ("?" + parts.query if parts.query else "")
        args = ["curl", "--noproxy", "*", "--globoff", "--path-as-is", "--include", "--request", request["method"], "--url", url]
        if "postData" in request:
            args += ["--header", "Content-Type: application/json", "--data-raw", request["postData"]["text"].rstrip("\n")]
        commands.append(shlex.join(args))
    return ("# BugCapsule local replay\n\n" + "\n\n".join(LIMITATIONS)
            + "\n\nReview all four files before sharing. This capsule contains transformed data, not a model-safe payload.\n"
            + "\nExtract the ZIP and run from its extracted directory using Python 3.11+ (standard library only):\n\n"
            + "```sh\npython3 server.py --port 8000\n```\n\nIn another terminal, reproduce the recorded requests in order:\n\n```sh\n"
            + "\n".join(commands) + "\n```\n\nThe server binds only to 127.0.0.1. "
            + "Use `--port 0` for an assigned port, shown at startup; update curl URLs accordingly. "
            + "No API keys, BugCapsule install, PYTHONPATH, or upstream services are needed. "
            + "Unmatched requests return a local error. Stop with Ctrl-C.\n")


def prepare_capsule(result: ExecutionResult) -> CapsulePreview:
    """Build all four files in memory from an intact successful execution receipt."""
    try:
        verified = verified_export_result(result)
        har = _har(verified.candidate)
        files = (_json(har), Path(server.__file__).read_text(encoding="utf-8"),
                 _json(_report(verified.verification)), _readme(har))
        return CapsulePreview(tuple((name, text.encode("utf-8")) for name, text in zip(FILENAMES, files, strict=True)))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OSError):
        raise ExportError("Cannot prepare capsule from this result or replay data.") from None


def write_capsule(preview: CapsulePreview, approval: ExportApproval | None, output: Path | str) -> Path:
    """Write only the approved four fixed members; exclusive creation, no overwrite.

    Parent directories are created only after approval validation. Output paths
    are caller-selected, never extracted from captured data or archive members.
    """
    _valid_preview(preview)
    files = preview._files  # Immutable local snapshot, also used for approval validation.
    if (not isinstance(approval, ExportApproval) or approval._token is not preview._token
            or approval._digest != _digest(files)):
        raise ExportError("Missing or stale capsule export approval.")
    path = Path(output)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output_file:
            with ZipFile(output_file, "w", compression=ZIP_DEFLATED) as archive:
                for name, data in files:
                    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = 0o100600 << 16
                    archive.writestr(info, data)
    except FileExistsError:
        raise ExportError("Output already exists; refusing overwrite.") from None
    except OSError:
        raise ExportError("Could not write approved capsule.") from None
    return path
