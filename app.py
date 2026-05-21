import subprocess
import threading
import queue
import os
import json
import csv
import io
import tempfile
from flask import Flask, request, Response, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import PyPDF2

# ── Load config from .env in same folder as app.py ──────────
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GEMINI_CMD = os.getenv("GEMINI_CMD", "gemini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TOOLUNIVERSE_DIR = os.path.expanduser(os.getenv("TOOLUNIVERSE_DIR", ""))
FLASK_PORT = int(os.getenv("FLASK_PORT", 5001))
PDF_MAX_CHARS = int(os.getenv("PDF_MAX_CHARS", 8000))
COMPARE_TEXT_MAX_CHARS = int(os.getenv("COMPARE_TEXT_MAX_CHARS", 16000))
PLAINTEXT_MAX_CHARS = int(os.getenv("PLAINTEXT_MAX_CHARS", 12000))
CSV_MAX_ROWS = int(os.getenv("CSV_MAX_ROWS", 100))
CSV_CELL_MAX_CHARS = int(os.getenv("CSV_CELL_MAX_CHARS", 600))

# ── Validate config on startup ───────────────────────────────
def validate_config():
    errors = []
    if not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY is not set in .env")
    if not TOOLUNIVERSE_DIR or not os.path.isdir(TOOLUNIVERSE_DIR):
        errors.append(f"TOOLUNIVERSE_DIR does not exist: '{TOOLUNIVERSE_DIR}'")

    if errors:
        print("\n⚠️ CONFIG ERRORS:")
        for e in errors:
            print(f" ✗ {e}")
        print(" → Fix these in your .env file\n")
    else:
        print(f"\n✅ Config loaded:")
        print(f" GEMINI_CMD = {GEMINI_CMD}")
        print(f" TOOLUNIVERSE_DIR = {TOOLUNIVERSE_DIR}")
        print(f" FLASK_PORT = {FLASK_PORT}")
        print(f" PDF_MAX_CHARS = {PDF_MAX_CHARS}")
        print(f" COMPARE_TEXT_MAX_CHARS = {COMPARE_TEXT_MAX_CHARS}")
        print(f" PLAINTEXT_MAX_CHARS = {PLAINTEXT_MAX_CHARS}")
        print(f" CSV_MAX_ROWS = {CSV_MAX_ROWS}\n")


app = Flask(__name__)
CORS(app)


# ── Helpers ─────────────────────────────────────────────────
def truncate_text(text, limit):
    text = text or ""
    return text[:limit]


def extract_text_from_pdf(pdf_bytes, max_chars=PDF_MAX_CHARS):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name
    try:
        reader = PyPDF2.PdfReader(tmp_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
            if len(text) >= max_chars:
                break
        return text[:max_chars]
    finally:
        os.unlink(tmp_path)


def extract_text_from_plain_bytes(file_bytes, filename="", max_chars=PLAINTEXT_MAX_CHARS):
    if not file_bytes:
        return ""

    name = (filename or "").lower()
    raw = file_bytes.decode("utf-8", errors="replace")

    if name.endswith(".json"):
        try:
            obj = json.loads(raw)
            pretty = json.dumps(obj, indent=2, ensure_ascii=False)
            return pretty[:max_chars]
        except Exception:
            return raw[:max_chars]

    return raw[:max_chars]


def normalize_text(s):
    return " ".join((s or "").strip().split())


def safe_json_loads(s, default):
    try:
        return json.loads(s)
    except Exception:
        return default


def dedupe_questions(questions):
    seen = set()
    out = []
    for q in questions:
        qn = normalize_text(q)
        if not qn:
            continue
        key = qn.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(qn)
    return out


def bool_from_form(name, default=False):
    val = request.form.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def clamp_cell(text, limit=CSV_CELL_MAX_CHARS):
    text = normalize_text(text)
    return text[:limit]


def parse_csv_rows(file_storage, max_rows=CSV_MAX_ROWS):
    if not file_storage:
        return []

    raw = file_storage.read()
    text = raw.decode("utf-8", errors="replace")
    buf = io.StringIO(text)

    try:
        sample = text[:2048]
        dialect = csv.Sniffer().sniff(sample)
        buf.seek(0)
        reader = csv.DictReader(buf, dialect=dialect)
    except Exception:
        buf.seek(0)
        reader = csv.DictReader(buf)

    rows = []
    for i, row in enumerate(reader):
        if i >= max_rows:
            break
        cleaned = {}
        for k, v in (row or {}).items():
            key = normalize_text(k).lower()
            cleaned[key] = clamp_cell(v)
        rows.append(cleaned)

    return rows


def extract_questions_from_csv_rows(rows):
    question_keys = [
        "question", "questions", "microquestion", "micro_question", "prompt", "query"
    ]
    questions = []
    for row in rows:
        for key in question_keys:
            if row.get(key):
                questions.append(row[key])
                break
    return dedupe_questions(questions)


def summarize_csv_rows(rows):
    if not rows:
        return "No CSV rows provided."

    question_keys = ["question", "questions", "microquestion", "micro_question", "prompt", "query"]
    answer_keys = [
        "provided_answer", "answer", "answers", "extracted_answer",
        "ai_answer", "target_answer", "response"
    ]
    meta_keys = ["student_id", "file_id", "id", "artifact", "artifact_type"]

    lines = []
    for idx, row in enumerate(rows[:CSV_MAX_ROWS], start=1):
        q = ""
        a = ""
        meta = []

        for key in question_keys:
            if row.get(key):
                q = row[key]
                break

        for key in answer_keys:
            if row.get(key):
                a = row[key]
                break

        for key in meta_keys:
            if row.get(key):
                meta.append(f"{key}={row[key]}")

        line = f"{idx}. "
        if meta:
            line += "[" + ", ".join(meta) + "] "
        line += f"Q: {q or '(none)'}"
        if a:
            line += f" | Provided Answer: {a}"
        lines.append(line)

    return "\n".join(lines)


def merge_question_sources(frontend_questions, csv_rows):
    csv_questions = extract_questions_from_csv_rows(csv_rows)
    return dedupe_questions((frontend_questions or []) + csv_questions)


def read_intermediate_files(files, max_chars_each=PLAINTEXT_MAX_CHARS):
    extracted = []
    for f in files:
        filename = f.filename or "unnamed"
        try:
            content = extract_text_from_plain_bytes(f.read(), filename, max_chars_each)
        except Exception as e:
            content = f"[Failed to read {filename}: {e}]"
        extracted.append({
            "filename": filename,
            "content": content
        })
    return extracted


# ── Prompt builders ─────────────────────────────────────────
def build_prompt(paper_text, custom_questions=None, extra_instructions=""):
    custom_q_block = ""
    if custom_questions:
        custom_q_block = "\nADDITIONAL CUSTOM QUESTIONS (added by user — answer these too, mark isCustom: true in JSON):\n"
        custom_q_block += "\n".join(f"- {q}" for q in custom_questions)
        custom_q_block += "\n"

    extra_block = f"\n{extra_instructions}\n" if extra_instructions else ""

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
- If a tool call fails or returns an error, skip it silently and try an alternative tool. Do NOT stop.
- ToolUniverse is running in compact mode. Only 4 tools are directly exposed: mcp_tooluniverse_grep_tools, mcp_tooluniverse_list_tools, mcp_tooluniverse_get_tool_info, mcp_tooluniverse_execute_tool.
- To use any scientific tool (PubMed, ArXiv, web search etc), you MUST call them via mcp_tooluniverse_execute_tool like this: mcp_tooluniverse_execute_tool(tool_name="web_search", tool_input={{"query": "your search query"}})
- To find the right tool name and input format, first call mcp_tooluniverse_list_tools or mcp_tooluniverse_grep_tools(pattern="search") to discover available tools.
- Example calls:
mcp_tooluniverse_execute_tool(tool_name="web_search", tool_input={{"query": "paper title here"}})
mcp_tooluniverse_execute_tool(tool_name="PubMed_search_articles", tool_input={{"query": "topic here"}})
mcp_tooluniverse_execute_tool(tool_name="ArXiv_search_papers", tool_input={{"query": "topic here"}})
- Never call scientific tools directly by name — always route through mcp_tooluniverse_execute_tool.

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
PAPER TEXT:
{paper_text}
{custom_q_block}{extra_block}
Begin now. Use tools first, then output the JSON block at the end."""

def build_compare_prompt(
source_text,
target_text,
questions,
claims_text="",
csv_rows=None,
intermediate_files=None,
use_claims=True,
include_intermediate=True,
compare_provided_answers=True,
fast_compare=True,
):
    csv_rows = csv_rows or []
    intermediate_files = intermediate_files or []

    question_block = "\n".join(f"- {q}" for q in questions) if questions else "- No explicit questions provided"
    claims_block = claims_text.strip() if (use_claims and claims_text.strip()) else "No claims file provided."
    csv_block = summarize_csv_rows(csv_rows) if compare_provided_answers and csv_rows else "No CSV answer rows provided."
    intermediate_block = ""

    if include_intermediate and intermediate_files:
        chunks = []
        for item in intermediate_files:
            chunks.append(f"FILE: {item['filename']}\n{item['content']}")
        intermediate_block = "\n\n".join(chunks)
    else:
        intermediate_block = "No intermediate files provided."

    mode_line = (
        "Prefer lightweight retrieval/comparison reasoning first. Only use heavier semantic reasoning when needed."
        if fast_compare else
        "Use full semantic reasoning when necessary to resolve ambiguity."
    )

    return f"""You are a rigorous research fidelity evaluator.

TASK:
Compare a SOURCE PDF against a TARGET PDF using a unified question set Q.
Your goal is to determine how faithfully the target preserves the source's claims, answers, details, caveats, and research meaning.

PRIMARY GOALS:

For each question in Q, determine the best evidence-based answer from the SOURCE PDF.
For each question in Q, determine the best evidence-based answer from the TARGET PDF.
If CSV-provided answers exist, compare them against the extracted source/target answers.
If intermediate files exist, use them as supporting evidence to identify where information may have been preserved, altered, or lost.
Identify:
match
partial-match
missing-in-target
missing-in-source
mismatch
contradicted
unverifiable
RULES:

Do NOT use external literature tools for this mode unless absolutely necessary; this is primarily a source-vs-target fidelity task.
Ground your answers in the provided artifacts only.
Be conservative. If an answer is unclear, say so.
A wording difference is NOT automatically a mismatch if the meaning is preserved.
If the target omits an important qualifier, limitation, uncertainty, or methodological detail, note that in the comparison notes.
If the CSV contains provided answers, compare them where useful, but do not blindly trust them.
{mode_line}
OUTPUT:
Return one final JSON block in exactly this shape:

{{
"summary": "2-4 sentence overall assessment",
"fidelityScore": 78,
"scoreBreakdown": [
{{ "aspect": "Claim Preservation", "score": 84, "reasoning": "brief reason" }},
{{ "aspect": "Method Fidelity", "score": 69, "reasoning": "brief reason" }},
{{ "aspect": "Answer Consistency", "score": 75, "reasoning": "brief reason" }}
],
"gaps": [
"important missing or distorted point"
],
"claimCoverage": [
{{ "claim": "claim text", "status": "preserved|partial|missing|distorted|unsupported" }}
],
"comparisons": [
{{
"question": "question text",
"sourceAnswer": "best answer from source",
"targetAnswer": "best answer from target",
"providedAnswer": "answer from CSV if available, else empty string",
"intermediateAnswer": "intermediate evidence if relevant, else empty string",
"status": "match|partial-match|missing-in-target|missing-in-source|mismatch|contradicted|unverifiable",
"notes": "short explanation"
}}
]
}}

QUESTION SET Q:
{question_block}

CLAIMS FILE:
{claims_block}

CSV ROWS:
{csv_block}

INTERMEDIATE FILES:
{intermediate_block}

SOURCE PDF TEXT:
{source_text}

TARGET PDF TEXT:
{target_text}

Begin now. Think carefully, compare systematically, and output only the JSON block at the end after your analysis."""

def stream_gemini(prompt, output_queue, cwd=None):
    cmd = [GEMINI_CMD, "--yolo", "-p", prompt]

    env = os.environ.copy()
    env["HOME"] = os.path.expanduser("~")
    env["GEMINI_API_KEY"] = GEMINI_API_KEY

    run_cwd = cwd or TOOLUNIVERSE_DIR or os.path.dirname(__file__)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=run_cwd
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

def make_sse_response_for_prompt(prompt, cwd=None):
    output_queue = queue.Queue()
    thread = threading.Thread(target=stream_gemini, args=(prompt, output_queue, cwd))
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

@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    custom_questions = safe_json_loads(request.form.get("customQuestions", "[]"), [])
    extra_instructions = request.form.get("extraInstructions", "")

    try:
        paper_text = extract_text_from_pdf(
            request.files["file"].read(),
            max_chars=PDF_MAX_CHARS
        )
    except Exception as e:
        return jsonify({"error": f"PDF extraction failed: {e}"}), 500

    prompt = build_prompt(paper_text, custom_questions, extra_instructions)
    return make_sse_response_for_prompt(prompt, cwd=TOOLUNIVERSE_DIR)

@app.route("/compare", methods=["POST"])
def compare():
    if "sourcePdf" not in request.files or "targetPdf" not in request.files:
        return jsonify({"error": "Both sourcePdf and targetPdf are required"}), 400

    source_pdf = request.files["sourcePdf"]
    target_pdf = request.files["targetPdf"]
    claims_file = request.files.get("claimsFile")
    csv_file = request.files.get("csvFile")
    intermediate_files = request.files.getlist("intermediateFiles")

    frontend_questions = safe_json_loads(request.form.get("questions", "[]"), [])
    use_claims = bool_from_form("useClaims", True)
    include_intermediate = bool_from_form("includeIntermediate", True)
    compare_provided_answers = bool_from_form("compareProvidedAnswers", True)
    fast_compare = bool_from_form("fastCompare", True)

    try:
        source_text = extract_text_from_pdf(source_pdf.read(), max_chars=COMPARE_TEXT_MAX_CHARS)
    except Exception as e:
        return jsonify({"error": f"Source PDF extraction failed: {e}"}), 500

    try:
        target_text = extract_text_from_pdf(target_pdf.read(), max_chars=COMPARE_TEXT_MAX_CHARS)
    except Exception as e:
        return jsonify({"error": f"Target PDF extraction failed: {e}"}), 500

    claims_text = ""
    if claims_file and claims_file.filename:
        try:
            claims_text = extract_text_from_plain_bytes(
                claims_file.read(),
                claims_file.filename,
                max_chars=PLAINTEXT_MAX_CHARS
            )
        except Exception as e:
            return jsonify({"error": f"Claims file read failed: {e}"}), 500

    csv_rows = []
    if csv_file and csv_file.filename:
        try:
            csv_rows = parse_csv_rows(csv_file)
        except Exception as e:
            return jsonify({"error": f"CSV parse failed: {e}"}), 500

    extracted_intermediate = []
    if intermediate_files:
        try:
            extracted_intermediate = read_intermediate_files(intermediate_files, max_chars_each=PLAINTEXT_MAX_CHARS)
        except Exception as e:
            return jsonify({"error": f"Intermediate file read failed: {e}"}), 500

    merged_questions = merge_question_sources(frontend_questions, csv_rows)

    prompt = build_compare_prompt(
        source_text=source_text,
        target_text=target_text,
        questions=merged_questions,
        claims_text=claims_text,
        csv_rows=csv_rows,
        intermediate_files=extracted_intermediate,
        use_claims=use_claims,
        include_intermediate=include_intermediate,
        compare_provided_answers=compare_provided_answers,
        fast_compare=fast_compare,
    )


    return make_sse_response_for_prompt(prompt, cwd=os.path.dirname(__file__))
    
@app.route("/pitch")
def pitch():
    return send_from_directory(os.path.dirname(__file__), "pitch.html")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "gemini_cmd": GEMINI_CMD,
        "tooluniverse_dir": TOOLUNIVERSE_DIR,
        "port": FLASK_PORT,
        "pdf_max_chars": PDF_MAX_CHARS,
        "compare_text_max_chars": COMPARE_TEXT_MAX_CHARS,
        "plaintext_max_chars": PLAINTEXT_MAX_CHARS
    })

if __name__ == "__main__":
    validate_config()
    app.run(debug=True, port=FLASK_PORT, threaded=True)