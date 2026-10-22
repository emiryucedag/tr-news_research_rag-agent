import os
import sys
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from observability.tracer import observe
from agents.multi_agent import research, write, verify_citations, tracer

# A handful of distinct topics/categories - broad enough to exercise the
# category-match retrieval path, the pure-semantic path, and a mix.
SCENARIOS = [
    "Bugün spor ile ilgili hangi haberler var, özetler misin?",
    "Bugün teknoloji ile ilgili hangi haberler var, özetler misin?",
    "Yapay zeka ile ilgili güncel haberler neler?",
    "Ekonomi ile ilgili bugünkü haberler neler?",
    "Bugün gündemde neler var, özetler misin?",
]

# Patterns that should NEVER appear in a final answer - this directly encodes
# the junk-filtering regression we fixed earlier (the Turkish İ/I lowering bug).
FORBIDDEN_PATTERNS = ["canlı izle", "şifresiz izle", "canlı yayın izle"]

LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
DATE_PATTERN = re.compile(r"tarih:\s*\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}")


def turkish_lower(text):
    return text.replace("İ", "i").replace("I", "ı").lower()


def check_no_hallucinated_links(answer, findings):
    # Reuses the exact same check the production pipeline uses (verify_citations)
    # rather than a separate, looser definition - one source of truth for what
    # counts as a "trustworthy" citation.
    hallucinated = verify_citations(answer, findings)
    total_links = len(LINK_PATTERN.findall(answer))
    return {
        "total_links": total_links,
        "hallucinated_links": hallucinated,
        "passed": len(hallucinated) == 0,
    }


def check_every_item_has_date(answer):
    # Count markdown links vs. "tarih:" occurrences - they should roughly match
    # (one date per cited article). This is a heuristic, not a perfect parse.
    link_count = len(LINK_PATTERN.findall(answer))
    date_count = len(DATE_PATTERN.findall(answer))
    return {
        "link_count": link_count,
        "date_count": date_count,
        "passed": link_count > 0 and date_count >= link_count,
    }


def check_no_forbidden_patterns(answer):
    lowered = turkish_lower(answer)
    found = [p for p in FORBIDDEN_PATTERNS if p in lowered]
    return {
        "found_patterns": found,
        "passed": len(found) == 0,
    }


@observe(as_type="evaluator", name="eval-scenario")
def evaluate_scenario(question):
    """
    Runs one scenario end to end and returns its checks. Decorated so that each
    scenario becomes its own trace: the researcher and writer runs nest under it,
    and the pass/fail result is its output - which makes a failing scenario easy
    to trace back to the exact steps that produced it.
    """
    findings = research(question)
    answer = write(question, findings)

    hallucination_check = check_no_hallucinated_links(answer, findings)
    date_check = check_every_item_has_date(answer)
    junk_check = check_no_forbidden_patterns(answer)

    scenario_passed = (
        hallucination_check["passed"]
        and date_check["passed"]
        and junk_check["passed"]
    )

    return {
        "question": question,
        "num_findings": len(findings),
        "hallucination_check": hallucination_check,
        "date_check": date_check,
        "junk_check": junk_check,
        "passed": scenario_passed,
    }


def run_evals():
    results = []

    for i, question in enumerate(SCENARIOS, start=1):
        print(f"\n{'='*70}\nSCENARIO {i}/{len(SCENARIOS)}: {question}\n{'='*70}")

        result = evaluate_scenario(question)
        results.append(result)

        hallucination_check = result["hallucination_check"]
        date_check = result["date_check"]
        junk_check = result["junk_check"]

        status = "PASS" if result["passed"] else "FAIL"
        print(f"\n[{status}] findings={result['num_findings']} "
              f"links={hallucination_check['total_links']} "
              f"hallucinated={len(hallucination_check['hallucinated_links'])} "
              f"dates={date_check['date_count']} "
              f"junk_found={junk_check['found_patterns']}")

        if hallucination_check["hallucinated_links"]:
            print(f"  HALLUCINATED LINKS: {hallucination_check['hallucinated_links']}")

    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    passed_count = sum(1 for r in results if r["passed"])
    print(f"{passed_count}/{len(results)} scenarios passed")

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['question']}")

    tracer.flush()

    return results


if __name__ == "__main__":
    run_evals()