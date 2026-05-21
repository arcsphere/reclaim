import subprocess
import threading
import queue
import os
import tempfile
from flask import Flask, request, Response, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import PyPDF2

# ── Load config from .env in same folder as app.py ──────────
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GEMINI_CMD       = os.getenv("GEMINI_CMD", "gemini")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
TOOLUNIVERSE_DIR = os.path.expanduser(os.getenv("TOOLUNIVERSE_DIR", ""))
FLASK_PORT       = int(os.getenv("FLASK_PORT", 5001))
PDF_MAX_CHARS    = int(os.getenv("PDF_MAX_CHARS", 8000))

# ── Validate config on startup ───────────────────────────────
def validate_config():
    errors = []
    if not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY is not set in .env")
    if not TOOLUNIVERSE_DIR or not os.path.isdir(TOOLUNIVERSE_DIR):
        errors.append(f"TOOLUNIVERSE_DIR does not exist: '{TOOLUNIVERSE_DIR}'")
    if errors:
        print("\n⚠️  CONFIG ERRORS:")
        for e in errors:
            print(f"   ✗ {e}")
        print("   → Fix these in your .env file\n")
    else:
        print(f"\n✅ Config loaded:")
        print(f"   GEMINI_CMD       = {GEMINI_CMD}")
        print(f"   TOOLUNIVERSE_DIR = {TOOLUNIVERSE_DIR}")
        print(f"   FLASK_PORT       = {FLASK_PORT}")
        print(f"   PDF_MAX_CHARS    = {PDF_MAX_CHARS}\n")

app = Flask(__name__)
CORS(app)


def extract_text_from_pdf(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name
    try:
        reader = PyPDF2.PdfReader(tmp_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text[:PDF_MAX_CHARS]
    finally:
        os.unlink(tmp_path)


def build_prompt(paper_text):
    return f"""You are a rigorous scientific research validator with access to scientific literature tools.

TASK: Validate the research paper below. Work through it systematically using your tools.

STEP 1 — Extract the top 5 key claims or findings from the paper.
STEP 2 — For each claim, generate a micro-question that precisely tests it.
STEP 3 — Use your available tools (literature search, web search, data lookup) to attempt to answer each micro-question independently. Do NOT rely on the paper itself for verification.
STEP 4 — Score coherence across: Internal Consistency, Methodology Clarity, Claim Support, Citation Quality, Logical Flow.
STEP 5 — Flag zones where AI verification is impossible (proprietary data, custom simulations, unpublished metrics).

ANTI-HALLUCINATION RULES:
- If you cannot verify a claim with tools, explicitly state "No tool evidence found". Do NOT fabricate.
- Never invent citations or statistics.
- Status for each micro-question must be one of: verified / partially-verified / unverifiable / contradicted

After completing all steps using your tools, output a final JSON block in this exact format:

```json
{{
  "title": "paper title",
  "domain": "research domain",
  "claims": [
    {{ "id": 1, "text": "claim text", "type": "empirical|theoretical|methodological", "verifiable": true }}
  ],
  "microQuestions": [
    {{ "id": 1, "question": "question", "targetClaim": 1, "difficulty": "low|medium|high", "answer": "what tools found", "status": "verified|partially-verified|unverifiable|contradicted" }}
  ],
  "coherenceScores": [
    {{ "aspect": "Internal Consistency", "score": 85, "reasoning": "brief reason" }}
  ],
  "hallucinationFlags": [
    {{ "zone": "zone name", "risk": "low|medium|high", "reason": "why AI cannot verify this" }}
  ],
  "overallCoherence": 85,
  "summary": "2-3 sentence assessment"
}}
```

PAPER TEXT:
{paper_text}

Begin now. Use tools first, then output the JSON block at the end."""


def stream_gemini(prompt, output_queue):
    cmd = [GEMINI_CMD, "--yolo", "-p", prompt]

    env = os.environ.copy()
    env["HOME"]           = os.path.expanduser("~")
    env["GEMINI_API_KEY"] = GEMINI_API_KEY

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=TOOLUNIVERSE_DIR
        )
        for line in iter(proc.stdout.readline, ""):
            output_queue.put(line)
        proc.wait()
        if proc.returncode != 0:
            output_queue.put(f"\n[EXIT CODE {proc.returncode}]\n")
    except FileNotFoundError:
        output_queue.put(f"ERROR: gemini CLI not found at '{GEMINI_CMD}'. Check GEMINI_CMD in .env\n")
    except Exception as e:
        output_queue.put(f"ERROR: {str(e)}\n")
    finally:
        output_queue.put(None)


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        paper_text = extract_text_from_pdf(request.files["file"].read())
    except Exception as e:
        return jsonify({"error": f"PDF extraction failed: {e}"}), 500

    prompt = build_prompt(paper_text)
    output_queue = queue.Queue()
    thread = threading.Thread(target=stream_gemini, args=(prompt, output_queue))
    thread.daemon = True
    thread.start()

    def event_stream():
        while True:
            line = output_queue.get()
            if line is None:
                yield "data: [DONE]\n\n"
                break
            yield f"data: {line.rstrip()}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "gemini_cmd": GEMINI_CMD,
        "tooluniverse_dir": TOOLUNIVERSE_DIR,
        "port": FLASK_PORT
    })


if __name__ == "__main__":
    validate_config()
    app.run(debug=True, port=FLASK_PORT, threaded=True)