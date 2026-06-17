#!/usr/bin/env python3
"""LangSmith PII Leakage Evaluator and Trace Scanner.

This script demonstrates how to set up, configure, and run a PII evaluator with
LangSmith to detect when sensitive data (like real names, email addresses, or
phone numbers) leaks into LLM prompts or outputs.

It provides:
1. A python-level LangSmith Evaluator using the service's Presidio engine.
2. A CLI scanner to pull recent runs from LangSmith and flag any PII leaks.
3. A template for a Custom Code Evaluator to be pasted into the LangSmith Web UI.

Usage:
    # Set your API keys in .env, then run:
    .venv/bin/python scripts/langsmith_pii_evaluator.py --limit 10
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from typing import Any, Iterator

# Put the service root on sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from langsmith import Client
from langsmith.schemas import Run, Example
from pipelines import extract, load_models, models_loaded

# Regex for common PII patterns (used as a fallback or in sandboxed environments)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
# Exclude known handle patterns (e.g., person001, company002) from leakage flags
HANDLE_RE = re.compile(r"\b(?:person|company|email|phone|location|url)\d{3,}\b")


def get_prose_strings(data: Any, key: str | None = None) -> Iterator[str]:
    """Recursively yield only prose strings, avoiding metadata keys like 'id', 'type', etc."""
    if isinstance(data, str):
        # 1. Skip system/metadata keys
        if key in ("id", "type", "lc", "role", "name", "field", "entity_type", "record_id", "status"):
            return
        # 2. Skip obvious serialization paths/classnames
        if "langchain" in data or "HumanMessage" in data or "SystemMessage" in data or "AIMessage" in data:
            return
        # 3. Skip single words without letters or short words without spaces (unless they are valid emails/phones)
        if len(data) < 3:
            return
        yield data
    elif isinstance(data, dict):
        # If it's a message dictionary, we primarily want to scan its content
        if "content" in data and isinstance(data["content"], str):
            yield data["content"]
            # Still traverse other fields but skip the 'id' and 'type' keys
            for k, val in data.items():
                if k != "content":
                    yield from get_prose_strings(val, key=k)
        else:
            for k, val in data.items():
                yield from get_prose_strings(val, key=k)
    elif isinstance(data, list):
        for item in data:
            yield from get_prose_strings(item, key=key)


def detect_pii_leaks(text: str) -> list[dict[str, Any]]:
    """Scan text for raw PII using the project's Presidio pipeline.
    
    Ignores valid entity handles (e.g. person001) so they are not flagged as leaks.
    """
    if not text:
        return []
    
    # 1. Use Presidio to extract PII
    entities = extract(text)
    leaks = []
    
    for entity in entities:
        label = entity["label"]
        val = entity["text"]
        
        # Ignore entity handles like 'person001' or 'email002'
        if HANDLE_RE.search(val):
            continue
            
        # If it's a person, company, email, or phone number, it's a leak!
        if label in ("person", "company", "email address", "phone number"):
            leaks.append({
                "type": label,
                "value": val,
                "score": entity["score"]
            })
            
    # 2. Supplementary regex check to catch any emails or phones missed by NER
    for email in EMAIL_RE.findall(text):
        if not any(leak["value"] == email for leak in leaks):
            leaks.append({"type": "email address (regex)", "value": email, "score": 1.0})
            
    for phone in PHONE_RE.findall(text):
        if not any(leak["value"] == phone for leak in leaks):
            leaks.append({"type": "phone number (regex)", "value": phone, "score": 1.0})
            
    return leaks


def pii_leak_evaluator(run: Run, example: Example | None = None) -> dict[str, Any]:
    """LangSmith Run Evaluator function to detect PII leakage.
    
    Can be used locally with the evaluate() SDK function.
    """
    # We want to scan the inputs sent to the LLM (prompts)
    all_texts = list(get_prose_strings(run.inputs))
    
    # Also inspect outputs to ensure the LLM didn't leak raw PII in its response
    if run.outputs:
        all_texts.extend(get_prose_strings(run.outputs))
        
    all_leaks = []
    for text in all_texts:
        all_leaks.extend(detect_pii_leaks(text))
        
    is_leaked = len(all_leaks) > 0
    score = 1.0 if is_leaked else 0.0  # 1.0 = Leak occurred, 0.0 = Safe/No leak
    
    comment = "No PII leakage detected."
    if is_leaked:
        leak_details = ", ".join(f"[{l['type']}] '{l['value']}'" for l in all_leaks[:5])
        if len(all_leaks) > 5:
            leak_details += f" (+{len(all_leaks)-5} more)"
        comment = f"PII Leakage detected: {leak_details}"
        
    return {
        "key": "pii_leakage",
        "score": score,
        "comment": comment
    }


def scan_recent_traces(project_name: str, limit: int = 20) -> None:
    """Fetch recent runs from LangSmith and check them for PII leakage."""
    print(f"Connecting to LangSmith project: '{project_name}'...")
    client = Client()
    
    # Fetch recent runs. We filter for run_type="llm" to target LLM prompts directly.
    print(f"Fetching last {limit} LLM runs...")
    runs = client.list_runs(
        project_name=project_name,
        run_type="llm",
        limit=limit
    )
    
    leak_count = 0
    total_scanned = 0
    
    print("-" * 80)
    for run in runs:
        total_scanned += 1
        eval_result = pii_leak_evaluator(run)
        
        if eval_result["score"] > 0:
            leak_count += 1
            print(f"\n[⚠️ LEAK DETECTED] Run ID: {run.id}")
            print(f"  Name: {run.name}")
            print(f"  Time: {run.start_time}")
            print(f"  Url:  {run.url}")
            print(f"  Detail: {eval_result['comment']}")
            
            # Print a snippet of what was sent to the LLM
            inputs_str = str(run.inputs)[:200] + "..."
            print(f"  Input Snippet: {inputs_str}")
            
    print("-" * 80)
    print(f"Scan complete. Scanned {total_scanned} runs. Found {leak_count} leaks.")


# ===========================================================================
# LANGSMITH WEB UI CUSTOM CODE EVALUATOR TEMPLATE
# ===========================================================================
UI_CODE_EVALUATOR_TEMPLATE = """
# copy-paste this code directly into the Custom Code Evaluator inside the LangSmith Web UI
# Go to your Project -> Rules -> + Add Rule -> Custom Code Evaluator or Dataset -> + Evaluator -> Code
import re

EMAIL_RE = re.compile(r"\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}\\b")
PHONE_RE = re.compile(r"\\b(?:\\+?\\d{1,3}[-.\\s]?)?\\(?\\d{3}\\)?[-\\s.]?\\d{3}[-\\s.]?\\d{4}\\b")
HANDLE_RE = re.compile(r"\\b(?:person|company|email|phone|location|url)\\d{3,}\\b")

# Regex to detect common capitalization pattern for names (e.g. John Doe, Sarah Connor)
# This is a lightweight heuristic for sandboxed python execution.
NAME_RE = re.compile(r"\\b[A-Z][a-z]+ [A-Z][a-z]+\\b")

def get_all_strings(data):
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for val in data.values():
            yield from get_all_strings(val)
    elif isinstance(data, list):
        for item in data:
            yield from get_all_strings(item)

def perform_eval(run: dict, example: dict = None) -> dict:
    all_texts = list(get_all_strings(run.get("inputs", {})))
    if run.get("outputs"):
        all_texts.extend(get_all_strings(run.get("outputs", {})))
        
    leaks = []
    for text in all_texts:
        # Find email leaks
        for email in EMAIL_RE.findall(text):
            leaks.append(f"[Email] {email}")
            
        # Find phone leaks
        for phone in PHONE_RE.findall(text):
            leaks.append(f"[Phone] {phone}")
            
        # Find potential person names that aren't masked handles
        for name in NAME_RE.findall(text):
            if not HANDLE_RE.search(name):
                # Simple exclusion of common non-person title-case phrases (optional)
                if name not in ("Integration Plan", "Budget Approved", "Platform Integration"):
                    leaks.append(f"[Name] {name}")
                    
    is_leaked = len(leaks) > 0
    score = 1.0 if is_leaked else 0.0 # 1 = leaked, 0 = clean
    
    return {
        "key": "pii_leakage",
        "score": score,
        "comment": f"Leaks found: {', '.join(leaks)}" if is_leaked else "Clean"
    }
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LangSmith PII leakage evaluation scanner.")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent runs to scan.")
    parser.add_argument("--project", default="twenty-ai-service", help="LangSmith project name.")
    parser.add_argument("--show-ui-code", action="store_true", help="Print copy-pasteable UI code evaluator.")
    args = parser.parse_args()
    
    if args.show_ui_code:
        print("\n=== LANGSMITH WEB UI CUSTOM CODE EVALUATOR TEMPLATE ===")
        print(UI_CODE_EVALUATOR_TEMPLATE)
        print("========================================================\n")
        return

    # Check for API Key
    if not os.environ.get("LANGSMITH_API_KEY"):
        print("❌ Error: LANGSMITH_API_KEY is not set in your environment or .env file.")
        sys.exit(1)
        
    # Warm up models for NER detection
    print("Loading Presidio NER models...")
    load_models()
    if not models_loaded():
        print("⚠️ Warning: Presidio/spaCy models failed to load. Evaluator will fall back to regex checks.")
        
    scan_recent_traces(args.project, limit=args.limit)


if __name__ == "__main__":
    main()
