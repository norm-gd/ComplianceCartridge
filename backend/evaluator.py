"""
evaluator.py — ComplianceCartridge AI inference layer.

Compliance engine: fully agnostic to any normative standard. All domain
knowledge arrives via the `control` dict (the "cartridge") parsed upstream
by main.py from /standards/*.json files. This module only knows how to
read the cartridge and call Ollama.

Imported by main.py:
    from evaluator import evaluate_control
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from schemas import AuditRequest, AuditResponse, AuditResult as SchemaAuditResult
from ingestion import query_evidence

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration. Every value can be overridden via environment variables so
# the pipeline can be tuned for low-RAM machines without code changes.
# ---------------------------------------------------------------------------

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))  # seconds

# Passed to Ollama as the payload `options` block. Shrinking num_ctx cuts the
# KV-cache RAM during inference; num_thread caps CPU so the machine stays
# responsive; keep_alive controls how long the model stays resident in memory.
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "4096"))
LLM_NUM_THREAD = int(os.getenv("LLM_NUM_THREAD", "0"))  # 0 = let Ollama decide
LLM_KEEP_ALIVE = os.getenv("LLM_KEEP_ALIVE", "5m")

# Opt-in low-RAM pacing: inserts a short pause between control evaluations so
# the OS gets breathing room and the box stays usable for other tasks.
LOW_RAM_MODE = os.getenv("COMPLIANCE_LOW_RAM", "").strip().lower() in ("1", "true", "yes", "on")
LOW_RAM_PAUSE_SECONDS = float(os.getenv("COMPLIANCE_LOW_RAM_PAUSE", "1.0"))


def _llm_options() -> dict:
    """Build the Ollama ``options`` block from the tunable config above."""
    opts: dict = {
        "num_ctx": LLM_NUM_CTX,
        "keep_alive": LLM_KEEP_ALIVE,
    }
    if LLM_NUM_THREAD > 0:
        opts["num_thread"] = LLM_NUM_THREAD
    return opts

AUDIT_SYSTEM_PROMPT = """
You are a Senior Normative Auditor with over 20 years of experience at a Big 4 consulting 
firm, specialising in regulatory compliance, internal controls, and corporate governance f
rameworks.

Your task is to evaluate whether the EVIDENCE provided by the user satisfies the RULE TO E
VALUATE for the given control. You will be given the standard name, control identifier, co
ntrol title, the rule to evaluate, and the evidence retrieved from company documents.

CRITICAL RULES — follow without exception:
1. Base your entire analysis EXCLUSIVELY on the evidence text supplied in the user message
. You must NOT infer, assume, extrapolate, or use any external knowledge, industry benchma
rks, or information not explicitly present in the provided evidence.
2. CRITICAL: Evaluate the evidence SEMANTICALLY and FLEXIBLY, not strictly literally. If the 
document uses synonyms, specific examples, or reasonable variations (e.g., 'portal' instead 
of 'system', or 'ID card' instead of 'RFID badge'), you MUST consider it COMPLIANT. 
Understand the broader business context and intent.
3. If the evidence is empty, absent, vague, or insufficient to demonstrate compliance, you
 MUST output status "Non-Compliant".
4. If the evidence shows intent or partial implementation but lacks proof of full complian
ce, you MUST output status "Partial".
5. Only output "Compliant" when the evidence directly (or via clear semantic equivalence) 
demonstrates that every aspect of the rule is satisfied.
6. When in doubt, rate down: prefer "Non-Compliant" over "Partial", and "Partial" over "Co
mpliant".
7. Every value for "evidence_found" and "gaps" MUST be derived verbatim or directly paraph
rased from the supplied evidence text. You must NEVER fabricate or invent content for any 
field.
8. You MUST respond with a single, valid JSON object and NOTHING ELSE. No preamble, no exp
lanation, no markdown fences. The response must begin with '{' and end with '}'.

OUTPUT SCHEMA (strict — include all four keys, no extras):
{
  "status": "Compliant" | "Non-Compliant" | "Partial",
  "evidence_found": "<Direct quotes or close paraphrases from the evidence that support co
mpliance. Write 'None' if no supporting evidence exists.>",
  "gaps": "<Specific deficiencies in the evidence relative to what the rule requires. Writ
e 'None' if the control is fully satisfied.>",
  "recommendation": "<A single, concrete, actionable remediation step. Write 'None' if the
 control is fully satisfied.>"
}
""".strip()

# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class AuditResult(BaseModel):
    status: Literal["Compliant", "Non-Compliant", "Partial"]
    evidence_found: str
    gaps: str
    recommendation: str


# ---------------------------------------------------------------------------
# Fallback returned when inference fails after one retry
# ---------------------------------------------------------------------------

_FALLBACK: dict = {
    "status": "Non-Compliant",
    "evidence_found": "Evaluation could not be completed.",
    "gaps": "Internal error during LLM inference or response parsing.",
    "recommendation": "Check Ollama service status and model availability.",
    "_error": True,
}

# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def evaluate_control(
    standard_name: str,
    control: dict,
    chromadb_evidence: str,
) -> dict:
    """Evaluate a normative control against evidence retrieved from ChromaDB.

    Args:
        standard_name:     Human-readable name of the normative standard being
                           audited, e.g. "ISO 27001:2022" or "SOC 2 Type II".
                           Comes from the parent standard JSON, not hardcoded.
        control:           Full control dict parsed from a /standards/*.json
                           cartridge. Expected keys: "control_id", "title",
                           "description". Any extra keys are ignored.
        chromadb_evidence: Pre-retrieved evidence string assembled by
                           ingestion.py from ChromaDB using the control's
                           search_keywords. May be empty if no chunks matched.

    Returns:
        A dict with keys ``status``, ``evidence_found``, ``gaps``, and
        ``recommendation``. On unrecoverable failure an additional
        ``_error: True`` key is present and ``status`` is forced to
        "Non-Compliant".
    """
    user_message = (
        f"STANDARD: {standard_name}\n"
        f"CONTROL ID: {control['control_id']}\n"
        f"CONTROL TITLE: {control['title']}\n"
        f"RULE TO EVALUATE: {control['description']}\n"
        f"EVIDENCE FOUND IN COMPANY DOCUMENTS:\n{chromadb_evidence}\n\n"
        "Evaluate strictly using ONLY the evidence above. Respond in JSON."
    )

    full_prompt = f"{AUDIT_SYSTEM_PROMPT}\n\n{user_message}"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": full_prompt,
        "format": "json",
        "stream": False,
        "options": _llm_options(),
    }

    def _call_and_parse() -> dict:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            response = client.post(OLLAMA_URL, json=payload)
            response.raise_for_status()

        raw: str = response.json()["response"]
        parsed = json.loads(raw)
        audit_result = AuditResult.model_validate(parsed)
        return audit_result.model_dump()

    try:
        return _call_and_parse()
    except (httpx.HTTPError, json.JSONDecodeError, ValidationError, KeyError) as exc:
        logger.warning(
            "evaluate_control: attempt 1 failed [%s %s] — retrying",
            control.get("control_id", "unknown"),
            type(exc).__name__,
        )

    try:
        return _call_and_parse()
    except Exception as exc:
        logger.error(
            "evaluate_control: retry failed [%s %s] — returning fallback",
            control.get("control_id", "unknown"),
            type(exc).__name__,
        )
        return _FALLBACK.copy()


# ---------------------------------------------------------------------------
# Risk mapping helper
# ---------------------------------------------------------------------------

_STATUS_TO_RISK: dict[str, Literal["High", "Medium", "Low", "None"]] = {
    "Compliant":     "None",
    "Partial":       "Medium",
    "Non-Compliant": "High",
}

_STATUS_TO_SCORE: dict[str, float] = {
    "Compliant":     1.0,
    "Partial":       0.5,
    "Non-Compliant": 0.0,
}

# ---------------------------------------------------------------------------
# Public API — consumed by main.py
# ---------------------------------------------------------------------------


def check_llm_alive() -> None:
    """Raise RuntimeError if the Ollama LLM endpoint is not reachable."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get("http://localhost:11434/api/tags")
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Ollama LLM not reachable: {exc}") from exc


SUMMARY_SYSTEM_PROMPT_TEMPLATE = (
    "You are a Senior Cybersecurity Auditor. Your task is to summarise the company's "
    "compliance posture based on the findings provided. Be professional, focus on "
    "critical risks, and finish with an overall conclusion. "
    "Write the entire response in {language}. "
    "Use at most 3 paragraphs, plain text only — no markdown, no bullet lists, no "
    "headings. Do not include any preamble such as 'Sure' or 'Here is the summary'; "
    "output only the summary itself."
)


def generate_executive_summary(
    standard_audited: str,
    global_score_percentage: float,
    findings: list[dict],
    language: str = "English",
) -> str:
    """Call Llama 3.1 to produce a 3-paragraph executive summary of an audit.

    Args:
        standard_audited:         Name of the standard that was audited.
        global_score_percentage:  Overall compliance score (0–100).
        findings:                 List of finding dicts (control_id, status,
                                  evidence_found, gaps, recommendation,
                                  risk_level).

    Returns:
        Plain-text summary string. Raises RuntimeError on inference failure.
    """
    findings_block_lines: list[str] = []
    for f in findings:
        findings_block_lines.append(
            f"- [{f.get('control_id', '?')}] {f.get('status', '?')} "
            f"(risk: {f.get('risk_level', '?')}) — "
            f"gaps: {f.get('gaps', 'None') or 'None'} | "
            f"recommendation: {f.get('recommendation', 'None') or 'None'}"
        )
    findings_block = "\n".join(findings_block_lines) if findings_block_lines else "No findings."

    lang = (language or "English").strip() or "English"
    system_prompt = SUMMARY_SYSTEM_PROMPT_TEMPLATE.format(language=lang)

    user_message = (
        f"AUDITED STANDARD: {standard_audited}\n"
        f"GLOBAL SCORE: {global_score_percentage:.1f}%\n"
        f"FINDINGS:\n{findings_block}\n\n"
        f"Write the executive summary in {lang}, following the system instructions."
    )

    full_prompt = f"{system_prompt}\n\n{user_message}"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": full_prompt,
        "stream": False,
        "options": _llm_options(),
    }

    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            response = client.post(OLLAMA_URL, json=payload)

            response.raise_for_status()
        text: str = response.json()["response"].strip()
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
        logger.exception("generate_executive_summary failed")
        raise RuntimeError(f"LLM summary generation failed: {exc}") from exc

    if not text:
        raise RuntimeError("LLM returned an empty summary.")

    return text


def run_audit(payload: AuditRequest) -> AuditResponse:
    """Evaluate every control in *payload* and return a full AuditResponse.

    For each control: retrieves evidence from ChromaDB via query_evidence,
    calls evaluate_control, maps the raw result to SchemaAuditResult, and
    collects everything into an AuditResponse.

    The global score is calculated using the weighted formula:
        score = sum(weight_i * score_i) / sum(weight_i) * 100
    where score_i is 1.0 for Compliant, 0.5 for Partial, 0.0 for Non-Compliant.
    """
    findings: list[SchemaAuditResult] = []
    total = len(payload.controls)

    for i, control in enumerate(payload.controls):
        evidence = query_evidence(control.search_keywords)
        raw = evaluate_control(
            standard_name=payload.standard_name,
            control=control.model_dump(),
            chromadb_evidence=evidence,
        )
        score = _STATUS_TO_SCORE.get(raw["status"], 0.0)
        findings.append(SchemaAuditResult(
            control_id=control.control_id,
            status=raw["status"],
            evidence_found=raw["evidence_found"],
            gaps=raw["gaps"],
            recommendation=raw["recommendation"],
            risk_level=_STATUS_TO_RISK.get(raw["status"], "High"),
            score=score,
        ))

        if LOW_RAM_MODE and i < total - 1:
            logger.debug(
                "LOW_RAM_MODE: pausing %.1fs before next control.",
                LOW_RAM_PAUSE_SECONDS,
            )
            time.sleep(LOW_RAM_PAUSE_SECONDS)

    total_weight = sum(c.weight for c in payload.controls)
    weighted_sum = sum(
        payload.controls[i].weight * findings[i].score
        for i in range(len(payload.controls))
    )
    score = round(weighted_sum / total_weight * 100, 1) if total_weight else 0.0

    return AuditResponse(
        standard_audited=payload.standard_name,
        global_score_percentage=score,
        results=findings,
    )


# ---------------------------------------------------------------------------
# Smoke test — mirrors the main.py call pattern
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    # Simulate a cartridge control being passed in by main.py
    test_control = {
        "control_id": "A.5.1",
        "title": "Information Security Policies",
        "description": (
            "Security policies must be defined, approved by management, "
            "published and communicated to all employees."
        ),
        "search_keywords": ["security policy", "approval", "communication", "management"],
    }

    test_evidence = """
        Document: Employee Handbook v3.2 (2024-01-10)
        Section 4.1 states: 'All information security policies were reviewed and
        formally approved by the CTO on December 15, 2023. Policies are published
        on the internal wiki at security.acme.com.' HR records show 287 of 310
        employees completed the mandatory policy acknowledgment form. 23 employees
        hired after December have not yet completed it.
    """

    print("\n=== ComplianceCartridge Smoke Test — evaluate_control ===")
    result = evaluate_control("ISO 27001:2022", test_control, test_evidence)
    pprint.pprint(result)
