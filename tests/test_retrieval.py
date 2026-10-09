import pytest

from app.config import Settings
from app.retrieval import Retriever, load_chunks, tokenize


@pytest.fixture(scope="module")
def retriever():
    return Retriever.from_dir(Settings().data_dir)


def test_every_data_file_is_chunked():
    sources = {c.source for c in load_chunks(Settings().data_dir)}
    assert sources == {"services.md", "prices.md", "hours.md", "faq.md", "policies.md"}


def test_tokenize_stems_and_drops_stopwords():
    assert tokenize("What are the fillings?") == ["fill"]
    assert tokenize("filling") == tokenize("fillings") == tokenize("filled")
    assert tokenize("cleanings") == tokenize("cleaning")
    assert "price" in tokenize("How much does it cost?", expand=True)


@pytest.mark.parametrize(
    "question, expected_title",
    [
        ("How much is a dental implant?", "Price list"),
        ("When are you open on Saturday?", "Opening hours"),
        ("Do you accept insurance?", "Do you take insurance?"),
        ("Do you offer braces or Invisalign?", "Services we do not offer"),
        ("What is your cancellation policy?", "Cancellation policy"),
        ("Is there parking near the clinic?", "Getting here"),
        ("I'm scared of the dentist", "I am nervous about the dentist. Can you help?"),
        ("Can I pay in instalments?", "Payment"),
        ("Do you treat kids?", "Children's dentistry"),
        ("I have terrible toothache, what do I do?", "Emergency appointments"),
    ],
)
def test_top3_contains_expected_section(retriever, question, expected_title):
    titles = [h.chunk.title for h in retriever.search(question, k=3)]
    assert expected_title in titles


@pytest.mark.parametrize(
    "question",
    ["What is the capital of France?", "Write me a poem about pirates", "What's the weather tomorrow?", "Tell me a joke"],
)
def test_off_topic_questions_find_nothing_relevant(retriever, question):
    min_score = Settings().min_retrieval_score
    assert all(h.score < min_score for h in retriever.search(question))


def test_coverage_uses_synonyms_of_inflected_words(retriever):
    # "cleaning" is only a synonym key; coverage must look it up before stemming
    assert retriever.coverage("cleaning") == 1.0
