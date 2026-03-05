import json
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple
import requests
import os

BASE_URL = os.getenv("AUDIT_API_URL", "http://localhost:8000/ask_file")
TIMEOUT = 180  # seconds
CATEGORY_MAP = {
    "indemn": ["liability exposure", "indemnification risk", "liability escalation", "damages"],
    "liability": ["liability exposure", "liability escalation", "damages", "indemnification risk"],
    "exposure": ["liability exposure", "risk exposure", "indemnification risk"],
    "escalation": ["liability escalation", "liability exposure"],
    "damages": ["damages", "liability", "indemnification risk"],
}

CAP_SYNONYMS = [
    "cap",
    "capped",
    "liability cap",
    "cap at",
    "aggregate cap",
    "limitation of liability",
    "limit liability",
    "liability limit",
    "maximum liability",
]

CATEGORY_GROUPS = {
    "structural inconsistency": [
        "structural inconsistency",
        "structural conflict",
        "structural omission",
        "enforceability weakness",
    ],
    "residuals": [
        "residuals",
        "residuals risk",
    ],
    "indemnification": [
        "indemnification",
        "indemnification risk",
        "liability exposure",
    ],
}

@dataclass
class Issue:
    severity: str
    category: str
    quoted: str
    risk_explanation: str
    suggested_improvement: str
    block: str


def load_expectation(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def post_file(path: Path) -> Tuple[bool, str, int]:
    session_id = f"regtest-{uuid.uuid4()}"
    data = {"session_id": session_id, "mode": "AUDIT"}

    for attempt in range(2):
        try:
            with path.open("rb") as fh:
                files = {"file": (path.name, fh, "text/plain")}
                resp = requests.post(BASE_URL, files=files, data=data, timeout=(3, TIMEOUT))
            return resp.ok, resp.text, resp.status_code
        except requests.Timeout:
            if attempt == 0:
                continue
            return False, "Request timeout", 0
        except requests.RequestException as exc:
            return False, f"Request error: {exc}", 0


def extract_issues(report: str) -> List[Issue]:
    chunks = re.split(r"(?im)^Issue:\s*", report)
    issues: List[Issue] = []
    for chunk in chunks[1:]:
        block = chunk.strip()
        severity = _search_line(block, "Severity")
        category = _search_line(block, "Category")
        quoted = _search_block(block, "Quoted Text")
        risk_explanation = _search_block(block, "Risk Explanation")
        suggested_improvement = _search_block(block, "Suggested Improvement")
        issues.append(
            Issue(
                severity=severity or "",
                category=category or "",
                quoted=quoted or "",
                risk_explanation=risk_explanation or "",
                suggested_improvement=suggested_improvement or "",
                block=block,
            )
        )
    return issues


def _search_line(text: str, label: str) -> str:
    match = re.search(rf"(?im)^{label}:\s*(.+)", text)
    return match.group(1).strip() if match else ""


def _search_block(text: str, label: str) -> str:
    match = re.search(
        rf"(?is){label}:\s*(.*?)(?:\n[A-Z][A-Za-z ]+:\s|$)", text.strip()
    )
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def check_keywords(response_text: str, expectation: Dict) -> Tuple[bool, List[str]]:
    lower = response_text.lower()
    failures = []

    must_all = expectation.get("must_detect_all") or expectation.get("must_detect") or []
    must_any = expectation.get("must_detect_any") or []
    must_not = expectation.get("must_not_detect") or []

    for kw in must_all:
        if kw.lower() not in lower:
            failures.append(f"missing '{kw}'")

    if must_any:
        if not any(kw.lower() in lower for kw in must_any) and not any(cap in lower for cap in CAP_SYNONYMS):
            failures.append(f"none of must_detect_any found ({', '.join(must_any)})")

    for kw in must_not:
        if kw.lower() in lower:
            failures.append(f"forbidden '{kw}' present")

    return (len(failures) == 0), failures


def category_matches(expected: str, actual: str) -> bool:
    if not expected or not actual:
        return False
    expected = expected.lower().strip()
    actual = actual.lower().strip()
    if expected in CATEGORY_GROUPS:
        if any(group_item in actual for group_item in CATEGORY_GROUPS[expected]):
            return True
    return expected in actual or actual in expected


def check_severity(issues: List[Issue], expected: Dict[str, str]) -> Tuple[bool, List[str]]:
    failures = []
    for exp_cat, exp_level in expected.items():
        match = next(
            (
                iss
                for iss in issues
                if category_matches(exp_cat, iss.category)
            ),
            None,
        )
        if not match:
            failures.append(f"category '{exp_cat}' not found")
            continue
        if match.severity.strip().upper() != exp_level.strip().upper():
            failures.append(
                f"{exp_cat} severity {match.severity} != expected {exp_level}"
            )
    return (len(failures) == 0), failures


def check_duplicates(issues: List[Issue]) -> Tuple[bool, List[str]]:
    quoted = [iss.quoted for iss in issues if iss.quoted]
    counts = Counter(quoted)
    dups = [q for q, c in counts.items() if c > 1]
    return (len(dups) == 0), dups


def detect_clause_hallucinations(response_text: str, doc_text: str) -> List[str]:
    resp_refs = re.findall(r"(?i)\b(?:clause|section)\s+\d+(?:\.\d+)?", response_text)
    doc_lower = doc_text.lower()
    hallucinations = []
    for ref in resp_refs:
        if ref.lower() not in doc_lower:
            hallucinations.append(f"hallucinated reference '{ref.strip()}'")
    return hallucinations


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def detect_quote_hallucinations(issues: List[Issue], doc_text: str) -> List[str]:
    doc_tokens = re.findall(r"\w+", doc_text.lower())
    doc_token_set = set(doc_tokens)
    hallucinations = []
    for iss in issues:
        if iss.quoted:
            q_tokens = re.findall(r"\w+", iss.quoted.lower())
            if not q_tokens:
                continue
            match_count = sum(1 for t in q_tokens if t in doc_token_set)
            ratio = match_count / len(q_tokens)
            if ratio < 0.8:
                hallucinations.append("quoted text not sufficiently present in document")
    return hallucinations


def liability_only_in_indemnity(issues: List[Issue]) -> Tuple[bool, List[str]]:
    offenders = []
    for iss in issues:
        if "liability" in iss.block.lower():
            cat = iss.category.lower()
            if "indemn" not in cat and "liability" not in cat:
                offenders.append(f"liability in category '{iss.category}'")
    return (len(offenders) == 0), offenders


def overflagging_check(issues: List[Issue], expectation: Dict[str, str]) -> Tuple[bool, str]:
    expected_levels = [v.upper() for v in expectation.get("expected_severity", {}).values()]
    clean = not any(level in {"HIGH", "CRITICAL"} for level in expected_levels)
    if not clean:
        return True, ""

    high_count = sum(1 for iss in issues if iss.severity.strip().upper() == "HIGH")
    critical_count = sum(1 for iss in issues if iss.severity.strip().upper() == "CRITICAL")
    if critical_count > 0 or high_count > 1:
        return False, f"over-flagging clean contract (HIGH={high_count}, CRITICAL={critical_count})"
    return True, ""


def contradiction_checks(issues: List[Issue]) -> Tuple[bool, List[str]]:
    messages = []
    # conflicting severities for same quote
    quote_to_sev: Dict[str, set] = {}
    for iss in issues:
        if iss.quoted:
            quote_to_sev.setdefault(iss.quoted, set()).add(iss.severity.strip().upper())
    for quote, sevs in quote_to_sev.items():
        if len(sevs) > 1:
            messages.append(f"conflicting severities for quoted text '{quote}'")

    # same quote under multiple categories
    quote_to_cat: Dict[str, set] = {}
    for iss in issues:
        if iss.quoted:
            quote_to_cat.setdefault(iss.quoted, set()).add(iss.category.lower())
    for quote, cats in quote_to_cat.items():
        if len(cats) > 1:
            messages.append(f"same clause categorized multiple ways: {', '.join(sorted(cats))}")

    return (len(messages) == 0), messages


def rewrite_sanity(issue: Issue) -> Tuple[bool, str]:
    rexpl = issue.risk_explanation.lower()
    improv = issue.suggested_improvement.lower()
    quoted = issue.quoted.lower()

    if not improv:
        return True, ""

    def contains_any(text: str, words: List[str]) -> bool:
        return any(w in text for w in words)

    # If risk says overly broad, improvement should narrow
    if contains_any(rexpl, ["overly broad", "too broad", "broad scope", "overbroad"]):
        if contains_any(improv, ["broaden", "expand", "widen", "unrestricted", "unlimited"]):
            return False, "rewrite expands despite overbreadth risk"
        if not contains_any(improv, ["narrow", "limit", "restrict", "define", "specific", "scope"]):
            return False, "rewrite does not narrow overbroad clause"

    if contains_any(quoted, ["unlimited", "uncapped", "no cap", "no limit"]):
        if contains_any(improv, ["remove cap", "no cap", "unlimited"]):
            return False, "rewrite keeps clause unlimited"
        if not contains_any(improv, ["cap", "limit", "maximum", "ceiling"]):
            return False, "rewrite fails to add cap for unlimited risk"

    if contains_any(rexpl, ["terminat", "expires"]) and contains_any(rexpl, ["confidential"]):
        if contains_any(improv, ["terminate immediately", "end confidentiality", "remove survival"]):
            return False, "rewrite accelerates termination of confidentiality"
        if not contains_any(improv, ["survive", "continue", "extend", "post-termination", "after expiration"]):
            return False, "rewrite fails to maintain confidentiality survival"

    return True, ""


def detect_doc_indemnity_direction(text: str) -> Tuple[str, str]:
    match = re.search(
        r"\b(Discloser|Recipient)[^\.]{0,80}?indemnif\w+[^\.]{0,80}?(Discloser|Recipient)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).lower(), match.group(2).lower()
    return "", ""


def verify_party_direction(doc_text: str, response_text: str, issues: List[Issue]) -> Tuple[bool, str]:
    indemnifier, indemnitee = detect_doc_indemnity_direction(doc_text)
    if not indemnifier or not indemnitee:
        return True, ""

    pattern = re.compile(
        rf"{indemnifier}[^\.]{{0,80}}indemnif\w+[^\.]{{0,80}}{indemnitee}",
        re.IGNORECASE,
    )
    if pattern.search(response_text):
        pass
    else:
        return False, f"direction mismatch ({indemnifier} -> {indemnitee} not stated)"

    def mentions_harm(text: str, party: str) -> bool:
        return bool(
            re.search(
                rf"{party}[^\.]{{0,120}}(exposed|exposes|exposure|risk|liable|liability|harm|damages|bears)",
                text,
                re.IGNORECASE,
            )
        )

    harmed_flag = any(
        mentions_harm(segment, indemnitee)
        for segment in ([response_text] + [iss.block for iss in issues] + [iss.risk_explanation for iss in issues])
    )
    reversed_flag = any(
        mentions_harm(segment, indemnifier)
        for segment in ([response_text] + [iss.block for iss in issues] + [iss.risk_explanation for iss in issues])
    )

    if reversed_flag and not harmed_flag:
        return False, f"harmed party reversed (points to {indemnifier})"
    if not harmed_flag:
        return False, f"harmed party ({indemnitee}) not identified"

    return True, ""


def structural_checks(issues: List[Issue], expectation: Dict) -> Tuple[bool, List[str]]:
    messages = []

    # Same clause assigned to multiple categories
    quoted_to_categories: Dict[str, set] = {}
    for iss in issues:
        if not iss.quoted:
            continue
        quoted_to_categories.setdefault(iss.quoted, set()).add(iss.category.lower())
    for quote, cats in quoted_to_categories.items():
        if len(cats) > 1:
            messages.append(f"same clause used in categories: {', '.join(sorted(cats))}")

    # Unlimited exposure language in non-liability contexts
    for iss in issues:
        block_lower = iss.block.lower()
        if re.search(r"\b(unlimited|uncapped|no cap|no limit)\b", block_lower):
            if re.search(r"\b(liability|damages|exposure)\b", block_lower):
                cat_lower = iss.category.lower()
                if all(token not in cat_lower for token in ["indemn", "liability", "damage"]):
                    messages.append("unlimited exposure language outside liability context")

    # More than 2 critical issues in clean contracts
    critical_count = sum(1 for iss in issues if iss.severity.strip().upper() == "CRITICAL")
    expected_levels = [v.upper() for v in expectation.get("expected_severity", {}).values()]
    clean_contract = "CRITICAL" not in expected_levels
    if clean_contract and critical_count > 2:
        messages.append("clean contract flagged with >2 CRITICAL issues")

    return (len(messages) == 0), messages


def determinism_check(run_a: List[Issue], run_b: List[Issue]) -> Tuple[bool, List[str], float]:
    messages = []
    if len(run_a) != len(run_b):
        messages.append("issue count changed between runs")

    sev_a = Counter([iss.severity.strip().upper() for iss in run_a])
    sev_b = Counter([iss.severity.strip().upper() for iss in run_b])
    if sev_a != sev_b:
        messages.append("severity distribution changed between runs")

    quotes_a = {normalize_text(iss.quoted) for iss in run_a if iss.quoted}
    quotes_b = {normalize_text(iss.quoted) for iss in run_b if iss.quoted}
    if quotes_a != quotes_b:
        messages.append("quoted clauses changed between runs")

    stable = len(messages) == 0
    stability_score = 1.0 if stable else 0.0
    return stable, messages, stability_score


def collect_failure_reasons(res: Dict) -> List[str]:
    reasons = []
    if res.get("server_error"):
        reasons.append(res["server_error"])
    if str(res.get("risk", "")).startswith("FAIL") and res.get("keyword_summary") not in {"PASS", None}:
        reasons.append(res["keyword_summary"])
    if str(res.get("severity", "")).startswith("FAIL"):
        reasons.append(res["severity"])
    if str(res.get("duplication", "")).startswith("FAIL"):
        reasons.append(res["duplication"])
    if str(res.get("count", "")).startswith("FAIL"):
        reasons.append(res["count"])
    if str(res.get("structural", "")).startswith("FAIL") and res.get("structural_summary") not in {"PASS", None}:
        reasons.append(res["structural_summary"])
    if str(res.get("hallucination", "")).startswith("FAIL") and res.get("hallucination_summary") not in {"PASS", None}:
        reasons.append(res["hallucination_summary"])
    if str(res.get("determinism", "")).startswith("FAIL") and res.get("determinism_summary"):
        reasons.append(res["determinism_summary"])
    if res.get("false_positive_count", 0):
        reasons.append(f"False positives: {res['false_positive_count']}")
    if res.get("hallucination_count", 0):
        reasons.append(f"Hallucinations: {res['hallucination_count']}")
    return reasons


def run_test(doc_path: Path) -> Dict[str, str]:
    expectation_path = doc_path.with_suffix(".expected.json")
    expectation = load_expectation(expectation_path)

    def evaluate_once() -> Dict:
        ok, body, status = post_file(doc_path)
        base = {
            "risk": "PASS",
            "severity": "PASS",
            "duplication": "PASS",
            "count": "PASS",
            "structural": "PASS",
            "hallucination": "PASS",
            "keyword_summary": "PASS",
            "structural_summary": "PASS",
            "hallucination_summary": "PASS",
            "false_positive_count": 0,
            "issues": [],
            "response_text": body,
            "hallucination_count": 0,
            "server_error": None,
        }
        if not ok:
            base["server_error"] = body
            for key in base:
                if isinstance(base[key], str):
                    base[key] = f"FAIL (http {status}) {body}"
            return base

        doc_text = doc_path.read_text(encoding="utf-8")
        issues = extract_issues(body)
        base["issues"] = issues
        total_issues = len(issues)
        critical_count = sum(1 for iss in issues if iss.severity.strip().upper() == "CRITICAL")
        high_count = sum(1 for iss in issues if iss.severity.strip().upper() == "HIGH")
        base["issues_detected"] = total_issues
        base["critical_count"] = critical_count
        base["high_count"] = high_count

        kw_ok, kw_failures = check_keywords(body, expectation)
        if not kw_ok:
            base["risk"] = "FAIL"
            base["keyword_summary"] = "; ".join(kw_failures)

        sev_ok, sev_failures = check_severity(issues, expectation.get("expected_severity", {}))
        if not sev_ok:
            base["severity"] = f"FAIL ({'; '.join(sev_failures)})"

        dup_ok, dup_list = check_duplicates(issues)
        if not dup_ok:
            base["duplication"] = f"FAIL (duplicate quoted text: {', '.join(dup_list)})"

        if len(issues) > expectation.get("max_issues", 8):
            base["count"] = f"FAIL ({len(issues)} issues > max {expectation.get('max_issues', 8)})"

        liability_ok, liability_failures = liability_only_in_indemnity(issues)
        if not liability_ok:
            base["risk"] = "FAIL"
            base["keyword_summary"] = (
                "; ".join(
                    filter(
                        None,
                        [
                            base.get("keyword_summary")
                            if base.get("keyword_summary") not in {"PASS", None}
                            else None
                        ]
                        + liability_failures,
                    )
                )
                or "; ".join(liability_failures)
            )

        party_ok, party_msg = verify_party_direction(doc_text, body, issues)
        if not party_ok:
            base["risk"] = "FAIL"
            base["keyword_summary"] = (
                "; ".join(
                    [base["keyword_summary"]] + [party_msg]
                )
                if base["keyword_summary"] != "PASS"
                else party_msg
            )

        structural_ok, structural_msgs = structural_checks(issues, expectation)
        if not structural_ok:
            base["structural"] = f"FAIL"
            base["structural_summary"] = "; ".join(structural_msgs)

        contr_ok, contr_msgs = contradiction_checks(issues)
        if not contr_ok:
            base["structural"] = "FAIL"
            base["structural_summary"] = "; ".join(
                filter(
                    None,
                    [
                        base.get("structural_summary")
                        if base.get("structural_summary") not in {"PASS", "no structural contradictions detected"}
                        else None
                    ]
                    + contr_msgs,
                )
            )

        over_ok, over_msg = overflagging_check(issues, expectation)
        if not over_ok:
            base["risk"] = "FAIL"
            base["keyword_summary"] = (
                "; ".join(
                    filter(
                        None,
                        [
                            base.get("keyword_summary")
                            if base.get("keyword_summary") not in {"PASS", None}
                            else None
                        ]
                        + [over_msg],
                    )
                )
            )

        # Hallucination checks
        hall_msgs = detect_clause_hallucinations(body, doc_text) + detect_quote_hallucinations(issues, doc_text)
        if hall_msgs:
            base["hallucination"] = "FAIL"
            base["hallucination_summary"] = "; ".join(hall_msgs)
            base["hallucination_count"] = len(hall_msgs)

        # Rewrite sanity
        sanity_msgs = []
        for iss in issues:
            ok_sane, msg = rewrite_sanity(iss)
            if not ok_sane:
                sanity_msgs.append(msg)
        if sanity_msgs:
            base["risk"] = "FAIL"
            base["keyword_summary"] = (
                "; ".join(
                    filter(
                        None,
                        [
                            base.get("keyword_summary")
                            if base.get("keyword_summary") not in {"PASS", None}
                            else None
                        ]
                        + sanity_msgs,
                    )
                )
            )

        # False positives on clean contracts
        expected_levels = [v.upper() for v in expectation.get("expected_severity", {}).values()]
        clean_contract = not any(level in {"HIGH", "CRITICAL"} for level in expected_levels)
        if clean_contract:
            base["false_positive_count"] = high_count + critical_count

        return base

    run1 = evaluate_once()
    run2 = evaluate_once()

    # Determinism check
    det_ok, det_msgs, stability_score = determinism_check(run1.get("issues", []), run2.get("issues", []))

    combined = run1.copy()
    combined["determinism"] = "PASS" if det_ok else "FAIL"
    combined["determinism_summary"] = "; ".join(det_msgs) if det_msgs else "stable"
    combined["stability_score"] = stability_score
    combined["issues_run2"] = run2.get("issues", [])
    combined["hallucination_count"] = max(run1.get("hallucination_count", 0), run2.get("hallucination_count", 0))
    combined["false_positive_count"] = max(run1.get("false_positive_count", 0), run2.get("false_positive_count", 0))
    combined["high_count"] = max(run1.get("high_count", 0), run2.get("high_count", 0))
    combined["critical_count"] = max(run1.get("critical_count", 0), run2.get("critical_count", 0))

    # Merge FAIL statuses from second run
    for key in ["risk", "severity", "duplication", "count", "structural", "hallucination"]:
        if isinstance(run2.get(key), str) and run2.get(key, "").startswith("FAIL"):
            combined[key] = run2[key]
    # Prefer more detailed summaries if present
    if run2.get("keyword_summary", "") not in {"PASS", None} and run2.get("keyword_summary", "") != "":
        combined["keyword_summary"] = run2["keyword_summary"]
    if run2.get("structural_summary", "") not in {"PASS", None, "no structural contradictions detected"} and run2.get("structural_summary", "") != "":
        combined["structural_summary"] = run2["structural_summary"]
    if run2.get("hallucination_summary", "") not in {"PASS", None} and run2.get("hallucination_summary", "") != "":
        combined["hallucination_summary"] = run2["hallucination_summary"]

    return combined


def main() -> None:
    corpus = Path(__file__).parent / "test_corpus"
    test_files = sorted(corpus.glob("*.txt"))

    overall_pass = True
    results = []
    for doc in test_files:
        res = run_test(doc)
        res["name"] = doc.stem
        results.append(res)
        if any(str(val).startswith("FAIL") for val in res.values()):
            overall_pass = False

    headers = [
        "Test",
        "Issues",
        "HIGH",
        "CRIT",
        "Risk",
        "Severity",
        "Dup",
        "Count",
        "Structural",
        "Halluc",
        "Determinism",
        "FP",
        "Halluc#",
        "Stability",
    ]
    col_widths = [18, 6, 5, 5, 8, 10, 6, 6, 10, 9, 12, 4, 8, 9]
    def fmt(val, width):
        return str(val).ljust(width)

    header_line = " ".join(fmt(h, w) for h, w in zip(headers, col_widths))
    print(header_line)
    print("-" * len(header_line))

    for res in results:
        row = [
            res["name"],
            res.get("issues_detected", "N/A"),
            res.get("high_count", "N/A"),
            res.get("critical_count", "N/A"),
            res.get("risk", ""),
            res.get("severity", ""),
            res.get("duplication", ""),
            res.get("count", ""),
            res.get("structural", ""),
            res.get("hallucination", ""),
            res.get("determinism", ""),
            res.get("false_positive_count", 0),
            res.get("hallucination_count", 0),
            res.get("stability_score", 0),
        ]
        print(" ".join(fmt(val, w) for val, w in zip(row, col_widths)))

    print("-" * len(header_line))
    print(f"OVERALL RESULT: {'PASS' if overall_pass else 'FAIL'}")

    failing = [res for res in results if any(str(res.get(k, "")).startswith("FAIL") for k in ["risk", "severity", "duplication", "count", "structural", "hallucination", "determinism"]) or res.get("server_error")]
    if failing:
        print("\nFailure details:")
        for res in failing:
            reasons = collect_failure_reasons(res)
            print(f"- {res['name']}:")
            for reason in reasons:
                print(f"  * {reason}")
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
