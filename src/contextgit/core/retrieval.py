import math
import re
from typing import List, Dict, Tuple, Any


def tokenize(text: str) -> List[str]:
    """Basic lowercase tokenization removing non-alphanumeric punctuation."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return [w for w in text.split() if w]


class BM25Searcher:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_contents: List[str] = []
        self.doc_ids: List[Any] = []
        self.corpus_size = 0
        self.avgdl = 0.0
        self.doc_lengths: List[int] = []
        self.tf: List[Dict[str, int]] = []
        self.df: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}

    def fit(self, doc_ids: List[Any], doc_contents: List[str]) -> None:
        """Fits the BM25 model on a list of document IDs and their text contents."""
        self.doc_ids = doc_ids
        self.doc_contents = doc_contents
        self.corpus_size = len(doc_contents)
        if self.corpus_size == 0:
            self.avgdl = 0.0
            return

        self.doc_lengths = []
        self.tf = []
        self.df = {}

        total_length = 0
        for doc in doc_contents:
            tokens = tokenize(doc)
            doc_len = len(tokens)
            self.doc_lengths.append(doc_len)
            total_length += doc_len

            freqs = {}
            for token in tokens:
                freqs[token] = freqs.get(token, 0) + 1
            self.tf.append(freqs)

            for token in freqs:
                self.df[token] = self.df.get(token, 0) + 1

        self.avgdl = total_length / self.corpus_size

        for token, df_val in self.df.items():
            # Standard BM25 IDF formulation with a floor of 10^-5 to avoid negative scores
            num = self.corpus_size - df_val + 0.5
            denom = df_val + 0.5
            val = math.log(num / denom + 1)
            self.idf[token] = max(val, 1e-5)

    def search(self, query: str, top_k: int = 10) -> List[Tuple[Any, float]]:
        """Searches the fitted corpus and returns sorted List of (doc_id, score)."""
        if self.corpus_size == 0:
            return []

        query_tokens = tokenize(query)
        scores: List[float] = []

        for i in range(self.corpus_size):
            score = 0.0
            doc_len = self.doc_lengths[i]
            freqs = self.tf[i]

            for token in query_tokens:
                if token not in freqs:
                    continue
                tf_val = freqs[token]
                token_idf = self.idf.get(token, 0.0)

                num = tf_val * (self.k1 + 1)
                denom = tf_val + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                score += token_idf * (num / denom)

            scores.append(score)

        ranked = sorted(
            zip(self.doc_ids, scores),
            key=lambda x: x[1],
            reverse=True
        )
        return ranked[:top_k]
