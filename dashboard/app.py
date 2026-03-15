import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

load_dotenv()

app = Flask(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyD4WJwNBFy6uCS03EonwqvzQaVtumBl1Hk")
RTG_ROOT = Path(__file__).parent.parent
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Shared log buffer per session (simple in-memory, single-user)
log_buffer = []
log_lock = threading.Lock()
testing_done = False

MAX_LOG_BUFFER = 500

def add_log(msg, level="info"):
    with log_lock:
        log_buffer.append({"level": level, "msg": msg, "time": datetime.now().strftime("%H:%M:%S")})
        if len(log_buffer) > MAX_LOG_BUFFER:
            del log_buffer[0]

def clear_logs():
    with log_lock:
        log_buffer.clear()

BUG_KEYWORDS = [
    'bug', 'BUG', 'crash', 'hijack', 'vulnerability', 'workaround', 'broken',
    'accepts any', 'allows any', 'does not validate', 'no check', 'no validation',
    'no stock', 'no duplicate', 'no auth', 'leaks', 'exploit', 'mass assignment',
    'KeyError', 'inconsisten', 'soft delete', 'soft-delete', 'ghost', 'chained bug',
    'Note:', 'note:', 'still accessible', 'can be set', 'if provided', 'overrides',
    'erroneously', 'does not restore', 'is not restored', 'never decremented',
    'does not decrement', 'inherent', 'known vulnerabilit', 'actual behavior',
    'documented bugs', 'including known', 'rather than removing', 'instead of removing',
    'marks the product as deleted', 'marking the product'
]

SERVER_FIELDS = {
    'id', 'uuid', 'role', 'balance', 'deleted', 'active', 'created_at',
    'updated_at', 'score', 'verified', 'is_admin', 'permissions', 'timestamp',
    'created', 'updated', 'is_deleted', 'is_active'
}

# Fields that are server-assigned on creation but may be client-settable on update
# (e.g. status defaults to "pending" on POST /orders — client must not set it)
CREATE_SERVER_FIELDS = {'status', 'total', 'created_at', 'updated_at'}

NUMERIC_MIN_1 = {'quantity', 'stock', 'count', 'age', 'size', 'page', 'limit', 'per_page', 'pages'}
NUMERIC_MIN_0 = {'price', 'cost', 'fee', 'amount', 'total', 'salary', 'rate', 'discount', 'tax'}
STRING_MIN_1  = {'name', 'username', 'title', 'slug', 'code', 'label'}
STATUS_ENUM   = ['pending', 'confirmed', 'shipped', 'delivered', 'cancelled']
ROLE_ENUM     = ['user', 'admin']
PRIORITY_ENUM = ['low', 'medium', 'high']

BUG_EXAMPLE_KEYS = [
    'bug', 'exploit', 'crash', 'hack', 'attack', 'vuln', 'negative',
    'invalid', 'missing', 'hijack', 'mass', 'subtract', 'buggy'
]


def clean_text(text):
    if not text:
        return text
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text)]
    clean = []
    for s in sentences:
        if not any(kw.lower() in s.lower() for kw in BUG_KEYWORDS):
            clean.append(s)
    return ' '.join(clean).strip() or text.split('.')[0].strip()


def _fix_schema_fields(schema, is_request, is_create_request=False):
    """Apply field-level constraints and cleanup to a single schema dict."""
    props = schema.get('properties', {})
    for field_name in list(props.keys()):
        field = props[field_name]

        remove_fields = SERVER_FIELDS | (CREATE_SERVER_FIELDS if is_create_request else set())
        if is_request and field_name in remove_fields:
            del props[field_name]
            if field_name in schema.get('required', []):
                schema['required'].remove(field_name)
            continue

        if not is_request and field_name in SERVER_FIELDS:
            field['readOnly'] = True

        if 'description' in field:
            field['description'] = clean_text(field['description'])

        ftype = field.get('type', '')

        if ftype in ('integer', 'number') and 'minimum' not in field:
            if field_name in NUMERIC_MIN_1:
                field['minimum'] = 1
            elif field_name in NUMERIC_MIN_0:
                field['minimum'] = 0

        if ftype == 'string':
            if field_name in STRING_MIN_1 and 'minLength' not in field:
                field['minLength'] = 1
            if field_name == 'status' and 'enum' not in field:
                field['enum'] = STATUS_ENUM
            if field_name == 'role' and 'enum' not in field:
                field['enum'] = ROLE_ENUM
            if field_name == 'priority' and 'enum' not in field:
                field['enum'] = PRIORITY_ENUM
            if field.get('format') == 'email' and 'minLength' not in field:
                field['minLength'] = 5

    if 'description' in schema:
        schema['description'] = clean_text(schema['description'])


def fix_spec(spec):
    schemas = spec.get('components', {}).get('schemas', {})

    # Find which named schema names are used in requestBody
    request_schema_names = set()
    for path_item in spec.get('paths', {}).values():
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            rb = op.get('requestBody', {})
            for media in rb.get('content', {}).values():
                ref = media.get('schema', {}).get('$ref', '')
                if ref:
                    request_schema_names.add(ref.split('/')[-1])

    # Identify which schemas are specifically for creation (POST) operations
    create_schema_names = set()
    for path_item in spec.get('paths', {}).values():
        op = path_item.get('post', {})
        if not isinstance(op, dict):
            continue
        rb = op.get('requestBody', {})
        for media in rb.get('content', {}).values():
            ref = media.get('schema', {}).get('$ref', '')
            if ref:
                create_schema_names.add(ref.split('/')[-1])
    # Also treat any schema whose name ends with CreateRequest or CreateInput as a create schema
    for name in list(schemas.keys()):
        lower = name.lower()
        if any(lower.endswith(sfx) for sfx in ('createrequest', 'createinput', 'createpayload', 'createbody')):
            create_schema_names.add(name)

    # Fix named schemas in components/schemas
    for schema_name, schema in schemas.items():
        is_request = schema_name in request_schema_names
        is_create = schema_name in create_schema_names
        _fix_schema_fields(schema, is_request=is_request, is_create_request=is_create)

    # Also fix inline schemas directly embedded in requestBody
    for path_item in spec.get('paths', {}).values():
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            rb = op.get('requestBody', {})
            for media in rb.get('content', {}).values():
                inline = media.get('schema', {})
                # Only process if it's a true inline schema (no $ref at top level)
                if inline and '$ref' not in inline and inline.get('type') == 'object':
                    _fix_schema_fields(inline, is_request=True, is_create_request=(method == 'post'))

    # Clean paths: descriptions, summaries, examples
    for path_item in spec.get('paths', {}).values():
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for key in ('description', 'summary'):
                if key in op:
                    op[key] = clean_text(op[key])

            # Remove bug-demonstrating examples from requestBody
            rb = op.get('requestBody', {})
            for media in rb.get('content', {}).values():
                examples = media.get('examples', {})
                to_remove = [k for k in examples
                             if any(w in k.lower() for w in BUG_EXAMPLE_KEYS)]
                for k in to_remove:
                    del examples[k]

                # Clean example values — strip server fields
                for ex in examples.values():
                    val = ex.get('value', {})
                    if isinstance(val, dict):
                        for sf in list(val.keys()):
                            if sf in SERVER_FIELDS:
                                del val[sf]

    # Clean info
    if 'info' in spec:
        if 'description' in spec['info']:
            spec['info']['description'] = clean_text(spec['info']['description'])
        spec['info']['title'] = spec['info'].get('title', '').replace('(Buggy)', '').replace('Buggy', '').strip()

    return spec


def strip_code_comments(source_code):
    # Remove full-line comments and inline comments containing bug/hack/todo markers
    bug_markers = ("# BUG", "# bug", "# HACK", "# hack", "# TODO", "# todo",
                   "# FIXME", "# fixme", "# NOTE", "# note", "# WARNING", "# warning")
    cleaned_lines = []
    for line in source_code.splitlines():
        stripped = line.strip()
        # Drop full-line comment if it contains a bug marker
        if stripped.startswith("#") and any(m in line for m in bug_markers):
            continue
        # Strip inline bug-marker comments from the end of code lines
        if "#" in line and any(m in line for m in bug_markers):
            line = line[:line.index("#")].rstrip()
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def generate_spec_with_gemini(source_code, server_url="http://localhost:8081"):
    client = genai.Client(api_key=GEMINI_API_KEY)
    source_code = strip_code_comments(source_code)

    prompt = f"""
You are an OpenAPI specification expert and API contract designer.

Below is the full source code of a Python Flask REST API running at {server_url}.

Your task is to generate a complete, valid OpenAPI 3.0.1 specification in JSON format that defines
the CORRECT INTENDED behavior of this API — not what the current code does, but what a well-designed
API of this type should do. This spec will be used as the testing oracle by an automated REST API
testing tool, so precision and correctness of constraints are critical.

⚠️  CRITICAL INSTRUCTION — READ BEFORE ANYTHING ELSE:
The source code may contain comments marked with "# BUG:", "# TODO:", "# HACK:", or similar.
IGNORE ALL OF THEM COMPLETELY.
- Do not reference them
- Do not copy them into descriptions
- Do not let them influence the schema you generate
- Do not use words like: bug, crash, hijack, vulnerability, broken, accepts, allows, workaround
Your spec must define what a CORRECT, WELL-DESIGNED API should do.
Treat the code as a broken implementation of a good API — your job is to define the good API.

─── ENDPOINT COVERAGE ────────────────────────────────────────────────────────
- Include every route, HTTP method, path parameter, query parameter, and request body
- For every parameter include: name, in, required, description, and schema (type + format)
- For every response include: all applicable status codes (200/201, 400, 404, 422, 500),
  a description, and a complete response body schema

─── SCHEMA DESIGN ────────────────────────────────────────────────────────────
- Always define SEPARATE schemas for requests vs responses:
    e.g. <Resource>CreateRequest (client sends) vs <Resource> (server returns)
- Request schemas must EXCLUDE server-generated fields — identify them by context:
    - Auto-generated identifiers (id, uuid, slug) → never in request body
    - Server-assigned metadata (created_at, updated_at, timestamps) → never in request body
    - Server-controlled authority fields (role, status set by system, permissions, score,
      balance, verified, is_admin, deleted, active) → never client-settable in request body
- Response schemas must include ALL fields the API returns, with all fields marked required: true
- Use $ref to reference schemas under components/schemas
- Use descriptive schema names that reflect the operation (e.g. CommentCreateRequest, not CommentInput)

─── BUSINESS LOGIC CONSTRAINTS ───────────────────────────────────────────────
Read the code carefully and infer what constraints SHOULD apply based on the domain.
Apply the following rules universally:

Numeric fields:
- Any field representing a monetary value, price, fee, score, rating, count, age, or size
  → add minimum: 0 (or minimum: 1 if zero makes no sense, e.g. quantity, page number)
- Any field with a natural upper bound (rating out of 5, percentage) → add maximum accordingly
- Integer IDs used in path parameters → format: int32, minimum: 1

String fields:
- Any non-empty identifier (username, title, name, slug, code) → minLength: 1
- Add maxLength based on what makes sense for the domain (e.g. 255 for names, 1000 for descriptions)
- Fields that look like emails → format: email
- Fields that look like UUIDs → format: uuid
- Fields that look like URLs → format: uri
- Fields that look like dates → format: date or date-time
- Fields that look like passwords → format: password, minLength: 8

Enumerated fields:
- Any field that only accepts a fixed set of values (status, type, category, role, state,
  priority, visibility, gender, method) → define an enum listing ALL valid values only
- Derive the valid enum values from the code logic, not from what the buggy code accepts

Boolean fields:
- Always type: boolean, never type: string for true/false fields

─── MASS ASSIGNMENT PREVENTION ───────────────────────────────────────────────
- Mark server-controlled fields in response schemas as readOnly: true
- Never include server-generated or server-controlled fields in POST/PUT request body schemas
- If a field appears in the response but should not be client-settable, exclude it from
  the corresponding request schema entirely

─── CONTRACT COMPLETENESS ────────────────────────────────────────────────────
- In every response schema, mark ALL returned fields as required: true so the testing tool
  can detect missing fields as contract violations
- For collection/list endpoints, wrap the response in an array schema with items: $ref
- Always use a consistent Error schema: {{ "error": {{ "type": "string" }} }} for all error responses
- Include realistic, domain-appropriate example values for every field and every schema

─── TESTING COVERAGE HINTS ───────────────────────────────────────────────────
This spec will be used by an automated tool to test for the following bug categories.
Ensure the spec is designed to enable detection of each:

1. CRUD workflows (missing validation, ghost data, duplicate records):
   - Add 409 Conflict response to every POST endpoint that creates a uniquely identified resource
   - Ensure DELETE responses are distinct from GET responses so ghost data is detectable
   - Mark all required creation fields as required: true in request schemas

2. Security — mass assignment (client setting server-owned fields):
   - Exclude all server-generated/server-controlled fields from request schemas
   - Mark them readOnly: true in response schemas

3. Security — unauthorized access:
   - Add 401 Unauthorized to any endpoint that should require authentication
   - Add 403 Forbidden to any endpoint that should require specific roles/permissions

4. Crash detection (unhandled nulls, missing fields causing 500s):
   - Mark every field that would crash the server if missing as required: true
   - Never mark a crash-causing field as optional

5. Contract violations (response missing fields, wrong types):
   - Every response schema must have ALL returned fields marked required: true
   - Use the correct type for every field — never use string for numeric or boolean values

6. Business logic (invalid values, negative numbers, bad states):
   - Add minimum/maximum constraints for all numeric fields
   - Add enum constraints for all status/type/role/state fields with only valid values

7. Method abuse (wrong HTTP verbs accepted):
   - Only define HTTP methods that are explicitly implemented for each path
   - Do not add methods that the code does not handle

8. Chained operation bugs (bugs appearing only after a sequence of calls):
   - Ensure output field names in responses exactly match the input parameter names
     they feed into across operations
   - Example: if POST /users returns {{ "id": 1 }}, then GET /users/{{user_id}} must use
     the name "user_id" — and the response "id" will be matched to it by the testing tool
   - Keep parameter naming consistent across all related endpoints

─── WHAT NOT TO DO ───────────────────────────────────────────────────────────
- Do NOT use these words anywhere in descriptions: bug, crash, hijack, vulnerability,
  broken, workaround, accepts, allows, hack, missing, note, warning
- Do NOT reuse the same schema for both request body and response
- Do NOT omit constraints just because the current implementation does not enforce them
- Do NOT add implementation details or code variable names to descriptions
- Do NOT use a reduced/summary schema for list endpoints — use the full resource schema
- Write descriptions that state the intended purpose and valid values ONLY

─── SELF-CHECK BEFORE OUTPUT ─────────────────────────────────────────────────
Before returning the JSON, mentally verify every item:

[ ] No field description contains: bug, crash, hijack, accepts, allows, vulnerability,
    broken, workaround, note, or any reference to broken behavior
[ ] Request schemas do NOT contain any of these server-assigned fields:
    id, role, balance, deleted, created_at, updated_at, score, verified, is_admin,
    permissions, or any field the server sets automatically
[ ] Every numeric field representing a value, amount, quantity, count, or price
    has a minimum constraint (minimum: 0 or minimum: 1)
[ ] Every field representing a fixed set of choices (status, role, type, state,
    priority, category, method) has an enum constraint listing ONLY the valid values
[ ] Every string identifier field (name, username, title, slug) has minLength: 1
[ ] Every response schema has ALL its fields marked as required: true
[ ] List/collection endpoints return the FULL resource schema, NOT a reduced summary
[ ] Every POST endpoint that creates a unique resource has a 409 Conflict response
[ ] Server-controlled fields in response schemas are marked readOnly: true

─── OUTPUT FORMAT ────────────────────────────────────────────────────────────
- Server url must be: {server_url}
- Output ONLY raw JSON — no markdown, no code fences, no explanation text
- Start with {{ and end with }}

Flask API source code:
```python
{source_code}
```

Return only the JSON object starting with {{ and ending with }}.
"""

    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    # Step 2: Fix the spec using deterministic Python post-processing
    spec_dict = json.loads(raw)
    fixed_dict = fix_spec(spec_dict)
    return json.dumps(fixed_dict, indent=2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    api_name = request.form.get("api_name", "custom-api").strip().lower().replace(" ", "-")
    server_url = request.form.get("server_url", "http://localhost:8081").strip()
    server_port = request.form.get("server_port", "8081").strip()

    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    # Save uploaded file
    upload_path = UPLOAD_DIR / file.filename
    file.save(upload_path)
    source_code = upload_path.read_text()

    clear_logs()
    add_log(f"Uploaded: {file.filename} ({len(source_code)} chars)")
    add_log(f"API name: {api_name} | Server: {server_url}")

    # Create API directory structure
    api_dir = RTG_ROOT / "apis" / api_name
    spec_dir = api_dir / "specifications"
    scripts_dir = api_dir / "scripts"
    spec_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Copy source file to scripts dir
    shutil.copy(upload_path, scripts_dir / file.filename)

    # Generate spec with Gemini (2-step: generate then fix)
    add_log("Step 1/2: Generating spec with Gemini 2.5 Flash...", "info")
    try:
        raw_spec = generate_spec_with_gemini(source_code, server_url)
        add_log("Step 2/2: Cleaning and fixing spec constraints...", "info")
        spec = json.loads(raw_spec)
        add_log("Spec generated and fixed successfully!", "success")
    except json.JSONDecodeError as e:
        add_log(f"Gemini returned invalid JSON: {e}", "error")
        return jsonify({"error": "Gemini returned invalid JSON"}), 500
    except Exception as e:
        add_log(f"Gemini error: {e}", "error")
        return jsonify({"error": str(e)}), 500

    # Save spec
    spec_path = spec_dir / "openapi.json"
    spec_path.write_text(json.dumps(spec, indent=2))
    add_log(f"Spec saved: {spec_path}")

    # Count endpoints
    endpoints = list(spec.get("paths", {}).keys())
    add_log(f"Endpoints found: {endpoints}")

    # Write api-config.yml
    config_path = api_dir / "api-config.yml"
    config_path.write_text(f"name: {api_name}\nspecificationFileName: openapi.json\nhost: \"{server_url}/\"\n")

    # Update rtg-config.yml
    rtg_config = RTG_ROOT / "rtg-config.yml"
    rtg_config.write_text(f"apiUnderTest: {api_name}\nstrategyClassName: NominalAndErrorStrategy\n")
    add_log(f"RestTestGen configured to test: {api_name}")

    return jsonify({
        "success": True,
        "api_name": api_name,
        "endpoints": endpoints,
        "spec": spec,
        "server_url": server_url,
        "server_port": server_port,
        "source_file": file.filename
    })


@app.route("/start-api", methods=["POST"])
def start_api():
    data = request.json
    script_path = str(RTG_ROOT / "apis" / data["api_name"] / "scripts" / data["source_file"])
    port = data.get("server_port", "8081")

    add_log(f"Starting API server on port {port}...")

    def run_server():
        subprocess.Popen(
            ["python3", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    threading.Thread(target=run_server, daemon=True).start()
    time.sleep(2)

    # Check if it started
    import urllib.request
    server_url = data.get("server_url", f"http://localhost:{port}")
    try:
        urllib.request.urlopen(server_url, timeout=3)
        add_log("API server is up!", "success")
        return jsonify({"success": True})
    except Exception:
        add_log("API server started (could not verify — check manually)", "warn")
        return jsonify({"success": True, "warn": "Could not verify server"})


@app.route("/run-tests", methods=["POST"])
def run_tests():
    global testing_done
    testing_done = False
    add_log("Starting RestTestGen... (Gradle may take a few seconds to initialize)", "info")

    def run():
        global testing_done
        env = os.environ.copy()
        env["JAVA_HOME"] = "/usr/lib/jvm/jdk-17"
        env["PATH"] = "/usr/lib/jvm/jdk-17/bin:" + env.get("PATH", "")

        process = subprocess.Popen(
            ["./gradlew", "run"],
            cwd=str(RTG_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env
        )

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if "ERROR" in line or "BREACH" in line or "CRASH" in line:
                add_log(line, "error")
            elif "WARN" in line:
                add_log(line, "warn")
            elif "BUILD SUCCESSFUL" in line:
                add_log("RestTestGen completed successfully!", "success")
            elif "BUILD FAILED" in line:
                add_log("RestTestGen build failed!", "error")
            elif "Executed test interaction" in line or "BreachingFuzzer" in line or "AdaptiveErrorFuzzer" in line:
                add_log(line, "info")

        process.wait()
        add_log("Testing finished.", "success")
        testing_done = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True})


@app.route("/logs")
def logs():
    def stream():
        seen = 0
        while True:
            with log_lock:
                current = log_buffer[seen:]
            for entry in current:
                yield f"data: {json.dumps(entry)}\n\n"
                seen += 1
            if testing_done and seen >= len(log_buffer):
                yield f"data: {json.dumps({'level': 'done', 'msg': '__done__', 'time': ''})}\n\n"
                break
            time.sleep(0.5)

    return Response(stream_with_context(stream()), mimetype="text/event-stream")


@app.route("/report")
def report():
    api_name = request.args.get("api_name", "")
    results_dir = RTG_ROOT / "apis" / api_name / "results"
    if not results_dir.exists():
        return "No results yet.", 404

    sessions = sorted(results_dir.iterdir(), reverse=True)
    for session in sessions:
        report_path = session / "html-report" / "report.html"
        if report_path.exists():
            html = report_path.read_text()
            html = html.replace(
                "report-resources/",
                f"/report-assets/{api_name}/{session.name}/"
            )
            return html, 200, {"Content-Type": "text/html"}

    return "No HTML report found.", 404


@app.route("/report-assets/<api_name>/<session>/<path:filename>")
def report_assets(api_name, session, filename):
    from flask import send_from_directory
    asset_dir = RTG_ROOT / "apis" / api_name / "results" / session / "html-report" / "report-resources"
    return send_from_directory(str(asset_dir), filename)


ORACLE_DESCRIPTIONS = {
    'ErrorStatusCodeOracle': 'Server accepted an invalid/malformed request (should have returned 4xx)',
    'StatusCodeOracle':      'Server returned wrong HTTP status code for a valid request',
    'SchemaOracle':          'Response body missing required fields or has wrong data types',
    'CrashOracle':           'Server crashed with HTTP 500',
    'NotNullOracle':         'Response contained null where a value is required',
}


def _normalize_path(url):
    """Strip query string and normalize numeric path segments to {id}."""
    path = url.split('?')[0]
    # Keep only the path portion (after host:port)
    if 'localhost:' in path:
        path = '/' + path.split('localhost:')[1].lstrip('/').split('/', 1)[-1] if '/' in path.split('localhost:')[1] else '/'
    # Replace numeric path segments with {id}
    path = re.sub(r'/\d+', '/{id}', path)
    return path


def collect_failures(api_name, max_per_oracle=20):
    """Parse all JSON test reports and return a deduplicated list of failure dicts."""
    results_dir = RTG_ROOT / "apis" / api_name / "results"
    if not results_dir.exists():
        return []

    sessions = sorted(results_dir.iterdir(), reverse=True)
    if not sessions:
        return []

    latest = sessions[0]
    # Collect all unique failures first, then cap
    all_failures = {}   # sig -> failure dict (keeps best example)

    for fuzzer_dir in (latest / "json-reports").iterdir():
        if not fuzzer_dir.is_dir():
            continue
        for report_file in fuzzer_dir.glob("*.json"):
            try:
                data = json.loads(report_file.read_text())
            except Exception:
                continue

            for oracle_name, oracle in data.get('testResults', {}).items():
                if oracle.get('result') != 'FAIL':
                    continue

                for ti in data.get('testInteractions', []):
                    method = ti.get('requestMethod', '')
                    url    = ti.get('requestURL', '')
                    body   = ti.get('requestBody', '')
                    status = ti.get('responseStatusCode', {}).get('code')
                    resp   = ti.get('responseBody', '')

                    norm_path = _normalize_path(url)
                    sig = f"{method}|{norm_path}|{oracle_name}"

                    if sig not in all_failures:
                        all_failures[sig] = {
                            'oracle':       oracle_name,
                            'description':  ORACLE_DESCRIPTIONS.get(oracle_name, oracle_name),
                            'oracle_msg':   oracle.get('message', ''),
                            'generator':    data.get('generator', ''),
                            'method':       method,
                            'url':          url,
                            'norm_path':    norm_path,
                            'request_body': body[:400] if body else None,
                            'status_code':  status,
                            'response':     resp[:300] if resp else None,
                        }

    # Cap per oracle type to keep Gemini prompt manageable
    from collections import defaultdict
    oracle_counts = defaultdict(int)
    failures = []
    for f in all_failures.values():
        if oracle_counts[f['oracle']] < max_per_oracle:
            failures.append(f)
            oracle_counts[f['oracle']] += 1

    return failures


@app.route("/fix-code", methods=["POST"])
def fix_code():
    data = request.json or {}
    api_name    = data.get("api_name", "")
    source_file = data.get("source_file", "")

    if not api_name or not source_file:
        return jsonify({"error": "api_name and source_file required"}), 400

    source_path = RTG_ROOT / "apis" / api_name / "scripts" / source_file
    if not source_path.exists():
        return jsonify({"error": "Source file not found"}), 404

    source_code = source_path.read_text()
    failures    = collect_failures(api_name)

    if not failures:
        return jsonify({"error": "No test failures found. Run RestTestGen first."}), 400

    # Build a concise bug report for Gemini
    bug_report_lines = []
    for i, f in enumerate(failures, 1):
        line = f"[{i}] {f['method']} {f['url']}"
        if f['request_body']:
            line += f"\n    Request body: {f['request_body']}"
        line += f"\n    Server responded: HTTP {f['status_code']} — {(f['response'] or '')[:120]}"
        line += f"\n    Bug type: {f['description']}"
        line += f"\n    Oracle message: {f['oracle_msg']}"
        bug_report_lines.append(line)

    bug_report = "\n\n".join(bug_report_lines)

    prompt = f"""You are a security-focused Python backend developer.

Below is a Flask REST API source file that has been automatically tested by RestTestGen.
The test tool found the following vulnerabilities and contract violations:

─── DETECTED BUGS ──────────────────────────────────────────────────────────────
{bug_report}

─── SOURCE CODE ────────────────────────────────────────────────────────────────
```python
{source_code}
```

─── YOUR TASK ──────────────────────────────────────────────────────────────────
Fix every detected bug in the source code. Apply these rules strictly:

1. ErrorStatusCodeOracle failures (server accepted invalid input):
   - Add input validation at the start of the route
   - Return HTTP 400 or 422 for invalid values (negative numbers, wrong types, out-of-enum strings, missing required fields)
   - Validate EVERY parameter the route uses before using it

2. StatusCodeOracle failures (wrong status code for valid request):
   - Return the correct HTTP status code (201 for creation, 200 for update/get, 204 for delete with no body)

3. SchemaOracle failures (response missing fields):
   - Ensure the response always includes ALL required fields
   - Fix any conditional field omissions

4. CrashOracle failures (HTTP 500 crashes):
   - Add try/except or null checks to prevent KeyError, TypeError, AttributeError
   - Return 400 for missing/null required fields instead of crashing

5. Mass assignment (server-controlled fields set by client):
   - Never read id, role, balance, deleted, created_at, updated_at from request body
   - Always assign those fields server-side

6. Business logic fixes:
   - Reject negative or zero prices/quantities/amounts
   - Reject invalid enum values for status, role, category, priority
   - Reject duplicate resources (return 409)
   - After DELETE, return 404 if the resource is accessed again (do not return soft-deleted data)
   - After DELETE, restore stock if an order is cancelled

─── OUTPUT FORMAT ──────────────────────────────────────────────────────────────
- Output ONLY the complete fixed Python source code
- No explanation, no markdown, no code fences
- Preserve the original structure and all routes
- Add only the validation/fix logic needed — do not refactor or rename anything
- Start directly with: from flask import ...
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    fixed_code = response.text.strip()

    # Strip any accidental code fences
    fixed_code = re.sub(r"^```(?:python)?\s*", "", fixed_code, flags=re.MULTILINE)
    fixed_code = re.sub(r"\s*```$", "", fixed_code, flags=re.MULTILINE)
    fixed_code = fixed_code.strip()

    # Save fixed file alongside original
    fixed_path = source_path.parent / ("fixed_" + source_file)
    fixed_path.write_text(fixed_code)

    return jsonify({
        "success":       True,
        "fixed_code":    fixed_code,
        "bugs_found":    len(failures),
        "fixed_file":    fixed_path.name,
        "fixed_path":    str(fixed_path),
    })


@app.route("/results")
def results():
    api_name = request.args.get("api_name", "")
    results_dir = RTG_ROOT / "apis" / api_name / "results"
    if not results_dir.exists():
        return jsonify({"sessions": []})

    sessions = sorted(results_dir.iterdir(), reverse=True)
    data = []
    has_report = False
    for session in sessions[:5]:
        nominal_dir = session / "json-reports" / "nominal-fuzzer"
        passed = failed = 0
        if nominal_dir.exists():
            for f in nominal_dir.glob("*.json"):
                try:
                    r = json.loads(f.read_text())
                    result = r.get("testResults", {})
                    for oracle in result.values():
                        if oracle.get("result") == "PASS":
                            passed += 1
                        elif oracle.get("result") == "FAIL":
                            failed += 1
                except Exception:
                    pass

        # Count unique vulnerabilities across all fuzzers (deduped by method+path+oracle)
        vuln_sigs = set()
        json_reports_dir = session / "json-reports"
        if json_reports_dir.exists():
            for fuzzer_dir in json_reports_dir.iterdir():
                if not fuzzer_dir.is_dir():
                    continue
                for report_file in fuzzer_dir.glob("*.json"):
                    try:
                        r = json.loads(report_file.read_text())
                    except Exception:
                        continue
                    for oracle_name, oracle in r.get("testResults", {}).items():
                        if oracle.get("result") != "FAIL":
                            continue
                        for ti in r.get("testInteractions", []):
                            method = ti.get("requestMethod", "")
                            url = ti.get("requestURL", "")
                            vuln_sigs.add(f"{method}|{_normalize_path(url)}|{oracle_name}")

        if (session / "html-report" / "report.html").exists():
            has_report = True
        data.append({"session": session.name, "passed": passed, "failed": failed, "vulns": len(vuln_sigs)})

    return jsonify({"sessions": data, "has_report": has_report})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
