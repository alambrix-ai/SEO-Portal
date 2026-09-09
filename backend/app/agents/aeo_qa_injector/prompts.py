"""Prompts and HTML rendering for the AEO Q&A injector."""
from __future__ import annotations

import re

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You prepare web content so answer engines such as Perplexity, "
    "ChatGPT, Google's AI answers and Claude can retrieve and quote it "
    "accurately.\n\n"
    "What makes a pair worth publishing:\n"
    "- The question is one a real person types or says, in their words, not "
    "the company's. 'How much does it cost?' beats 'What is the pricing "
    "structure?'. One idea per question.\n"
    "- The answer is complete on its own. A retrieval engine quotes it with "
    "no page around it, so it names the subject rather than saying 'this "
    "service' or 'we', and it does not refer to 'the above' or 'below'.\n"
    "- Two to three sentences. Long enough to actually answer, short enough "
    "to be lifted whole.\n"
    "- Every fact comes from the source copy. You never invent prices, "
    "availability, timescales, guarantees, statistics, certifications or "
    "client names. If the page does not answer a question a reader would "
    "obviously ask, leave that question out. A confident wrong answer "
    "published on the customer's own site is far worse than a gap.\n"
    "- You do not restate the page. A pair that only rephrases a sentence "
    "already on the page adds nothing to retrieve."
)
SYSTEM = SYSTEM + HOUSE_STYLE

SCHEMA_HINT = """{
  "pairs": [
    {
      "question": "a question a person would actually ask, in their words",
      "answer": "a complete 2-3 sentence answer, drawn only from the source,
                 that stands alone when quoted without the page"
    }
  ]
}"""

# Marker delimiting the section this agent owns, so a re-injection replaces
# its own block instead of appending a second copy.
SECTION_START = "<!-- automarket:aeo:start -->"
SECTION_END = "<!-- automarket:aeo:end -->"
_SECTION_RE = re.compile(
    re.escape(SECTION_START) + r".*?" + re.escape(SECTION_END), re.DOTALL
)

def qa_pairs(
    *,
    title: str,
    url: str,
    body: str,
    industry: str,
    existing_questions: list[str],
    max_body_chars: int = 6000,
) -> str:
    already = (
        "\nALREADY COVERED (do not repeat these):\n"
        + "\n".join(f"- {q}" for q in existing_questions[:20])
        if existing_questions
        else ""
    )
    sector = (
        f"SECTOR: {industry}"
        if industry
        else "SECTOR: not stated. Infer it from the copy below"
    )
    return f"""[TASK:aeo_pairs]
Produce conversational Q&A pairs for this page.

TITLE: {title}
URL: {url}
{sector}

SOURCE COPY:
{body[:max_body_chars]}{already}

Write between three and six new pairs, in the order a reader would want them
answered: what this is, then whether it fits them, then the practical
questions.

Only write a pair where the source copy genuinely contains the answer. If
that means three pairs rather than six, write three. Padding the count with
answers the page does not support is the one failure mode that matters here:
these publish to the customer's own site under their name."""

def render_qa_block(question: str, answer: str) -> str:
    """One pair as an ``FAQPage`` entry.

    ``itemprop="mainEntity"`` is what attaches this Question to the FAQPage
    that :func:`merge_qa_section` opens around it. Without that container
    these were free-standing Question items, which are not eligible for FAQ
    rich results and read to a parser as unrelated fragments — the one piece
    of structured data this agent exists to get right.
    """
    return (
        '<div class="aeo-qa" itemscope itemprop="mainEntity" '
        'itemtype="https://schema.org/Question">\n'
        f'  <h3 itemprop="name">{_escape(question)}</h3>\n'
        '  <div itemprop="acceptedAnswer" itemscope '
        'itemtype="https://schema.org/Answer">\n'
        f'    <p itemprop="text">{_escape(answer)}</p>\n'
        "  </div>\n"
        "</div>"
    )

def merge_qa_section(body: str, blocks: list[str]) -> str:
    """Insert or replace this agent's Q&A section in a page body."""
    section = "\n".join(
        [
            SECTION_START,
            '<section class="aeo-questions" itemscope '
            'itemtype="https://schema.org/FAQPage" '
            'aria-label="Frequently asked questions">',
            "<h2>Questions and answers</h2>",
            *blocks,
            "</section>",
            SECTION_END,
        ]
    )
    if _SECTION_RE.search(body):
        return _SECTION_RE.sub(section, body)
    return f"{body.rstrip()}\n\n{section}\n"

def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
