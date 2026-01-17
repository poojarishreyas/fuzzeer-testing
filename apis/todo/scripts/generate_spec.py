"""
Reads the Todo API source code and uses Gemini 2.5 Flash
to automatically generate an OpenAPI 3.0 spec for it.
"""

import json
import re
import sys
import google.generativeai as genai

API_KEY = "AIzaSyA2DWCbQ0E49p9gr6Mp9PtGDMhZSoO-8n8"
SERVER_FILE = "/home/shreyas/Pictures/64-bit/RestTestGen/apis/todo/scripts/todo_server.py"
OUTPUT_FILE = "/home/shreyas/Pictures/64-bit/RestTestGen/apis/todo/specifications/openapi.json"

def read_server_code():
    with open(SERVER_FILE, "r") as f:
        return f.read()

def generate_spec(server_code):
    genai.configure(api_key=API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = f"""
You are an OpenAPI specification expert.

Below is the full source code of a Python Flask REST API running on http://localhost:8081.

Analyze the code carefully and generate a complete, valid OpenAPI 3.0.1 specification in JSON format.

Requirements:
- Include all endpoints, HTTP methods, parameters (path, query, body), and responses
- For every parameter include: name, in, required, description, and schema with type and format
- For every response include: status code, description, and response body schema
- Define reusable schemas under components/schemas
- Use $ref to reference schemas where appropriate
- Include realistic example values for every parameter and schema field
- The server url must be http://localhost:8081
- Output ONLY the raw JSON — no markdown, no code blocks, no explanation

Flask API source code:
```python
{server_code}
```

Return only the JSON object starting with {{ and ending with }}.
"""

    print("Sending code to Gemini 2.5 Flash...")
    response = model.generate_content(prompt)
    return response.text.strip()

def clean_json(raw):
    # Strip markdown code fences if Gemini wraps it anyway
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    return raw.strip()

def main():
    server_code = read_server_code()
    print(f"Read {len(server_code)} chars from todo_server.py")

    raw = generate_spec(server_code)
    cleaned = clean_json(raw)

    # Validate it's real JSON
    try:
        spec = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"ERROR: Gemini returned invalid JSON: {e}")
        print("Raw output:")
        print(raw[:500])
        sys.exit(1)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(spec, f, indent=2)

    print(f"\nSpec saved to: {OUTPUT_FILE}")
    print(f"Endpoints found: {list(spec.get('paths', {}).keys())}")
    print("\nDone! You can now run RestTestGen against the Todo API.")

if __name__ == "__main__":
    main()
