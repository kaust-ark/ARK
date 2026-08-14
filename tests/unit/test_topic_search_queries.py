"""Turning a research topic into academic-search queries, and filtering the hits.

Regression for a real failure: the Deep-Research fallback fed the DR *prompt*
— prose written for an LLM, markdown headers and LaTeX included — straight to
keyword search engines as ``search_papers(query[:300])``. CrossRef's
``query.bibliographic`` fuzzy-matched incidental words and returned 12/12
topically unrelated papers for a study of spectral bias in two-layer ReLU MLPs:

  * "Comparison of two full-mouth approaches ... peri-implant mucositis"  (two/comparison)
  * "Study on Tunnel Face Failure Mechanism in Two-Layer Soils"           (two-layer)
  * "Changes in retinal layer thickness ... in the dog"                   (layer/spectral)
  * three CrossRef *figure* records, which are not papers at all

Every fixture below is a real title from that run (``projects/citefix_verify``):
POSITIVE = the 10 references the finished paper actually used, NEGATIVE = the 12
the prose query returned.
"""

import pytest

from ark.citation import (
    is_artifact_record,
    looks_like_paper,
    search_queries_from_topic,
    title_on_topic,
    topic_terms,
)


TITLE = "Empirical Investigation of Spectral Bias in Small ReLU MLPs"

# Abridged from the real config's research_idea.
IDEA = """
# Empirical Investigation of Spectral Bias in Small ReLU MLPs Fitting Univariate
Functions of Mixed Frequency Content

## Short Hypothesis
When a two-layer ReLU MLP is trained by gradient descent to fit a univariate target
function composed of multiple sinusoidal modes, the training error on low-frequency
components decreases before the error on high-frequency components. This
frequency-dependent learning order — the spectral bias — manifests as a structured
ordering of per-mode convergence times.

## Related Work
Rahaman et al. (2019) coined the term "spectral bias". Basri et al. (2019) and
Ronen et al. (2019) connected it to the eigenvalue spectrum of the Neural Tangent
Kernel. Subsequent work (Wang et al. 2021; Tancik et al. 2020 on Fourier features)
has used spectral bias as a design lever.
"""

# The DR prompt as the researcher agent actually emitted it: a markdown section,
# full sentences, an inline formula.
DR_PROSE = """## Research Topic Summary

This project provides an empirical study of **spectral bias in two-layer ReLU MLPs**
trained on univariate multi-frequency targets. A synthetic scalar function
y(x) = $\\sum_{k \\in \\{1,2,4,8,16\\}} \\sin(2\\pi k x) + \\epsilon$
(sigma = 0.05, n=512 uniform samples) is fit by full-batch SGD.
"""

POSITIVE = [
    "On the Spectral Bias of Neural Networks",
    "Neural Tangent Kernel: Convergence and Generalization in Neural Networks",
    "Fourier Features Let Networks Learn High Frequency Functions in Low Dimensional Domains",
    "Convergence and Implicit Regularization Properties of Gradient Descent for Deep Residual Networks",
    "The Convergence Rate of Neural Networks for Learned Functions of Different Frequencies",
    "On the Eigenvector Bias of Fourier Feature Networks: From Regression to Solving Multi-Scale PDEs",
    "Wide Neural Networks of Any Depth Evolve as Linear Models Under Gradient Descent",
    "Frequency Principle: Fourier Analysis Sheds Light on Implicit Regularization of Gradient Descent",
]

NEGATIVE = [
    "FMFDF: A Frequency-aware Multi-view Framework Based on a Pre-trained Language Model for Bearing Fault Diagnosis under Imbalanced Samples",
    "Quantifying What Better-Ear PTA4 Does Not Capture: High-Frequency and Asymmetric Classification Discordance in Two Independent Adult Population Samples",
    "Comparison of two full-mouth approaches in the treatment of peri-implant mucositis: a pilot study",
    "Changes in retinal layer thickness with maturation in the dog: an in vivo spectral domain optical coherence tomography imaging study",
    "Study on Tunnel Face Failure Mechanism in Two-Layer Soils",
    "Analytical and experimental study of the oblique passing of a solitary wave over a shelf in a two-layer fluid",
    "Five-Sense Healing for Empowering Employees' Psychological and Competency Development",
    "Comparison of two methods in the treatment of congenital pseudarthrosis of clavicle: multicenter experience",
]

# CrossRef records that are not papers.
ARTIFACT_TITLES = [
    "Figure 6. Pathway bias and neuromodulatory control of SPN excitatory synaptogenesis during development.",
    "Figure 3—figure supplement 2. Learning recurrent dynamics with leaky integrate-and-fire and Izhikevich neuron models.",
    "Figure 3—figure supplement 1. Structure-function analysis of the Patronin protein.",
    "Table 6: Extra layer in attention with ReLU and CNN with sigmoid.",
    "Supplementary Figure 2: gradient descent traces",
    "Appendix A. Proof of the frequency bound",
]


@pytest.fixture(scope="module")
def terms():
    return topic_terms(f"{TITLE}\n{DR_PROSE}\n{IDEA}")


# ── query construction ───────────────────────────────────────────────────────

def test_queries_are_short_and_keyword_shaped():
    """Long queries are what turn a keyword search back into prose matching."""
    qs = search_queries_from_topic(f"{DR_PROSE}\n{IDEA}", TITLE, limit=4)
    assert qs, "no queries derived from the topic"
    for q in qs:
        assert 2 <= len(q.split()) <= 8, f"{q!r} is not a keyword query"
        for junk in ("##", "**", "$", "\\", "\n", "y(x)"):
            assert junk not in q, f"{q!r} still carries markup {junk!r}"


def test_queries_capture_the_actual_subject():
    qs = [q.lower() for q in search_queries_from_topic(f"{DR_PROSE}\n{IDEA}", TITLE, limit=4)]
    joined = " | ".join(qs)
    assert "spectral" in joined and "bias" in joined
    assert "relu" in joined or "mlp" in joined


def test_named_prior_work_becomes_its_own_query():
    """`Rahaman et al. (2019)` names a paper that provably exists — the most
    precise query material in the whole idea."""
    qs = search_queries_from_topic(f"{DR_PROSE}\n{IDEA}", TITLE, limit=6)
    assert any("Rahaman" in q and "2019" in q for q in qs), qs


def test_no_duplicate_words_within_a_query():
    """The title often repeats inside the idea; a query must not read
    'Spectral Bias Small ReLU MLPs Spectral Bias Small'."""
    for q in search_queries_from_topic(f"{TITLE}\n{DR_PROSE}\n{IDEA}", TITLE, limit=6):
        words = [w.lower() for w in q.split()]
        assert len(words) == len(set(words)), q


def test_named_work_is_anchored_to_the_title_not_word_frequency():
    """Regression: corpus frequency is unstable across runs and two frequent
    but unrelated terms compose into another field's term of art.

    A real run's DR prompt leaned on network *width* (the hypothesis is about
    width-invariance), so "width" outranked "bias" and the query became
    'Rahaman 2019 spectral width' — a laser/radar concept. Back came Springer
    glossary stubs for spectral line width and a radar-turbulence paper. The
    title is curated and does not drift, so anchor to it.
    """
    width_heavy = ("width width width width widths widths widths error error "
                   "mode per-mode convergence " + IDEA)
    qs = search_queries_from_topic(width_heavy, TITLE, limit=6)
    named = [q for q in qs if "Rahaman" in q]
    assert named, qs
    for q in named:
        assert "width" not in q.lower(), f"frequency noise leaked in: {q!r}"
        assert "spectral" in q.lower() and "bias" in q.lower(), q


def test_queries_are_deduplicated_and_capped():
    qs = search_queries_from_topic(f"{DR_PROSE}\n{IDEA}", TITLE, limit=3)
    assert len(qs) <= 3
    assert len({q.lower() for q in qs}) == len(qs)


def test_empty_topic_yields_no_queries():
    assert search_queries_from_topic("", "") == []


def test_survives_prose_with_no_emphasis_or_named_work():
    qs = search_queries_from_topic("We study how networks fit signals.", "Signal Fitting Networks")
    assert all(2 <= len(q.split()) <= 8 for q in qs)


# ── the relevance backstop ───────────────────────────────────────────────────

@pytest.mark.parametrize("title", POSITIVE)
def test_real_references_pass_the_gate(title, terms):
    assert title_on_topic(title, terms), title


@pytest.mark.parametrize("title", NEGATIVE)
def test_off_topic_hits_are_rejected(title, terms):
    assert not title_on_topic(title, terms), f"off-topic hit survived: {title}"


@pytest.mark.parametrize("title", ARTIFACT_TITLES)
def test_figure_and_table_records_are_rejected(title, terms):
    assert is_artifact_record(title), f"not detected as an artifact record: {title}"
    assert not title_on_topic(title, terms), f"artifact record survived: {title}"


@pytest.mark.parametrize("title", POSITIVE + [
    "Figures of Merit for Neural Network Compression",     # "Figures" ... but a paper
    "Tabular Data Learning with Frequency Features",       # starts with "Tab"-ish
    "Schemes for Spectral Analysis of ReLU Networks",       # starts with "Scheme"-ish
])
def test_real_papers_are_not_mistaken_for_artifacts(title):
    assert not is_artifact_record(title), title


@pytest.mark.parametrize("title", [
    "spectral width", "spectral line width", "pulse spectral width",
    "relative spectral width", "laser spectral width",
])
def test_reference_work_term_stubs_are_rejected(title):
    """Springer registers its reference works term by term: perfect keyword
    matches, no authors, no year, nothing to cite. A real run kept five."""
    assert not looks_like_paper(title, [], None)


@pytest.mark.parametrize("title,authors,year", [
    ("On the Spectral Bias of Neural Networks", ["N Rahaman"], 2019),
    ("Deep Learning", ["I Goodfellow"], 2016),      # 2 words, and a real book
    ("Frequency Principle", [], 2019),              # year alone is enough
    ("Wide Neural Networks", ["J Lee"], None),      # author alone is enough
])
def test_real_work_is_kept(title, authors, year):
    assert looks_like_paper(title, authors, year)


def test_artifact_records_are_rejected_even_with_metadata():
    assert not looks_like_paper("Figure 6. Pathway bias", ["A Author"], 2020)


@pytest.mark.parametrize("title", [
    # Real hit from a run: the paper is on topic, but this record is its
    # supplementary FILE, carrying the paper's title plus the file tag.
    "Towards the Spectral bias Alleviation by Normalizations in Coordinate Networks_supp1-3",
    "Some Paper - Supplementary Material",
    "A Method — Supplementary Information",
    "Method X supp 2",
    "Study Y_suppl_data",
])
def test_supplementary_file_records_are_rejected(title):
    assert is_artifact_record(title), title


@pytest.mark.parametrize("title", [
    "Supervised Learning",                  # starts with "supp"-ish letters
    "Supplementary Motor Area Activation",  # "Supplementary" as a real adjective
    "Analysis of SI Units in Physics",
    "Deep Learning",
])
def test_supplement_filter_does_not_eat_real_titles(title):
    assert not is_artifact_record(title), title


def test_untitled_hit_is_rejected():
    assert not looks_like_paper("", ["A Author"], 2020)


def test_gate_needs_a_topic_to_compare_against():
    """No vocabulary means no judgement — must not silently pass everything."""
    assert not title_on_topic("On the Spectral Bias of Neural Networks", set())
    assert not title_on_topic("", {"spectral", "bias"})


# ── topic_terms ──────────────────────────────────────────────────────────────

def test_generic_academic_words_are_not_topic_terms(terms):
    """These are what matched dentistry and civil engineering."""
    for w in ("study", "comparison", "empirical", "investigation", "analysis",
              "method", "results", "figure", "table", "two"):
        assert w not in terms, f"{w!r} must not count as a distinctive term"


def test_subject_words_are_topic_terms(terms):
    for w in ("spectral", "bias", "relu", "frequency", "gradient"):
        assert w in terms, f"{w!r} missing from the topic vocabulary"


def test_math_and_markup_do_not_become_terms():
    t = topic_terms(r"## Head $\sum_{k} \sin(2\pi k x)$ `code` https://x.io/paper spectral")
    assert "spectral" in t
    for junk in ("sum", "sin", "http", "https", "code", "pi"):
        assert junk not in t, f"{junk!r} leaked in from markup/math"
